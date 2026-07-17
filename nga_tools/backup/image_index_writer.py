from __future__ import annotations

import queue
import sqlite3
import threading
from collections.abc import Generator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from nga_tools.core.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS, configure_connection


type ImageMappingRow = tuple[str, str]

_QUEUE_CAPACITY = 1024
_MAX_TRANSACTION_ROWS = 256
_MAX_COALESCE_SECONDS = 0.050


@dataclass(frozen=True)
class ImageIndexWriterMetrics:
    rows_written: int
    transactions: int
    write_batches: int
    requests_submitted: int
    max_transaction_rows: int
    peak_queue_depth: int
    queue_put_seconds: float
    coalesce_wait_seconds: float
    transaction_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        mean_rows = (
            0.0
            if self.transactions == 0
            else self.rows_written / self.transactions
        )
        return {
            "rows_written": self.rows_written,
            "transactions": self.transactions,
            "write_batches": self.write_batches,
            "requests_submitted": self.requests_submitted,
            "max_transaction_rows": self.max_transaction_rows,
            "peak_queue_depth": self.peak_queue_depth,
            "queue_put_seconds": self.queue_put_seconds,
            "coalesce_wait_seconds": self.coalesce_wait_seconds,
            "transaction_seconds": self.transaction_seconds,
            "mean_rows_per_transaction": mean_rows,
        }


@dataclass(frozen=True)
class _WriteRequest:
    rows: tuple[ImageMappingRow, ...]
    future: Future[None]


@dataclass(frozen=True)
class _StopRequest:
    pass


type _QueueItem = _WriteRequest | _StopRequest


class ImageIndexWriter:
    """A bounded, durable, command-scoped SQLite mapping writer."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path.resolve()
        self._queue: queue.Queue[_QueueItem] = queue.Queue(_QUEUE_CAPACITY)
        self._state_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._closed = False
        self._fatal_error: BaseException | None = None
        self._rows_written = 0
        self._transactions = 0
        self._write_batches = 0
        self._requests_submitted = 0
        self._max_transaction_rows = 0
        self._peak_queue_depth = 0
        self._queue_put_seconds = 0.0
        self._coalesce_wait_seconds = 0.0
        self._transaction_seconds = 0.0

    def _ensure_started(self) -> None:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("图片索引写入器已经关闭。")
            if self._fatal_error is not None:
                raise RuntimeError("图片索引写入器已经失败。") from self._fatal_error
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._thread_main,
                name="nga-image-index-writer",
                daemon=True,
            )
            self._thread.start()

    def submit(self, rows: Sequence[ImageMappingRow]) -> Future[None]:
        future = Future[None]()
        if not rows:
            future.set_result(None)
            return future
        self._ensure_started()
        put_started_at = monotonic()
        self._queue.put(_WriteRequest(tuple(rows), future))
        put_seconds = monotonic() - put_started_at
        with self._state_lock:
            self._requests_submitted += 1
            self._queue_put_seconds += put_seconds
            self._peak_queue_depth = max(
                self._peak_queue_depth,
                self._queue.qsize(),
            )
        return future

    def _thread_main(self) -> None:
        try:
            connection = sqlite3.connect(
                self.db_path,
                timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            )
            configure_connection(connection)
        except BaseException as error:
            with self._state_lock:
                self._fatal_error = error
            self._fail_queued_requests(error)
            return

        stop_after_batch = False
        try:
            while not stop_after_batch:
                first = self._queue.get()
                if isinstance(first, _StopRequest):
                    break
                requests = [first]
                row_count = len(first.rows)
                deadline = monotonic() + _MAX_COALESCE_SECONDS
                while row_count < _MAX_TRANSACTION_ROWS:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        break
                    coalesce_started_at = monotonic()
                    try:
                        item = self._queue.get(timeout=remaining)
                    except queue.Empty:
                        with self._state_lock:
                            self._coalesce_wait_seconds += (
                                monotonic() - coalesce_started_at
                            )
                        break
                    with self._state_lock:
                        self._coalesce_wait_seconds += (
                            monotonic() - coalesce_started_at
                        )
                    if isinstance(item, _StopRequest):
                        stop_after_batch = True
                        break
                    requests.append(item)
                    row_count += len(item.rows)
                self._write_requests(connection, requests)
        finally:
            connection.close()

    def _write_requests(
        self,
        connection: sqlite3.Connection,
        requests: list[_WriteRequest],
    ) -> None:
        rows = [row for request in requests for row in request.rows]
        try:
            for start in range(0, len(rows), _MAX_TRANSACTION_ROWS):
                chunk = rows[start : start + _MAX_TRANSACTION_ROWS]
                transaction_started_at = monotonic()
                try:
                    with connection:
                        connection.executemany(
                            """
                            INSERT INTO image_mappings (
                                url,
                                unique_rel_path
                            )
                            VALUES (?, ?)
                            ON CONFLICT(url) DO UPDATE SET
                                unique_rel_path = excluded.unique_rel_path
                            """,
                            chunk,
                        )
                finally:
                    with self._state_lock:
                        self._transaction_seconds += (
                            monotonic() - transaction_started_at
                        )
                with self._state_lock:
                    self._rows_written += len(chunk)
                    self._transactions += 1
                    self._max_transaction_rows = max(
                        self._max_transaction_rows,
                        len(chunk),
                    )
            with self._state_lock:
                self._write_batches += 1
        except BaseException as error:
            for request in requests:
                request.future.set_exception(error)
            return
        for request in requests:
            request.future.set_result(None)

    def _fail_queued_requests(self, error: BaseException) -> None:
        while True:
            item = self._queue.get()
            if isinstance(item, _StopRequest):
                return
            item.future.set_exception(error)

    def snapshot(self) -> ImageIndexWriterMetrics:
        with self._state_lock:
            return ImageIndexWriterMetrics(
                rows_written=self._rows_written,
                transactions=self._transactions,
                write_batches=self._write_batches,
                requests_submitted=self._requests_submitted,
                max_transaction_rows=self._max_transaction_rows,
                peak_queue_depth=self._peak_queue_depth,
                queue_put_seconds=self._queue_put_seconds,
                coalesce_wait_seconds=self._coalesce_wait_seconds,
                transaction_seconds=self._transaction_seconds,
            )

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            thread = self._thread
        if thread is None:
            return
        self._queue.put(_StopRequest())
        thread.join()


class _ImageIndexWriterScope:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._writer: ImageIndexWriter | None = None

    def writer_for_path(self, db_path: Path) -> ImageIndexWriter:
        resolved_path = db_path.resolve()
        with self._lock:
            if self._writer is None:
                self._writer = ImageIndexWriter(resolved_path)
            elif self._writer.db_path != resolved_path:
                raise RuntimeError(
                    "同一命令不能写入多个图片索引："
                    f"active={self._writer.db_path}, requested={resolved_path}"
                )
            return self._writer

    def close(self) -> None:
        with self._lock:
            writer = self._writer
        if writer is not None:
            writer.close()

    def snapshot(self) -> ImageIndexWriterMetrics | None:
        with self._lock:
            writer = self._writer
        return None if writer is None else writer.snapshot()


_scope_lock = threading.RLock()
_active_scope: _ImageIndexWriterScope | None = None
_scope_depth = 0
_last_metrics: ImageIndexWriterMetrics | None = None


def active_image_index_writer(db_path: Path) -> ImageIndexWriter | None:
    with _scope_lock:
        scope = _active_scope
    return None if scope is None else scope.writer_for_path(db_path)


def image_index_writer_metrics() -> ImageIndexWriterMetrics | None:
    with _scope_lock:
        if _active_scope is not None:
            return _active_scope.snapshot()
        return _last_metrics


@contextmanager
def use_image_index_writer() -> Generator[None]:
    global _active_scope, _scope_depth, _last_metrics
    with _scope_lock:
        if _active_scope is None:
            _active_scope = _ImageIndexWriterScope()
        _scope_depth += 1
    try:
        yield
    finally:
        close_scope: _ImageIndexWriterScope | None = None
        with _scope_lock:
            _scope_depth -= 1
            if _scope_depth == 0:
                close_scope = _active_scope
                _active_scope = None
        if close_scope is not None:
            close_scope.close()
            with _scope_lock:
                _last_metrics = close_scope.snapshot()

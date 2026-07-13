from __future__ import annotations

import asyncio
import queue
import threading
import traceback
from collections import deque
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import TypeVar

import aiohttp

from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.atomic import replace_temp_file, temporary_sibling_path
from nga_tools.core.downloads import (
    DownloadFailureKind,
    DownloadFileResult,
    DownloadProgressCallback,
    DownloadSummary,
    DownloadTask,
)
from nga_tools.replay.offline import (
    ReplayOfflineError,
    assert_replay_request_allowed,
    current_replay_network_policy,
)


@dataclass(frozen=True)
class ImageDownloadRuntimeMetrics:
    capacity: int
    batches_submitted: int
    items_submitted: int
    items_completed: int
    active_downloads: int
    peak_active_downloads: int
    queued_items: int
    peak_queued_items: int
    retry_count: int
    queue_wait_seconds: float
    service_seconds: float
    runtime_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        utilization = (
            0.0
            if self.runtime_seconds <= 0
            else self.service_seconds / (self.runtime_seconds * self.capacity)
        )
        return {
            "capacity": self.capacity,
            "batches_submitted": self.batches_submitted,
            "items_submitted": self.items_submitted,
            "items_completed": self.items_completed,
            "active_downloads": self.active_downloads,
            "peak_active_downloads": self.peak_active_downloads,
            "queued_items": self.queued_items,
            "peak_queued_items": self.peak_queued_items,
            "retry_count": self.retry_count,
            "queue_wait_seconds": self.queue_wait_seconds,
            "service_seconds": self.service_seconds,
            "runtime_seconds": self.runtime_seconds,
            "capacity_utilization": min(1.0, utilization),
        }


@dataclass(frozen=True)
class _RetryEvent:
    logical_url: str
    error: BaseException
    retry_number: int
    retries: int
    wait_seconds: float


@dataclass(frozen=True)
class _ResultEvent:
    index: int
    result: DownloadFileResult


@dataclass(frozen=True)
class _FatalEvent:
    error: BaseException


type _BatchEvent = _RetryEvent | _ResultEvent | _FatalEvent


@dataclass(frozen=True)
class _AttemptFailure:
    error: BaseException
    failure_kind: DownloadFailureKind
    http_status: int | None
    retryable: bool


@dataclass
class _DownloadBatch:
    batch_id: int
    items: tuple[DownloadTask, ...]
    retries: int
    backoff_factor: float
    retry_statuses: tuple[int, ...]
    batch_limit: int
    events: queue.Queue[_BatchEvent]
    enqueued_at: float
    next_item: int = 0
    outstanding: int = 0
    running: int = 0
    delayed: int = 0
    consumed: int = 0
    cancelled: bool = False
    fatal_error: BaseException | None = None

    def __post_init__(self) -> None:
        self.retry_items: deque[int] = deque()
        self.attempts = [0] * len(self.items)
        self.ready_at = [self.enqueued_at] * len(self.items)

    def can_dispatch(self) -> bool:
        return (
            not self.cancelled
            and self.fatal_error is None
            and bool(self.retry_items or (
                self.next_item < len(self.items)
                and self.outstanding < self.batch_limit
            ))
        )


class ImageDownloadRuntime:
    """One background event loop with fair, fixed-worker image downloads."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("图片下载运行时并发数必须大于0。")
        self.capacity = capacity
        self._state_lock = threading.RLock()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="nga-image-runtime",
            daemon=True,
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._startup_error: BaseException | None = None
        self._work_available: asyncio.Event | None = None
        self._shutdown_requested: asyncio.Event | None = None
        self._batches: dict[int, _DownloadBatch] = {}
        self._ready_batches: deque[int] = deque()
        self._ready_batch_ids: set[int] = set()
        self._next_batch_id = 1
        self._closing = False
        self._closed = False
        self._batches_submitted = 0
        self._started_at = perf_counter()
        self._items_submitted = 0
        self._items_completed = 0
        self._active_downloads = 0
        self._peak_active_downloads = 0
        self._peak_queued_items = 0
        self._retry_count = 0
        self._queue_wait_seconds = 0.0
        self._service_seconds = 0.0
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("图片下载运行时启动超时。")
        if self._startup_error is not None:
            raise RuntimeError("图片下载运行时启动失败。") from self._startup_error

    def _thread_main(self) -> None:
        try:
            asyncio.run(self._async_main())
        except BaseException as error:
            self._startup_error = error
            self._ready.set()

    async def _async_main(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._work_available = asyncio.Event()
        self._shutdown_requested = asyncio.Event()
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(limit=self.capacity)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                connector=connector,
                trust_env=False,
            ) as session:
                workers = tuple(
                    asyncio.create_task(self._worker(session))
                    for _index in range(self.capacity)
                )
                self._ready.set()
                await self._shutdown_requested.wait()
                self._closing = True
                self._work_available.set()
                await asyncio.gather(*workers)
        finally:
            self._closed = True
            self._ready.set()

    def _loop_or_raise(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or self._closed:
            raise RuntimeError("图片下载运行时已经关闭。")
        return loop

    def _run_in_loop(
        self,
        coroutine: Coroutine[object, object, _LoopResultT],
    ) -> _LoopResultT:
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop_or_raise())
        return future.result()

    def _queued_items(self) -> int:
        return sum(
            max(0, len(batch.items) - batch.next_item) + len(batch.retry_items)
            for batch in self._batches.values()
            if not batch.cancelled and batch.fatal_error is None
        )

    def _record_queue_peak(self) -> None:
        self._peak_queued_items = max(
            self._peak_queued_items,
            self._queued_items(),
        )

    def _make_ready(self, batch: _DownloadBatch) -> None:
        if batch.can_dispatch() and batch.batch_id not in self._ready_batch_ids:
            self._ready_batches.append(batch.batch_id)
            self._ready_batch_ids.add(batch.batch_id)
            if self._work_available is not None:
                self._work_available.set()

    async def _register_batch(
        self,
        items: tuple[DownloadTask, ...],
        retries: int,
        backoff_factor: float,
        retry_statuses: tuple[int, ...],
        batch_limit: int,
    ) -> _DownloadBatch:
        if self._closing or self._closed:
            raise RuntimeError("图片下载运行时已经关闭。")
        batch = _DownloadBatch(
            batch_id=self._next_batch_id,
            items=items,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=batch_limit,
            events=queue.Queue(),
            enqueued_at=perf_counter(),
        )
        self._next_batch_id += 1
        self._batches[batch.batch_id] = batch
        self._batches_submitted += 1
        self._items_submitted += len(items)
        self._record_queue_peak()
        self._make_ready(batch)
        return batch

    async def _consume_result(self, batch_id: int) -> None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        batch.outstanding -= 1
        batch.consumed += 1
        self._make_ready(batch)
        if batch.consumed >= len(batch.items):
            self._remove_batch(batch_id)

    async def _cancel_batch(self, batch_id: int) -> None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        batch.cancelled = True
        batch.retry_items.clear()
        self._ready_batch_ids.discard(batch_id)
        if batch.running == 0:
            self._remove_batch(batch_id)

    async def _cancel_all_and_wait(self) -> None:
        for batch in tuple(self._batches.values()):
            batch.cancelled = True
            batch.retry_items.clear()
            self._ready_batch_ids.discard(batch.batch_id)
            if batch.running == 0:
                self._remove_batch(batch.batch_id)
        while self._batches:
            await asyncio.sleep(0.001)

    def _remove_batch(self, batch_id: int) -> None:
        self._batches.pop(batch_id, None)
        self._ready_batch_ids.discard(batch_id)
        if self._ready_batches:
            self._ready_batches = deque(
                current_id
                for current_id in self._ready_batches
                if current_id != batch_id
            )

    def download(
        self,
        items: list[DownloadTask],
        *,
        retries: int,
        backoff_factor: float,
        retry_statuses: tuple[int, ...],
        batch_limit: int,
        on_progress: DownloadProgressCallback | None,
    ) -> DownloadSummary:
        if not items:
            return {"succeeded": [], "failed": []}
        if batch_limit <= 0 or batch_limit > self.capacity:
            raise ValueError("图片下载批次并发数无效。")
        registered = self._run_in_loop(
            self._register_batch(
                tuple(items),
                retries,
                backoff_factor,
                retry_statuses,
                batch_limit,
            )
        )
        batch = registered
        succeeded: list[DownloadFileResult] = []
        failed: list[DownloadFileResult] = []
        completed = 0
        try:
            while completed < len(items):
                event = batch.events.get()
                if isinstance(event, _RetryEvent):
                    report_warning(
                        WarningCategory.DOWNLOAD_RETRY,
                        f"Download failed ({event.error}), retrying "
                        f"{event.retry_number}/{event.retries} after "
                        f"{event.wait_seconds:.1f}s: {event.logical_url}",
                    )
                    continue
                if isinstance(event, _FatalEvent):
                    raise event.error
                result = event.result
                self._run_in_loop(self._consume_result(batch.batch_id))
                completed += 1
                if result["success"]:
                    succeeded.append(result)
                else:
                    failed.append(result)
                if on_progress is not None:
                    on_progress(completed, len(items), result)
        except BaseException:
            self._run_in_loop(self._cancel_batch(batch.batch_id))
            raise
        return {"succeeded": succeeded, "failed": failed}

    def _take_work(self) -> tuple[_DownloadBatch, int] | None:
        while self._ready_batches:
            batch_id = self._ready_batches.popleft()
            self._ready_batch_ids.discard(batch_id)
            batch = self._batches.get(batch_id)
            if batch is None or not batch.can_dispatch():
                continue
            if batch.retry_items:
                index = batch.retry_items.popleft()
            else:
                index = batch.next_item
                batch.next_item += 1
                batch.outstanding += 1
            batch.running += 1
            self._active_downloads += 1
            self._peak_active_downloads = max(
                self._peak_active_downloads,
                self._active_downloads,
            )
            self._make_ready(batch)
            return batch, index
        if self._work_available is not None:
            self._work_available.clear()
        return None

    async def _worker(self, session: aiohttp.ClientSession) -> None:
        while True:
            work = self._take_work()
            while work is None:
                if self._closing:
                    return
                if self._work_available is None:
                    raise RuntimeError("图片下载工作事件未初始化。")
                await self._work_available.wait()
                work = self._take_work()
            batch, index = work
            started_at = perf_counter()
            self._queue_wait_seconds += max(
                0.0,
                started_at - batch.ready_at[index],
            )
            fatal_error: BaseException | None = None
            attempt_result: DownloadFileResult | _AttemptFailure | None = None
            try:
                attempt_result = await self._download_attempt(
                    session,
                    batch.items[index],
                    batch.retry_statuses,
                )
            except ReplayOfflineError as error:
                fatal_error = error
            except BaseException as error:
                attempt_result = {
                    "url": batch.items[index]["url"],
                    "save_path": batch.items[index]["save_path"],
                    "success": False,
                    "error": f"{error}\n{traceback.format_exc().rstrip()}",
                    "failure_kind": "unexpected_download",
                }
            ended_at = perf_counter()
            batch.running -= 1
            self._active_downloads -= 1
            self._service_seconds += ended_at - started_at

            if batch.cancelled:
                if batch.running == 0:
                    self._remove_batch(batch.batch_id)
                continue
            if fatal_error is not None:
                batch.fatal_error = fatal_error
                batch.cancelled = True
                batch.events.put(_FatalEvent(fatal_error))
                self._ready_batch_ids.discard(batch.batch_id)
                continue
            if attempt_result is None:
                raise RuntimeError("图片下载尝试未产生结果。")
            if isinstance(attempt_result, _AttemptFailure):
                attempt = batch.attempts[index]
                if attempt < batch.retries and attempt_result.retryable:
                    wait_seconds = batch.backoff_factor * (2**attempt)
                    batch.attempts[index] += 1
                    batch.delayed += 1
                    self._retry_count += 1
                    batch.events.put(
                        _RetryEvent(
                            logical_url=batch.items[index]["url"],
                            error=attempt_result.error,
                            retry_number=attempt + 1,
                            retries=batch.retries,
                            wait_seconds=wait_seconds,
                        )
                    )
                    loop = asyncio.get_running_loop()
                    loop.call_later(
                        wait_seconds,
                        self._retry_ready,
                        batch.batch_id,
                        index,
                    )
                    continue
                attempt_result = self._failure_result(
                    batch.items[index],
                    attempt_result,
                )
            self._items_completed += 1
            batch.events.put(_ResultEvent(index=index, result=attempt_result))

    def _retry_ready(self, batch_id: int, index: int) -> None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        batch.delayed = max(0, batch.delayed - 1)
        if batch.cancelled or batch.fatal_error is not None:
            return
        batch.ready_at[index] = perf_counter()
        batch.retry_items.append(index)
        self._make_ready(batch)

    async def _download_attempt(
        self,
        session: aiohttp.ClientSession,
        item: DownloadTask,
        retry_statuses: tuple[int, ...],
    ) -> DownloadFileResult | _AttemptFailure:
        logical_url = item["url"]
        request_url = item.get("request_url", logical_url)
        target_path = Path(item["save_path"])
        temp_path: Path | None = None
        try:
            assert_replay_request_allowed(request_url)
            replay_mode = current_replay_network_policy() is not None
            response_context = (
                session.get(request_url, allow_redirects=False)
                if replay_mode
                else session.get(request_url)
            )
            async with response_context as response:
                if response.status >= 300:
                    raise aiohttp.ClientResponseError(
                        request_info=response.request_info,
                        history=response.history,
                        status=response.status,
                        message=f"HTTP {response.status}",
                        headers=response.headers,
                    )
                target_path.parent.mkdir(parents=True, exist_ok=True)
                temp_path = temporary_sibling_path(target_path)
                with temp_path.open("wb") as output_file:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        output_file.write(chunk)
                replace_temp_file(temp_path, target_path)
                temp_path = None
            return {
                "url": logical_url,
                "save_path": str(target_path),
                "success": True,
            }
        except (
            aiohttp.ClientConnectorError,
            aiohttp.ClientPayloadError,
            aiohttp.ClientResponseError,
            asyncio.TimeoutError,
        ) as error:
            status = (
                error.status
                if isinstance(error, aiohttp.ClientResponseError)
                else None
            )
            failure_kind: DownloadFailureKind
            if isinstance(error, aiohttp.ClientResponseError):
                if 300 <= error.status < 400:
                    failure_kind = "http_3xx"
                elif error.status < 500:
                    failure_kind = "http_4xx"
                else:
                    failure_kind = "http_5xx"
            elif isinstance(error, asyncio.TimeoutError):
                failure_kind = "timeout"
            elif isinstance(error, aiohttp.ClientConnectorError):
                failure_kind = "connection"
            else:
                failure_kind = "payload"
            return _AttemptFailure(
                error=error,
                failure_kind=failure_kind,
                http_status=status,
                retryable=(status in retry_statuses if status is not None else True),
            )
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink()
                except FileNotFoundError:
                    pass

    @staticmethod
    def _failure_result(
        item: DownloadTask,
        failure: _AttemptFailure,
    ) -> DownloadFileResult:
        result: DownloadFileResult = {
            "url": item["url"],
            "save_path": item["save_path"],
            "success": False,
            "error": str(failure.error),
            "failure_kind": failure.failure_kind,
        }
        if failure.http_status is not None:
            result["http_status"] = failure.http_status
        return result

    def snapshot(self) -> ImageDownloadRuntimeMetrics:
        with self._state_lock:
            return ImageDownloadRuntimeMetrics(
                capacity=self.capacity,
                batches_submitted=self._batches_submitted,
                items_submitted=self._items_submitted,
                items_completed=self._items_completed,
                active_downloads=self._active_downloads,
                peak_active_downloads=self._peak_active_downloads,
                queued_items=self._queued_items(),
                peak_queued_items=self._peak_queued_items,
                retry_count=self._retry_count,
                queue_wait_seconds=self._queue_wait_seconds,
                service_seconds=self._service_seconds,
                runtime_seconds=max(0.0, perf_counter() - self._started_at),
            )

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._run_in_loop(self._cancel_all_and_wait())
            loop = self._loop_or_raise()
            shutdown = self._shutdown_requested
            if shutdown is None:
                raise RuntimeError("图片下载关闭事件未初始化。")
            loop.call_soon_threadsafe(shutdown.set)
        self._thread.join()


_runtime_lock = threading.RLock()
_active_runtime: ImageDownloadRuntime | None = None
_runtime_scope_depth = 0
_last_metrics: ImageDownloadRuntimeMetrics | None = None


def current_image_download_runtime() -> ImageDownloadRuntime | None:
    with _runtime_lock:
        return _active_runtime


def active_image_download_runtime_capacity() -> int | None:
    runtime = current_image_download_runtime()
    return None if runtime is None else runtime.capacity


def image_download_runtime_metrics() -> ImageDownloadRuntimeMetrics | None:
    with _runtime_lock:
        if _active_runtime is not None:
            return _active_runtime.snapshot()
        return _last_metrics


@contextmanager
def use_image_download_runtime(
    capacity: int,
) -> Generator[ImageDownloadRuntime]:
    global _active_runtime, _runtime_scope_depth, _last_metrics
    with _runtime_lock:
        if _active_runtime is None:
            _active_runtime = ImageDownloadRuntime(capacity)
        elif _active_runtime.capacity != capacity:
            raise RuntimeError(
                "图片下载运行时活动期间不能修改并发数："
                f"active={_active_runtime.capacity}, requested={capacity}"
            )
        runtime = _active_runtime
        _runtime_scope_depth += 1
    try:
        yield runtime
    finally:
        close_runtime: ImageDownloadRuntime | None = None
        with _runtime_lock:
            _runtime_scope_depth -= 1
            if _runtime_scope_depth == 0:
                close_runtime = _active_runtime
                _active_runtime = None
        if close_runtime is not None:
            close_runtime.close()
            with _runtime_lock:
                _last_metrics = close_runtime.snapshot()
_LoopResultT = TypeVar("_LoopResultT")

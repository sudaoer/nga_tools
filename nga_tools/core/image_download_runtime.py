from __future__ import annotations

import asyncio
import queue
import threading
import traceback
from collections import deque
from collections.abc import Coroutine, Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from hashlib import sha256
from pathlib import Path
from time import perf_counter
from typing import TypeVar
from urllib.parse import urlsplit

import aiohttp

from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.atomic import replace_temp_file, temporary_sibling_path
from nga_tools.core.download_types import (
    DownloadFailureKind,
    DownloadFileResult,
    DownloadProgressCallback,
    DownloadResourceKind,
    DownloadSummary,
    DownloadTask,
)
from nga_tools.core.nga_attachment import (
    attachment_url_alias,
    is_nga_legacy_attachment_host,
)
from nga_tools.replay.offline import (
    ReplayOfflineError,
    audio_request_url,
    assert_replay_request_allowed,
    current_replay_network_policy,
    image_request_url,
)


_ATTACHMENT_FALLBACK_PROBE_TIMEOUT_SECONDS = 10.0


def _download_request_urls(
    logical_url: str,
    *,
    explicit_request_url: str | None,
    resource_kind: DownloadResourceKind,
) -> tuple[str, ...]:
    if explicit_request_url is not None:
        return (explicit_request_url,)
    if current_replay_network_policy() is not None:
        if resource_kind == "image":
            return (image_request_url(logical_url),)
        return (audio_request_url(logical_url),)
    alias_url = attachment_url_alias(logical_url)
    if alias_url is None:
        return (logical_url,)
    return (logical_url, alias_url)


def _attachment_probe_timeout(
    request_url: str,
    *,
    has_fallback: bool,
) -> aiohttp.ClientTimeout | None:
    if not has_fallback:
        return None
    host = urlsplit(request_url).hostname
    if host is None or not is_nga_legacy_attachment_host(host):
        return None
    return aiohttp.ClientTimeout(
        total=_ATTACHMENT_FALLBACK_PROBE_TIMEOUT_SECONDS
    )


@dataclass(frozen=True)
class DownloadRuntimeMetrics:
    capacity: int
    batches_submitted: int
    items_submitted: int
    items_completed: int
    active_downloads: int
    peak_active_downloads: int
    queued_items: int
    peak_queued_items: int
    in_flight_requests: int
    peak_in_flight_requests: int
    pending_results: int
    peak_pending_results: int
    retry_count: int
    queue_wait_seconds: float
    service_seconds: float
    request_to_headers_seconds: float
    response_body_read_seconds: float
    temp_file_write_seconds: float
    content_hash_seconds: float
    atomic_replace_seconds: float
    result_delivery_wait_seconds: float
    progress_callback_seconds: float
    downloaded_bytes: int
    runtime_seconds: float

    def as_dict(self) -> dict[str, int | float | str]:
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
            "queued_items_kind": "logical_unstarted_or_retry",
            "in_flight_requests": self.in_flight_requests,
            "peak_in_flight_requests": self.peak_in_flight_requests,
            "pending_results": self.pending_results,
            "peak_pending_results": self.peak_pending_results,
            "retry_count": self.retry_count,
            "queue_wait_seconds": self.queue_wait_seconds,
            "service_seconds": self.service_seconds,
            "request_to_headers_seconds": self.request_to_headers_seconds,
            "response_body_read_seconds": self.response_body_read_seconds,
            "temp_file_write_seconds": self.temp_file_write_seconds,
            "content_hash_seconds": self.content_hash_seconds,
            "atomic_replace_seconds": self.atomic_replace_seconds,
            "result_delivery_wait_seconds": self.result_delivery_wait_seconds,
            "progress_callback_seconds": self.progress_callback_seconds,
            "downloaded_bytes": self.downloaded_bytes,
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
    completed_at: float


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


def _attempt_failure_priority(failure: _AttemptFailure) -> int:
    # Preserve explicit transient responses before considering a missing alias.
    if failure.http_status is not None and failure.retryable:
        return 0
    if failure.failure_kind == "payload":
        return 1
    # A responding host gives better evidence than an unreachable alias.
    if failure.http_status is not None:
        return 2
    return 3


def _combine_attempt_failures(
    failures: list[tuple[str, _AttemptFailure]],
) -> _AttemptFailure:
    selected = min(
        (failure for _, failure in failures),
        key=_attempt_failure_priority,
    )
    if len(failures) == 1:
        return selected

    details: list[str] = []
    for request_url, failure in failures:
        status = (
            "" if failure.http_status is None else f", HTTP {failure.http_status}"
        )
        details.append(
            f"{request_url} ({failure.failure_kind}{status}): {failure.error}"
        )
    return replace(
        selected,
        error=RuntimeError("All download URLs failed: " + "; ".join(details)),
    )


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
    pending_results: int = 0
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


class DownloadRuntime:
    """One background event loop with fair, fixed-worker file downloads."""

    def __init__(
        self,
        capacity: int,
        *,
        resource_kind: DownloadResourceKind = "image",
    ) -> None:
        if capacity <= 0:
            raise ValueError("文件下载运行时并发数必须大于0。")
        self.capacity = capacity
        self.resource_kind: DownloadResourceKind = resource_kind
        self.resource_label = "音频" if resource_kind == "audio" else "图片"
        self._state_lock = threading.RLock()
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"nga-{resource_kind}-runtime",
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
        self._in_flight_requests = 0
        self._peak_in_flight_requests = 0
        self._pending_results = 0
        self._peak_pending_results = 0
        self._retry_count = 0
        self._queue_wait_seconds = 0.0
        self._service_seconds = 0.0
        self._request_to_headers_seconds = 0.0
        self._response_body_read_seconds = 0.0
        self._temp_file_write_seconds = 0.0
        self._content_hash_seconds = 0.0
        self._atomic_replace_seconds = 0.0
        self._result_delivery_wait_seconds = 0.0
        self._progress_callback_seconds = 0.0
        self._downloaded_bytes = 0
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError(f"{self.resource_label}下载运行时启动超时。")
        if self._startup_error is not None:
            raise RuntimeError(
                f"{self.resource_label}下载运行时启动失败。"
            ) from self._startup_error

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
            raise RuntimeError(f"{self.resource_label}下载运行时已经关闭。")
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
            raise RuntimeError(f"{self.resource_label}下载运行时已经关闭。")
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
        with self._state_lock:
            self._batches_submitted += 1
            self._items_submitted += len(items)
        self._record_queue_peak()
        self._make_ready(batch)
        return batch

    async def _consume_result(self, batch_id: int, completed_at: float) -> None:
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        batch.pending_results -= 1
        with self._state_lock:
            self._pending_results -= 1
            self._result_delivery_wait_seconds += max(
                0.0,
                perf_counter() - completed_at,
            )
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
        self._discard_pending_results(batch)
        self._ready_batch_ids.discard(batch_id)
        if batch.running == 0:
            self._remove_batch(batch_id)

    async def _cancel_all_and_wait(self) -> None:
        for batch in tuple(self._batches.values()):
            batch.cancelled = True
            batch.retry_items.clear()
            self._discard_pending_results(batch)
            self._ready_batch_ids.discard(batch.batch_id)
            if batch.running == 0:
                self._remove_batch(batch.batch_id)
        while self._batches:
            await asyncio.sleep(0.001)

    def _discard_pending_results(self, batch: _DownloadBatch) -> None:
        if batch.pending_results <= 0:
            return
        with self._state_lock:
            self._pending_results -= batch.pending_results
        batch.pending_results = 0

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
        return self._download(
            items,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=batch_limit,
            on_progress=on_progress,
            collect_results=True,
        )

    def download_streaming(
        self,
        items: list[DownloadTask],
        *,
        retries: int,
        backoff_factor: float,
        retry_statuses: tuple[int, ...],
        batch_limit: int,
        on_progress: DownloadProgressCallback | None,
    ) -> None:
        self._download(
            items,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=batch_limit,
            on_progress=on_progress,
            collect_results=False,
        )

    def _download(
        self,
        items: list[DownloadTask],
        *,
        retries: int,
        backoff_factor: float,
        retry_statuses: tuple[int, ...],
        batch_limit: int,
        on_progress: DownloadProgressCallback | None,
        collect_results: bool,
    ) -> DownloadSummary:
        if not items:
            return {"succeeded": [], "failed": []}
        if batch_limit <= 0 or batch_limit > self.capacity:
            raise ValueError(f"{self.resource_label}下载批次并发数无效。")
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
                self._run_in_loop(
                    self._consume_result(batch.batch_id, event.completed_at)
                )
                completed += 1
                if collect_results:
                    if result["success"]:
                        succeeded.append(result)
                    else:
                        failed.append(result)
                if on_progress is not None:
                    callback_started_at = perf_counter()
                    try:
                        on_progress(completed, len(items), result)
                    finally:
                        with self._state_lock:
                            self._progress_callback_seconds += (
                                perf_counter() - callback_started_at
                            )
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
            with self._state_lock:
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
                    raise RuntimeError(
                        f"{self.resource_label}下载工作事件未初始化。"
                    )
                await self._work_available.wait()
                work = self._take_work()
            batch, index = work
            started_at = perf_counter()
            with self._state_lock:
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
            with self._state_lock:
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
                raise RuntimeError(f"{self.resource_label}下载尝试未产生结果。")
            if isinstance(attempt_result, _AttemptFailure):
                attempt = batch.attempts[index]
                if attempt < batch.retries and attempt_result.retryable:
                    wait_seconds = batch.backoff_factor * (2**attempt)
                    batch.attempts[index] += 1
                    batch.delayed += 1
                    with self._state_lock:
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
            with self._state_lock:
                self._items_completed += 1
            completed_at = perf_counter()
            batch.pending_results += 1
            with self._state_lock:
                self._pending_results += 1
                self._peak_pending_results = max(
                    self._peak_pending_results,
                    self._pending_results,
                )
            batch.events.put(
                _ResultEvent(
                    index=index,
                    result=attempt_result,
                    completed_at=completed_at,
                )
            )

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
        request_urls = _download_request_urls(
            logical_url,
            explicit_request_url=item.get("request_url"),
            resource_kind=self.resource_kind,
        )
        target_path = Path(item["save_path"])
        failures: list[tuple[str, _AttemptFailure]] = []
        for request_url in request_urls:
            attempt = await self._download_single_url(
                session,
                logical_url,
                request_url,
                target_path,
                retry_statuses,
                timeout=_attachment_probe_timeout(
                    request_url,
                    has_fallback=len(request_urls) > 1,
                ),
            )
            if not isinstance(attempt, _AttemptFailure):
                return attempt
            failures.append((request_url, attempt))
        if not failures:
            raise RuntimeError(f"{self.resource_label}下载尝试未产生结果。")
        return _combine_attempt_failures(failures)

    async def _download_single_url(
        self,
        session: aiohttp.ClientSession,
        logical_url: str,
        request_url: str,
        target_path: Path,
        retry_statuses: tuple[int, ...],
        *,
        timeout: aiohttp.ClientTimeout | None,
    ) -> DownloadFileResult | _AttemptFailure:
        temp_path: Path | None = None
        request_to_headers_seconds = 0.0
        response_body_read_seconds = 0.0
        temp_file_write_seconds = 0.0
        content_hash_seconds = 0.0
        atomic_replace_seconds = 0.0
        downloaded_bytes = 0
        content_hasher = sha256()
        request_started_at = 0.0
        headers_received = False
        request_started = False
        try:
            assert_replay_request_allowed(request_url)
            replay_mode = current_replay_network_policy() is not None
            if replay_mode:
                response_context = session.get(
                    request_url,
                    allow_redirects=False,
                )
            elif timeout is not None:
                response_context = session.get(
                    request_url,
                    timeout=timeout,
                )
            else:
                response_context = session.get(request_url)
            request_started_at = perf_counter()
            request_started = True
            with self._state_lock:
                self._in_flight_requests += 1
                self._peak_in_flight_requests = max(
                    self._peak_in_flight_requests,
                    self._in_flight_requests,
                )
            async with response_context as response:
                request_to_headers_seconds += perf_counter() - request_started_at
                headers_received = True
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
                file_opened_at = perf_counter()
                output_file = temp_path.open("wb")
                temp_file_write_seconds += perf_counter() - file_opened_at
                try:
                    chunks = response.content.iter_chunked(64 * 1024).__aiter__()
                    while True:
                        read_started_at = perf_counter()
                        try:
                            chunk = await anext(chunks)
                        except StopAsyncIteration:
                            response_body_read_seconds += (
                                perf_counter() - read_started_at
                            )
                            break
                        response_body_read_seconds += (
                            perf_counter() - read_started_at
                        )
                        downloaded_bytes += len(chunk)
                        hash_started_at = perf_counter()
                        content_hasher.update(chunk)
                        content_hash_seconds += perf_counter() - hash_started_at
                        write_started_at = perf_counter()
                        output_file.write(chunk)
                        temp_file_write_seconds += (
                            perf_counter() - write_started_at
                        )
                finally:
                    file_close_started_at = perf_counter()
                    output_file.close()
                    temp_file_write_seconds += (
                        perf_counter() - file_close_started_at
                    )
                content_encoding = response.headers.get(
                    "Content-Encoding",
                    "",
                ).lower()
                if (
                    content_encoding in {"", "identity"}
                    and response.content_length is not None
                    and downloaded_bytes != response.content_length
                ):
                    raise aiohttp.ClientPayloadError(
                        "响应Content-Length与实际下载字节数不一致："
                        f"expected={response.content_length}, "
                        f"actual={downloaded_bytes}"
                    )
                replace_started_at = perf_counter()
                replace_temp_file(temp_path, target_path)
                atomic_replace_seconds += perf_counter() - replace_started_at
                temp_path = None
            return {
                "url": logical_url,
                "save_path": str(target_path),
                "success": True,
                "content_sha256": content_hasher.hexdigest(),
                "content_bytes": downloaded_bytes,
            }
        except (
            aiohttp.ClientConnectionError,
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
            elif isinstance(error, aiohttp.ClientPayloadError):
                failure_kind = "payload"
            else:
                failure_kind = "connection"
            return _AttemptFailure(
                error=error,
                failure_kind=failure_kind,
                http_status=status,
                retryable=(status in retry_statuses if status is not None else True),
            )
        finally:
            if request_started:
                if not headers_received:
                    request_to_headers_seconds += (
                        perf_counter() - request_started_at
                    )
                with self._state_lock:
                    self._in_flight_requests -= 1
                    self._request_to_headers_seconds += (
                        request_to_headers_seconds
                    )
                    self._response_body_read_seconds += response_body_read_seconds
                    self._temp_file_write_seconds += temp_file_write_seconds
                    self._content_hash_seconds += content_hash_seconds
                    self._atomic_replace_seconds += atomic_replace_seconds
                    self._downloaded_bytes += downloaded_bytes
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

    def snapshot(self) -> DownloadRuntimeMetrics:
        with self._state_lock:
            return DownloadRuntimeMetrics(
                capacity=self.capacity,
                batches_submitted=self._batches_submitted,
                items_submitted=self._items_submitted,
                items_completed=self._items_completed,
                active_downloads=self._active_downloads,
                peak_active_downloads=self._peak_active_downloads,
                queued_items=self._queued_items(),
                peak_queued_items=self._peak_queued_items,
                in_flight_requests=self._in_flight_requests,
                peak_in_flight_requests=self._peak_in_flight_requests,
                pending_results=self._pending_results,
                peak_pending_results=self._peak_pending_results,
                retry_count=self._retry_count,
                queue_wait_seconds=self._queue_wait_seconds,
                service_seconds=self._service_seconds,
                request_to_headers_seconds=self._request_to_headers_seconds,
                response_body_read_seconds=self._response_body_read_seconds,
                temp_file_write_seconds=self._temp_file_write_seconds,
                content_hash_seconds=self._content_hash_seconds,
                atomic_replace_seconds=self._atomic_replace_seconds,
                result_delivery_wait_seconds=self._result_delivery_wait_seconds,
                progress_callback_seconds=self._progress_callback_seconds,
                downloaded_bytes=self._downloaded_bytes,
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
            raise RuntimeError(f"{self.resource_label}下载关闭事件未初始化。")
        loop.call_soon_threadsafe(shutdown.set)
        self._thread.join()


_runtime_lock = threading.RLock()
_active_runtimes: dict[DownloadResourceKind, DownloadRuntime] = {}
_runtime_scope_depths: dict[DownloadResourceKind, int] = {
    "image": 0,
    "audio": 0,
}
_last_metrics: dict[DownloadResourceKind, DownloadRuntimeMetrics | None] = {
    "image": None,
    "audio": None,
}


def current_download_runtime(
    resource_kind: DownloadResourceKind,
) -> DownloadRuntime | None:
    with _runtime_lock:
        return _active_runtimes.get(resource_kind)


def _current_image_download_runtime() -> DownloadRuntime | None:
    return current_download_runtime("image")


def _current_audio_download_runtime() -> DownloadRuntime | None:
    return current_download_runtime("audio")


def active_image_download_runtime_capacity() -> int | None:
    runtime = _current_image_download_runtime()
    return None if runtime is None else runtime.capacity


def active_audio_download_runtime_capacity() -> int | None:
    runtime = _current_audio_download_runtime()
    return None if runtime is None else runtime.capacity


def image_download_runtime_metrics() -> DownloadRuntimeMetrics | None:
    with _runtime_lock:
        runtime = _active_runtimes.get("image")
        if runtime is not None:
            return runtime.snapshot()
        return _last_metrics["image"]


def audio_download_runtime_metrics() -> DownloadRuntimeMetrics | None:
    with _runtime_lock:
        runtime = _active_runtimes.get("audio")
        if runtime is not None:
            return runtime.snapshot()
        return _last_metrics["audio"]


@contextmanager
def use_download_runtime(
    resource_kind: DownloadResourceKind,
    capacity: int,
) -> Generator[DownloadRuntime]:
    with _runtime_lock:
        active_runtime = _active_runtimes.get(resource_kind)
        if active_runtime is None:
            active_runtime = DownloadRuntime(
                capacity,
                resource_kind=resource_kind,
            )
            _active_runtimes[resource_kind] = active_runtime
        elif active_runtime.capacity != capacity:
            raise RuntimeError(
                f"{active_runtime.resource_label}下载运行时活动期间不能修改并发数："
                f"active={active_runtime.capacity}, requested={capacity}"
            )
        _runtime_scope_depths[resource_kind] += 1
    try:
        yield active_runtime
    finally:
        close_runtime: DownloadRuntime | None = None
        with _runtime_lock:
            _runtime_scope_depths[resource_kind] -= 1
            if _runtime_scope_depths[resource_kind] == 0:
                close_runtime = _active_runtimes.pop(resource_kind, None)
        if close_runtime is not None:
            close_runtime.close()
            with _runtime_lock:
                _last_metrics[resource_kind] = close_runtime.snapshot()


@contextmanager
def use_image_download_runtime(
    capacity: int,
) -> Generator[DownloadRuntime]:
    with use_download_runtime("image", capacity) as runtime:
        yield runtime


@contextmanager
def use_audio_download_runtime(
    capacity: int,
) -> Generator[DownloadRuntime]:
    with use_download_runtime("audio", capacity) as runtime:
        yield runtime
_LoopResultT = TypeVar("_LoopResultT")

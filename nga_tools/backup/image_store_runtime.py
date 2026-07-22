from __future__ import annotations

import os
import queue
import threading
from collections.abc import Callable, Generator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import Context, copy_context
from dataclasses import dataclass
from time import perf_counter
from typing import TypeVar, cast


_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class ImageStoreRuntimeMetrics:
    worker_count: int
    queue_capacity: int
    items_submitted: int
    items_completed: int
    active_workers: int
    peak_active_workers: int
    queued_items: int
    peak_queued_items: int
    submit_wait_seconds: float
    service_seconds: float
    runtime_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        utilization = (
            0.0
            if self.runtime_seconds <= 0
            else self.service_seconds / (self.runtime_seconds * self.worker_count)
        )
        return {
            "worker_count": self.worker_count,
            "queue_capacity": self.queue_capacity,
            "items_submitted": self.items_submitted,
            "items_completed": self.items_completed,
            "active_workers": self.active_workers,
            "peak_active_workers": self.peak_active_workers,
            "queued_items": self.queued_items,
            "peak_queued_items": self.peak_queued_items,
            "submit_wait_seconds": self.submit_wait_seconds,
            "service_seconds": self.service_seconds,
            "runtime_seconds": self.runtime_seconds,
            "capacity_utilization": min(1.0, utilization),
        }


@dataclass(frozen=True)
class _WorkItem:
    callback: Callable[[], object]
    context: Context
    future: Future[object]


@dataclass(frozen=True)
class _StopItem:
    pass


type _QueueItem = _WorkItem | _StopItem


class ImageStoreRuntime:
    """Bounded command-scoped workers for image validation and placement."""

    def __init__(self, worker_count: int) -> None:
        if worker_count <= 0:
            raise ValueError("图片落库工作线程数必须大于0。")
        self.worker_count = worker_count
        self.queue_capacity = worker_count * 2
        self.pending_limit = worker_count + self.queue_capacity
        self._queue: queue.Queue[_QueueItem] = queue.Queue(self.queue_capacity)
        self._state_lock = threading.RLock()
        self._closed = False
        self._items_submitted = 0
        self._items_completed = 0
        self._active_workers = 0
        self._peak_active_workers = 0
        self._queued_items = 0
        self._peak_queued_items = 0
        self._submit_wait_seconds = 0.0
        self._service_seconds = 0.0
        self._started_at = perf_counter()
        self._threads = tuple(
            threading.Thread(
                target=self._worker,
                name=f"nga-image-store-{index + 1}",
                daemon=True,
            )
            for index in range(worker_count)
        )
        for thread in self._threads:
            thread.start()

    def submit(self, callback: Callable[[], _ResultT]) -> Future[_ResultT]:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("图片落库运行时已经关闭。")
        future = Future[object]()
        item = _WorkItem(
            callback=cast(Callable[[], object], callback),
            context=copy_context(),
            future=future,
        )
        wait_started_at = perf_counter()
        with self._state_lock:
            self._items_submitted += 1
            self._queued_items += 1
            self._peak_queued_items = max(
                self._peak_queued_items,
                self._queued_items,
            )
        self._queue.put(item)
        wait_seconds = perf_counter() - wait_started_at
        with self._state_lock:
            self._submit_wait_seconds += wait_seconds
        return cast(Future[_ResultT], future)

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if isinstance(item, _StopItem):
                    return
                with self._state_lock:
                    self._queued_items -= 1
                    self._active_workers += 1
                    self._peak_active_workers = max(
                        self._peak_active_workers,
                        self._active_workers,
                    )
                started_at = perf_counter()
                try:
                    if item.future.set_running_or_notify_cancel():
                        try:
                            result = item.context.run(item.callback)
                        except BaseException as error:
                            item.future.set_exception(error)
                        else:
                            item.future.set_result(result)
                finally:
                    with self._state_lock:
                        self._active_workers -= 1
                        self._items_completed += 1
                        self._service_seconds += perf_counter() - started_at
            finally:
                self._queue.task_done()

    def snapshot(self) -> ImageStoreRuntimeMetrics:
        with self._state_lock:
            return ImageStoreRuntimeMetrics(
                worker_count=self.worker_count,
                queue_capacity=self.queue_capacity,
                items_submitted=self._items_submitted,
                items_completed=self._items_completed,
                active_workers=self._active_workers,
                peak_active_workers=self._peak_active_workers,
                queued_items=self._queued_items,
                peak_queued_items=self._peak_queued_items,
                submit_wait_seconds=self._submit_wait_seconds,
                service_seconds=self._service_seconds,
                runtime_seconds=max(0.0, perf_counter() - self._started_at),
            )

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._queue.join()
        for _thread in self._threads:
            self._queue.put(_StopItem())
        for thread in self._threads:
            thread.join()


def effective_image_store_workers(
    thread_workers: int,
    image_concurrency: int,
) -> int:
    if thread_workers <= 0:
        raise ValueError("备份工作线程数必须大于0。")
    if image_concurrency <= 0:
        raise ValueError("图片下载并发数必须大于0。")
    return min(thread_workers, image_concurrency, os.cpu_count() or 1)


_scope_lock = threading.RLock()
_active_runtime: ImageStoreRuntime | None = None
_scope_depth = 0
_last_metrics: ImageStoreRuntimeMetrics | None = None


def current_image_store_runtime() -> ImageStoreRuntime | None:
    with _scope_lock:
        return _active_runtime


def image_store_runtime_metrics() -> ImageStoreRuntimeMetrics | None:
    with _scope_lock:
        if _active_runtime is not None:
            return _active_runtime.snapshot()
        return _last_metrics


def image_store_pending_limit() -> int:
    runtime = current_image_store_runtime()
    return 1 if runtime is None else runtime.pending_limit


def submit_image_store_work(
    callback: Callable[[], _ResultT],
) -> Future[_ResultT]:
    runtime = current_image_store_runtime()
    if runtime is not None:
        return runtime.submit(callback)
    future = Future[_ResultT]()
    try:
        future.set_result(callback())
    except BaseException as error:
        future.set_exception(error)
    return future


@contextmanager
def use_image_store_runtime(worker_count: int) -> Generator[ImageStoreRuntime]:
    global _active_runtime, _scope_depth, _last_metrics
    with _scope_lock:
        if _active_runtime is None:
            _active_runtime = ImageStoreRuntime(worker_count)
        elif _active_runtime.worker_count != worker_count:
            raise RuntimeError(
                "图片落库运行时活动期间不能修改工作线程数："
                f"active={_active_runtime.worker_count}, requested={worker_count}"
            )
        runtime = _active_runtime
        _scope_depth += 1
    try:
        yield runtime
    finally:
        close_runtime: ImageStoreRuntime | None = None
        with _scope_lock:
            _scope_depth -= 1
            if _scope_depth == 0:
                close_runtime = _active_runtime
                _active_runtime = None
        if close_runtime is not None:
            close_runtime.close()
            with _scope_lock:
                _last_metrics = close_runtime.snapshot()

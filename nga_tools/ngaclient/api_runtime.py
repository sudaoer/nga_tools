from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Generic, TypeVar, cast

import requests

from nga_tools.ngaclient.session import create_api_session


_ItemT = TypeVar("_ItemT")
_ResultT = TypeVar("_ResultT")


@dataclass(frozen=True)
class APIRuntimeMetrics:
    capacity: int
    batches_submitted: int
    items_submitted: int
    items_completed: int
    active_requests: int
    peak_active_requests: int
    queued_items: int
    peak_queued_items: int
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
            "active_requests": self.active_requests,
            "peak_active_requests": self.peak_active_requests,
            "queued_items": self.queued_items,
            "peak_queued_items": self.peak_queued_items,
            "queue_wait_seconds": self.queue_wait_seconds,
            "service_seconds": self.service_seconds,
            "runtime_seconds": self.runtime_seconds,
            "capacity_utilization": min(1.0, utilization),
        }


@dataclass
class _ItemOutcome(Generic[_ResultT]):
    value: _ResultT | None = None
    error: BaseException | None = None


class _Batch(Generic[_ItemT, _ResultT]):
    def __init__(
        self,
        batch_id: int,
        items: Sequence[_ItemT],
        fetch: Callable[[requests.Session, _ItemT], _ResultT],
        *,
        result_window: int,
    ) -> None:
        self.batch_id = batch_id
        self.items = tuple(items)
        self.fetch = fetch
        self.result_window = result_window
        self.enqueued_at = perf_counter()
        self.next_dispatch = 0
        self.next_ordered_result = 0
        self.outstanding = 0
        self.running = 0
        self.cancelled = False
        self.first_error: BaseException | None = None
        self.outcomes: dict[int, _ItemOutcome[_ResultT]] = {}
        self.completion_order: deque[int] = deque()

    def can_dispatch(self) -> bool:
        return (
            not self.cancelled
            and self.first_error is None
            and self.next_dispatch < len(self.items)
            and self.outstanding < self.result_window
        )

    def is_finished(self) -> bool:
        if self.first_error is not None or self.cancelled:
            return self.running == 0
        return self.next_ordered_result >= len(self.items)


class FairAPIRuntime:
    """A fixed worker pool that schedules page batches round-robin."""

    def __init__(self, capacity: int) -> None:
        if capacity <= 0:
            raise ValueError("API运行时并发数必须大于0。")
        self.capacity = capacity
        self._condition = threading.Condition()
        self._batches: dict[int, _Batch[object, object]] = {}
        self._ready_batches: deque[int] = deque()
        self._ready_batch_ids: set[int] = set()
        self._next_batch_id = 1
        self._closing = False
        self._closed = False
        self._workers = tuple(
            threading.Thread(
                target=self._worker_main,
                name=f"nga-api-{index + 1}",
                daemon=True,
            )
            for index in range(capacity)
        )
        self._batches_submitted = 0
        self._started_at = perf_counter()
        self._items_submitted = 0
        self._items_completed = 0
        self._active_requests = 0
        self._peak_active_requests = 0
        self._peak_queued_items = 0
        self._queue_wait_seconds = 0.0
        self._service_seconds = 0.0
        for worker in self._workers:
            worker.start()

    def _queued_items_locked(self) -> int:
        return sum(
            max(0, len(batch.items) - batch.next_dispatch)
            for batch in self._batches.values()
            if not batch.cancelled and batch.first_error is None
        )

    def _record_queue_peak_locked(self) -> None:
        self._peak_queued_items = max(
            self._peak_queued_items,
            self._queued_items_locked(),
        )

    def _make_ready_locked(self, batch: _Batch[object, object]) -> None:
        if (
            batch.can_dispatch()
            and batch.batch_id not in self._ready_batch_ids
        ):
            self._ready_batches.append(batch.batch_id)
            self._ready_batch_ids.add(batch.batch_id)

    def _register_batch(
        self,
        items: Sequence[_ItemT],
        fetch: Callable[[requests.Session, _ItemT], _ResultT],
    ) -> _Batch[_ItemT, _ResultT]:
        with self._condition:
            if self._closing or self._closed:
                raise RuntimeError("NGA API运行时已经关闭。")
            batch_id = self._next_batch_id
            self._next_batch_id += 1
            batch = _Batch(
                batch_id,
                items,
                fetch,
                result_window=self.capacity,
            )
            erased_batch = cast(_Batch[object, object], batch)
            self._batches[batch_id] = erased_batch
            self._batches_submitted += 1
            self._items_submitted += len(items)
            self._record_queue_peak_locked()
            self._make_ready_locked(erased_batch)
            self._condition.notify_all()
        return batch

    def _remove_batch_locked(self, batch_id: int) -> None:
        self._batches.pop(batch_id, None)
        self._ready_batch_ids.discard(batch_id)
        if self._ready_batches:
            self._ready_batches = deque(
                current_id
                for current_id in self._ready_batches
                if current_id != batch_id
            )

    def _cancel_batch(self, batch: _Batch[object, object]) -> None:
        with self._condition:
            batch.cancelled = True
            self._ready_batch_ids.discard(batch.batch_id)
            self._condition.notify_all()

    def _wait_for_failed_batch_locked(
        self,
        batch: _Batch[object, object],
    ) -> BaseException:
        batch.cancelled = True
        while batch.running:
            self._condition.wait()
        error = batch.first_error
        self._remove_batch_locked(batch.batch_id)
        if error is None:
            return RuntimeError("NGA API批次已取消。")
        return error

    def map_unordered(
        self,
        items: Sequence[_ItemT],
        fetch: Callable[[requests.Session, _ItemT], _ResultT],
    ) -> Generator[tuple[_ItemT, _ResultT]]:
        if not items:
            return
        batch = self._register_batch(items, fetch)
        erased_batch = cast(_Batch[object, object], batch)
        yielded = 0
        try:
            while yielded < len(batch.items):
                with self._condition:
                    while not batch.completion_order and batch.first_error is None:
                        self._condition.wait()
                    if batch.first_error is not None:
                        raise self._wait_for_failed_batch_locked(erased_batch)
                    index = batch.completion_order.popleft()
                    outcome = batch.outcomes.pop(index)
                    batch.outstanding -= 1
                    yielded += 1
                    batch.next_ordered_result = yielded
                    self._make_ready_locked(erased_batch)
                    self._condition.notify_all()
                if outcome.error is not None:
                    raise outcome.error
                yield batch.items[index], cast(_ResultT, outcome.value)
        finally:
            with self._condition:
                if yielded < len(batch.items):
                    self._cancel_batch(erased_batch)
                    while batch.running:
                        self._condition.wait()
                self._remove_batch_locked(batch.batch_id)
                self._condition.notify_all()

    def map_ordered(
        self,
        items: Sequence[_ItemT],
        fetch: Callable[[requests.Session, _ItemT], _ResultT],
    ) -> Generator[tuple[_ItemT, _ResultT]]:
        if not items:
            return
        batch = self._register_batch(items, fetch)
        erased_batch = cast(_Batch[object, object], batch)
        yielded = 0
        try:
            while yielded < len(batch.items):
                with self._condition:
                    while yielded not in batch.outcomes and batch.first_error is None:
                        self._condition.wait()
                    if batch.first_error is not None:
                        raise self._wait_for_failed_batch_locked(erased_batch)
                    outcome = batch.outcomes.pop(yielded)
                    try:
                        batch.completion_order.remove(yielded)
                    except ValueError:
                        pass
                    batch.outstanding -= 1
                    index = yielded
                    yielded += 1
                    batch.next_ordered_result = yielded
                    self._make_ready_locked(erased_batch)
                    self._condition.notify_all()
                if outcome.error is not None:
                    raise outcome.error
                yield batch.items[index], cast(_ResultT, outcome.value)
        finally:
            with self._condition:
                if yielded < len(batch.items):
                    self._cancel_batch(erased_batch)
                    while batch.running:
                        self._condition.wait()
                self._remove_batch_locked(batch.batch_id)
                self._condition.notify_all()

    def _take_work_locked(
        self,
    ) -> tuple[_Batch[object, object], int, object] | None:
        while self._ready_batches:
            batch_id = self._ready_batches.popleft()
            self._ready_batch_ids.discard(batch_id)
            batch = self._batches.get(batch_id)
            if batch is None or not batch.can_dispatch():
                continue
            index = batch.next_dispatch
            batch.next_dispatch += 1
            batch.outstanding += 1
            batch.running += 1
            self._active_requests += 1
            self._peak_active_requests = max(
                self._peak_active_requests,
                self._active_requests,
            )
            self._make_ready_locked(batch)
            return batch, index, batch.items[index]
        return None

    def _worker_main(self) -> None:
        session = create_api_session()
        try:
            while True:
                with self._condition:
                    work = self._take_work_locked()
                    while work is None and not self._closing:
                        self._condition.wait()
                        work = self._take_work_locked()
                    if work is None:
                        return
                    batch, index, item = work
                    started_at = perf_counter()
                    self._queue_wait_seconds += max(
                        0.0,
                        started_at - batch.enqueued_at,
                    )
                outcome: _ItemOutcome[object]
                try:
                    outcome = _ItemOutcome(value=batch.fetch(session, item))
                except BaseException as error:
                    outcome = _ItemOutcome(error=error)
                ended_at = perf_counter()
                with self._condition:
                    batch.running -= 1
                    self._active_requests -= 1
                    self._items_completed += 1
                    self._service_seconds += ended_at - started_at
                    batch.outcomes[index] = outcome
                    batch.completion_order.append(index)
                    if outcome.error is not None and batch.first_error is None:
                        batch.first_error = outcome.error
                        batch.cancelled = True
                        self._ready_batch_ids.discard(batch.batch_id)
                    self._condition.notify_all()
        finally:
            session.close()

    def snapshot(self) -> APIRuntimeMetrics:
        with self._condition:
            return APIRuntimeMetrics(
                capacity=self.capacity,
                batches_submitted=self._batches_submitted,
                items_submitted=self._items_submitted,
                items_completed=self._items_completed,
                active_requests=self._active_requests,
                peak_active_requests=self._peak_active_requests,
                queued_items=self._queued_items_locked(),
                peak_queued_items=self._peak_queued_items,
                queue_wait_seconds=self._queue_wait_seconds,
                service_seconds=self._service_seconds,
                runtime_seconds=max(0.0, perf_counter() - self._started_at),
            )

    def close(self) -> None:
        with self._condition:
            if self._closed:
                return
            self._closing = True
            for batch in self._batches.values():
                batch.cancelled = True
            self._condition.notify_all()
        for worker in self._workers:
            worker.join()
        with self._condition:
            self._closed = True
            self._condition.notify_all()


_runtime_lock = threading.RLock()
_active_runtime: FairAPIRuntime | None = None
_runtime_scope_depth = 0
_last_metrics: APIRuntimeMetrics | None = None


def current_api_runtime() -> FairAPIRuntime | None:
    with _runtime_lock:
        return _active_runtime


def active_api_runtime_capacity() -> int | None:
    runtime = current_api_runtime()
    return None if runtime is None else runtime.capacity


def api_runtime_metrics() -> APIRuntimeMetrics | None:
    with _runtime_lock:
        if _active_runtime is not None:
            return _active_runtime.snapshot()
        return _last_metrics


@contextmanager
def use_api_runtime(capacity: int) -> Generator[FairAPIRuntime]:
    global _active_runtime, _runtime_scope_depth, _last_metrics
    with _runtime_lock:
        if _active_runtime is None:
            _active_runtime = FairAPIRuntime(capacity)
        elif _active_runtime.capacity != capacity:
            raise RuntimeError(
                "NGA API运行时活动期间不能修改并发数："
                f"active={_active_runtime.capacity}, requested={capacity}"
            )
        runtime = _active_runtime
        _runtime_scope_depth += 1
    try:
        yield runtime
    finally:
        close_runtime: FairAPIRuntime | None = None
        with _runtime_lock:
            _runtime_scope_depth -= 1
            if _runtime_scope_depth == 0:
                close_runtime = _active_runtime
                _active_runtime = None
        if close_runtime is not None:
            close_runtime.close()
            with _runtime_lock:
                _last_metrics = close_runtime.snapshot()

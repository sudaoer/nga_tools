from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from time import perf_counter

from nga_tools.replay.profile import TrafficProfile

Clock = Callable[[], float]
Sleep = Callable[[float], Awaitable[None]]


class SharedBandwidthLimiter:
    """Reserve byte-transfer time on one deterministic global timeline."""

    def __init__(
        self,
        bytes_per_second: int,
        *,
        clock: Clock = perf_counter,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if bytes_per_second < 0:
            raise ValueError("bytes_per_second不能小于0。")
        self._bytes_per_second = bytes_per_second
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_available = clock()

    def reserve_wait_seconds(self, byte_count: int) -> float:
        if byte_count < 0:
            raise ValueError("byte_count不能小于0。")
        if byte_count == 0 or self._bytes_per_second == 0:
            return 0.0
        transfer_seconds = byte_count / self._bytes_per_second
        with self._lock:
            now = self._clock()
            transfer_start = max(now, self._next_available)
            transfer_end = transfer_start + transfer_seconds
            self._next_available = transfer_end
        return max(0.0, transfer_end - now)

    async def wait_for_bytes(self, byte_count: int) -> float:
        wait_seconds = self.reserve_wait_seconds(byte_count)
        if wait_seconds > 0:
            await self._sleep(wait_seconds)
        return wait_seconds

    def reset(self) -> None:
        with self._lock:
            self._next_available = self._clock()


class TrafficShaper:
    def __init__(self, profile: TrafficProfile) -> None:
        self.profile = profile
        self._semaphore = asyncio.Semaphore(profile.max_inflight)
        self._bandwidth = SharedBandwidthLimiter(
            profile.bandwidth_bytes_per_second
        )
        self._state_lock = threading.Lock()
        self._active = 0
        self._waiting = 0

    @property
    def busy_count(self) -> int:
        with self._state_lock:
            return self._active + self._waiting

    @asynccontextmanager
    async def slot(self) -> AsyncGenerator[None]:
        acquired = False
        waiting_recorded = True
        with self._state_lock:
            self._waiting += 1
        try:
            await self._semaphore.acquire()
            acquired = True
            with self._state_lock:
                self._waiting -= 1
                waiting_recorded = False
                self._active += 1
            yield
        finally:
            with self._state_lock:
                if waiting_recorded:
                    self._waiting -= 1
                if acquired:
                    self._active -= 1
            if acquired:
                self._semaphore.release()

    async def wait_latency(self) -> float:
        latency_seconds = self.profile.latency_ms / 1000
        if latency_seconds > 0:
            await asyncio.sleep(latency_seconds)
        return latency_seconds

    async def wait_for_bytes(self, byte_count: int) -> float:
        return await self._bandwidth.wait_for_bytes(byte_count)

    def reset(self) -> None:
        if self.busy_count:
            raise RuntimeError("仍有重放请求在途，不能重置限速器。")
        self._bandwidth.reset()

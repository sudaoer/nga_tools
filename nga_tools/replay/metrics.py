from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, TypeAlias

TrafficKind: TypeAlias = Literal["api", "image"]
ReplayOperation: TypeAlias = Literal[
    "author_post_list",
    "original_post_list",
    "pid_redirect",
]


@dataclass(slots=True)
class _TrafficState:
    requests: int = 0
    response_bytes: int = 0
    synthetic_original_requests: int = 0
    latency_wait_seconds: float = 0.0
    bandwidth_wait_seconds: float = 0.0
    service_seconds: float = 0.0
    active: int = 0
    max_active: int = 0
    statuses: Counter[int] = field(default_factory=Counter[int])
    operations: Counter[str] = field(default_factory=Counter[str])

    def as_dict(self) -> dict[str, object]:
        return {
            "requests": self.requests,
            "response_bytes": self.response_bytes,
            "synthetic_original_requests": self.synthetic_original_requests,
            "latency_wait_seconds": self.latency_wait_seconds,
            "bandwidth_wait_seconds": self.bandwidth_wait_seconds,
            "service_seconds": self.service_seconds,
            "active": self.active,
            "max_active": self.max_active,
            "statuses": {
                str(status): count for status, count in sorted(self.statuses.items())
            },
            "operations": dict(sorted(self.operations.items())),
        }


class ReplayMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._api = _TrafficState()
        self._image = _TrafficState()
        self._reset_at = datetime.now().astimezone().isoformat()

    def _state(self, kind: TrafficKind) -> _TrafficState:
        return self._api if kind == "api" else self._image

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._api.active + self._image.active

    def begin(
        self,
        kind: TrafficKind,
        *,
        synthetic_original: bool,
        operation: ReplayOperation | None,
    ) -> None:
        with self._lock:
            state = self._state(kind)
            state.requests += 1
            state.active += 1
            state.max_active = max(state.max_active, state.active)
            if synthetic_original:
                state.synthetic_original_requests += 1
            if operation is not None:
                state.operations[operation] += 1

    def finish(
        self,
        kind: TrafficKind,
        *,
        status: int,
        response_bytes: int,
        latency_wait_seconds: float,
        bandwidth_wait_seconds: float,
        service_seconds: float,
    ) -> None:
        with self._lock:
            state = self._state(kind)
            state.response_bytes += response_bytes
            state.latency_wait_seconds += latency_wait_seconds
            state.bandwidth_wait_seconds += bandwidth_wait_seconds
            state.service_seconds += service_seconds
            state.statuses[status] += 1
            state.active -= 1
            if state.active < 0:
                raise RuntimeError("重放指标active计数无效。")

    def reset(self) -> str:
        with self._lock:
            if self._api.active or self._image.active:
                raise RuntimeError("仍有重放请求在途，不能重置指标。")
            self._api = _TrafficState()
            self._image = _TrafficState()
            self._reset_at = datetime.now().astimezone().isoformat()
            return self._reset_at

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "reset_at": self._reset_at,
                "api": self._api.as_dict(),
                "image": self._image.as_dict(),
            }

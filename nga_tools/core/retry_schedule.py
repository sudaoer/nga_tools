from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from nga_tools.core.hashing import hash_object


RetryDecisionReason = Literal[
    "missing_timestamp",
    "probability",
    "deadline",
    "waiting",
]


@dataclass(frozen=True)
class RetryScheduleDecision:
    should_run: bool
    cumulative_probability: float
    ticket: float | None
    reason: RetryDecisionReason


def cubic_retry_probability(
    elapsed: timedelta,
    max_interval: timedelta,
) -> float:
    if max_interval <= timedelta(0):
        raise ValueError("概率调度最大间隔必须大于0。")
    elapsed_seconds = max(elapsed.total_seconds(), 0.0)
    progress = min(elapsed_seconds / max_interval.total_seconds(), 1.0)
    return progress**3


def _require_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}必须包含时区。")


def _stable_ticket(
    *,
    namespace: str,
    target_key: str,
    last_event_at: datetime,
) -> float:
    if not namespace:
        raise ValueError("概率调度命名空间不能为空。")
    if not target_key:
        raise ValueError("概率调度目标标识不能为空。")
    canonical_time = last_event_at.astimezone(timezone.utc).isoformat(
        timespec="microseconds"
    )
    digest = hash_object((namespace, target_key, canonical_time))
    return int(digest[:16], 16) / float(1 << 64)


def retry_schedule_decision(
    *,
    namespace: str,
    target_key: str,
    last_event_at: datetime | None,
    now: datetime,
    max_interval: timedelta,
) -> RetryScheduleDecision:
    _require_aware_datetime(now, "概率调度当前时间")
    if max_interval <= timedelta(0):
        raise ValueError("概率调度最大间隔必须大于0。")
    if not namespace:
        raise ValueError("概率调度命名空间不能为空。")
    if not target_key:
        raise ValueError("概率调度目标标识不能为空。")
    if last_event_at is None:
        return RetryScheduleDecision(
            should_run=True,
            cumulative_probability=1.0,
            ticket=None,
            reason="missing_timestamp",
        )

    _require_aware_datetime(last_event_at, "概率调度上次事件时间")
    elapsed = now - last_event_at
    probability = cubic_retry_probability(elapsed, max_interval)
    ticket = _stable_ticket(
        namespace=namespace,
        target_key=target_key,
        last_event_at=last_event_at,
    )
    if elapsed >= max_interval:
        return RetryScheduleDecision(
            should_run=True,
            cumulative_probability=1.0,
            ticket=ticket,
            reason="deadline",
        )
    if ticket < probability:
        return RetryScheduleDecision(
            should_run=True,
            cumulative_probability=probability,
            ticket=ticket,
            reason="probability",
        )
    return RetryScheduleDecision(
        should_run=False,
        cumulative_probability=probability,
        ticket=ticket,
        reason="waiting",
    )

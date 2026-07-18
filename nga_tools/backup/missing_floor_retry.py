from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from nga_tools.backup.archive_posts import PostDate
from nga_tools.backup.processing_state import PendingMissingFloorRetry
from nga_tools.core.retry_schedule import (
    retry_schedule_decision_for_ticket,
    stable_retry_ticket,
)


_MISSING_FLOOR_RETRY_NAMESPACE = "backup-missing-floor-retry"


@dataclass(frozen=True)
class MissingFloorGap:
    author_lous: tuple[int, ...]
    estimated_missing_at: datetime
    last_attempt_at: datetime | None


@dataclass(frozen=True)
class MissingFloorRetrySelection:
    gaps: tuple[MissingFloorGap, ...]
    due_gaps: tuple[MissingFloorGap, ...]
    deferred_gaps: tuple[MissingFloorGap, ...]

    @property
    def due_lous(self) -> tuple[int, ...]:
        return tuple(lou for gap in self.due_gaps for lou in gap.author_lous)

    @property
    def deferred_lous(self) -> tuple[int, ...]:
        return tuple(lou for gap in self.deferred_gaps for lou in gap.author_lous)


def consecutive_missing_floor_groups(
    missing_lous: Sequence[int],
) -> tuple[tuple[int, ...], ...]:
    ordered = sorted(set(missing_lous))
    if not ordered:
        return ()

    groups: list[tuple[int, ...]] = []
    current = [ordered[0]]
    for lou in ordered[1:]:
        if lou == current[-1] + 1:
            current.append(lou)
            continue
        groups.append(tuple(current))
        current = [lou]
    groups.append(tuple(current))
    return tuple(groups)


def _require_aware_datetime(value: datetime, label: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label}必须包含时区。")


def _postdate_datetime(value: PostDate | None, *, now: datetime) -> datetime:
    if type(value) is int:
        try:
            parsed = datetime.fromtimestamp(value, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return now
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return now
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            local_timezone = now.astimezone().tzinfo
            if local_timezone is None:
                return now
            parsed = parsed.replace(tzinfo=local_timezone)
    else:
        return now
    return min(parsed, now)


def build_missing_floor_gaps(
    missing_lous: Sequence[int],
    *,
    next_postdates_by_gap_end: Mapping[int, PostDate | None],
    retries: Sequence[PendingMissingFloorRetry],
    now: datetime,
) -> tuple[MissingFloorGap, ...]:
    _require_aware_datetime(now, "缺失楼调度当前时间")
    retries_by_lou = {retry.author_lou: retry for retry in retries}
    groups = consecutive_missing_floor_groups(missing_lous)
    gaps: list[MissingFloorGap] = []
    for group in groups:
        retry_times: list[datetime] = []
        complete_history = True
        for lou in group:
            retry = retries_by_lou.get(lou)
            if retry is None:
                complete_history = False
                break
            _require_aware_datetime(retry.last_attempt_at, "缺失楼上次重试时间")
            retry_times.append(retry.last_attempt_at)
        gaps.append(
            MissingFloorGap(
                author_lous=group,
                estimated_missing_at=_postdate_datetime(
                    next_postdates_by_gap_end.get(group[-1]),
                    now=now,
                ),
                last_attempt_at=min(retry_times) if complete_history else None,
            )
        )
    return tuple(gaps)


def select_missing_floor_retries(
    missing_lous: Sequence[int],
    *,
    next_postdates_by_gap_end: Mapping[int, PostDate | None],
    retries: Sequence[PendingMissingFloorRetry],
    thread_target_key: str,
    now: datetime,
    immediate_window: timedelta,
    max_interval: timedelta,
    force: bool,
    shared_ticket: float | None = None,
) -> MissingFloorRetrySelection:
    _require_aware_datetime(now, "缺失楼调度当前时间")
    if immediate_window <= timedelta(0):
        raise ValueError("缺失楼积极重试窗口必须大于0。")
    if max_interval <= timedelta(0):
        raise ValueError("缺失楼最长重试间隔必须大于0。")
    gaps = build_missing_floor_gaps(
        missing_lous,
        next_postdates_by_gap_end=next_postdates_by_gap_end,
        retries=retries,
        now=now,
    )
    if not gaps:
        return MissingFloorRetrySelection((), (), ())

    ticket = (
        stable_retry_ticket(
            namespace=_MISSING_FLOOR_RETRY_NAMESPACE,
            target_key=thread_target_key,
        )
        if shared_ticket is None
        else shared_ticket
    )
    due: list[MissingFloorGap] = []
    deferred: list[MissingFloorGap] = []
    for gap in gaps:
        age = max(now - gap.estimated_missing_at, timedelta(0))
        if force or age <= immediate_window:
            due.append(gap)
            continue
        age_based_interval = min(age - immediate_window, max_interval)
        decision = retry_schedule_decision_for_ticket(
            ticket=ticket,
            last_event_at=gap.last_attempt_at,
            now=now,
            max_interval=age_based_interval,
        )
        if decision.should_run:
            due.append(gap)
        else:
            deferred.append(gap)
    return MissingFloorRetrySelection(gaps, tuple(due), tuple(deferred))


def pending_missing_floor_retries_after_attempt(
    retries: Sequence[PendingMissingFloorRetry],
    *,
    unresolved_lous: Sequence[int],
    attempted_lous: Sequence[int],
    attempted_at: datetime,
) -> tuple[PendingMissingFloorRetry, ...]:
    _require_aware_datetime(attempted_at, "缺失楼重试时间")
    unresolved = set(unresolved_lous)
    attempted = set(attempted_lous)
    retries_by_lou = {
        retry.author_lou: retry
        for retry in retries
        if retry.author_lou in unresolved and retry.author_lou not in attempted
    }
    for lou in attempted & unresolved:
        retries_by_lou[lou] = PendingMissingFloorRetry(lou, attempted_at)
    return tuple(retries_by_lou[lou] for lou in sorted(retries_by_lou))

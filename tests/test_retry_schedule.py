from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nga_tools.core.retry_schedule import (
    cubic_retry_probability,
    retry_schedule_decision,
    retry_schedule_decision_for_ticket,
    stable_retry_ticket,
)


def test_cubic_retry_probability_clamps_elapsed_time() -> None:
    interval = timedelta(hours=8)

    assert cubic_retry_probability(timedelta(hours=-1), interval) == 0.0
    assert cubic_retry_probability(timedelta(hours=4), interval) == 0.125
    assert cubic_retry_probability(timedelta(hours=6), interval) == 0.421875
    assert cubic_retry_probability(timedelta(hours=8), interval) == 1.0
    assert cubic_retry_probability(timedelta(hours=12), interval) == 1.0


def test_missing_timestamp_runs_immediately() -> None:
    decision = retry_schedule_decision(
        namespace="image-retry",
        target_key="123:456:https://example.invalid/image.png",
        last_event_at=None,
        now=datetime(2026, 7, 14, tzinfo=timezone.utc),
        max_interval=timedelta(hours=168),
    )

    assert decision.should_run
    assert decision.cumulative_probability == 1.0
    assert decision.ticket is None
    assert decision.reason == "missing_timestamp"


def test_ticket_is_stable_and_decision_is_monotonic() -> None:
    last_event_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    interval = timedelta(days=8)
    decisions = [
        retry_schedule_decision(
            namespace="image-retry",
            target_key="123:456:https://example.invalid/image.png",
            last_event_at=last_event_at,
            now=last_event_at + timedelta(days=day),
            max_interval=interval,
        )
        for day in range(9)
    ]

    assert len({decision.ticket for decision in decisions}) == 1
    assert [decision.cumulative_probability for decision in decisions] == [
        (day / 8) ** 3 for day in range(8)
    ] + [1.0]
    first_run_index = next(
        index for index, decision in enumerate(decisions) if decision.should_run
    )
    assert all(
        decision.should_run for decision in decisions[first_run_index:]
    )


def test_shared_ticket_is_stable_without_an_event_timestamp() -> None:
    first = stable_retry_ticket(
        namespace="image-retry",
        target_key="123:456",
    )
    second = stable_retry_ticket(
        namespace="image-retry",
        target_key="123:456",
    )

    assert first == second
    assert 0.0 <= first < 1.0


def test_shared_ticket_preserves_each_retry_probability_and_deadline() -> None:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    ticket = 0.1

    older = retry_schedule_decision_for_ticket(
        ticket=ticket,
        last_event_at=now - timedelta(days=4),
        now=now,
        max_interval=timedelta(days=7),
    )
    newer = retry_schedule_decision_for_ticket(
        ticket=ticket,
        last_event_at=now - timedelta(days=2),
        now=now,
        max_interval=timedelta(days=7),
    )
    deadline = retry_schedule_decision_for_ticket(
        ticket=ticket,
        last_event_at=now - timedelta(days=7),
        now=now,
        max_interval=timedelta(days=7),
    )

    assert older.should_run
    assert not newer.should_run
    assert deadline.reason == "deadline"


def test_deadline_always_runs_independently_of_ticket() -> None:
    last_event_at = datetime(2026, 7, 1, tzinfo=timezone.utc)

    decision = retry_schedule_decision(
        namespace="ankebak-full",
        target_key="123:456",
        last_event_at=last_event_at,
        now=last_event_at + timedelta(hours=168),
        max_interval=timedelta(hours=168),
    )

    assert decision.should_run
    assert decision.cumulative_probability == 1.0
    assert decision.reason == "deadline"


def test_ticket_normalizes_equivalent_timezones() -> None:
    utc_time = datetime(2026, 7, 1, tzinfo=timezone.utc)
    china_time = utc_time.astimezone(timezone(timedelta(hours=8)))
    now = datetime(2026, 7, 4, tzinfo=timezone.utc)

    utc_decision = retry_schedule_decision(
        namespace="image-retry",
        target_key="123:456:url",
        last_event_at=utc_time,
        now=now,
        max_interval=timedelta(days=7),
    )
    china_decision = retry_schedule_decision(
        namespace="image-retry",
        target_key="123:456:url",
        last_event_at=china_time,
        now=now,
        max_interval=timedelta(days=7),
    )

    assert china_decision == utc_decision


@pytest.mark.parametrize(
    ("namespace", "target_key"),
    [("", "target"), ("namespace", "")],
)
def test_rejects_empty_identity(namespace: str, target_key: str) -> None:
    with pytest.raises(ValueError):
        retry_schedule_decision(
            namespace=namespace,
            target_key=target_key,
            last_event_at=None,
            now=datetime(2026, 7, 14, tzinfo=timezone.utc),
            max_interval=timedelta(days=7),
        )


def test_rejects_naive_times_and_nonpositive_interval() -> None:
    aware_now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="当前时间"):
        retry_schedule_decision(
            namespace="retry",
            target_key="target",
            last_event_at=None,
            now=datetime(2026, 7, 14),
            max_interval=timedelta(days=7),
        )
    with pytest.raises(ValueError, match="上次事件时间"):
        retry_schedule_decision(
            namespace="retry",
            target_key="target",
            last_event_at=datetime(2026, 7, 1),
            now=aware_now,
            max_interval=timedelta(days=7),
        )
    with pytest.raises(ValueError, match="最大间隔"):
        retry_schedule_decision(
            namespace="retry",
            target_key="target",
            last_event_at=None,
            now=aware_now,
            max_interval=timedelta(0),
        )

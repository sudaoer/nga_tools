from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.missing_floor_retry import (
    build_missing_floor_gaps,
    consecutive_missing_floor_groups,
    pending_missing_floor_retries_after_attempt,
    select_missing_floor_retries,
)
from nga_tools.backup.processing_state import PendingMissingFloorRetry


_NOW = datetime(2026, 7, 31, tzinfo=timezone.utc)
_IMMEDIATE_WINDOW = timedelta(days=7)
_MAX_INTERVAL = timedelta(days=30)


def _retry(lou: int, *, days_ago: int) -> PendingMissingFloorRetry:
    return PendingMissingFloorRetry(lou, _NOW - timedelta(days=days_ago))


def test_groups_consecutive_missing_lous() -> None:
    assert consecutive_missing_floor_groups([8, 2, 3, 7, 3, 10]) == (
        (2, 3),
        (7, 8),
        (10,),
    )


def test_gap_uses_next_postdate_and_tail_uses_now() -> None:
    gaps = build_missing_floor_gaps(
        [2, 3, 8],
        next_postdates_by_gap_end={
            3: int((_NOW - timedelta(days=5)).timestamp()),
            8: None,
        },
        retries=(_retry(2, days_ago=1), _retry(3, days_ago=2)),
        now=_NOW,
    )

    assert gaps[0].author_lous == (2, 3)
    assert gaps[0].estimated_missing_at == _NOW - timedelta(days=5)
    assert gaps[0].last_attempt_at == _NOW - timedelta(days=2)
    assert gaps[1].author_lous == (8,)
    assert gaps[1].estimated_missing_at == _NOW
    assert gaps[1].last_attempt_at is None


def test_recent_and_new_missing_floor_groups_are_due() -> None:
    selection = select_missing_floor_retries(
        [2, 8],
        next_postdates_by_gap_end={
            2: int((_NOW - timedelta(days=3)).timestamp()),
            8: int((_NOW - timedelta(days=300)).timestamp()),
        },
        retries=(_retry(2, days_ago=0),),
        thread_target_key="123:456",
        now=_NOW,
        immediate_window=_IMMEDIATE_WINDOW,
        max_interval=_MAX_INTERVAL,
        force=False,
        shared_ticket=0.999,
    )

    assert selection.due_lous == (2, 8)
    assert selection.deferred_lous == ()


def test_tail_missing_floor_remains_due_after_previous_attempt() -> None:
    selection = select_missing_floor_retries(
        [8],
        next_postdates_by_gap_end={8: None},
        retries=(_retry(8, days_ago=0),),
        thread_target_key="123:456",
        now=_NOW,
        immediate_window=_IMMEDIATE_WINDOW,
        max_interval=_MAX_INTERVAL,
        force=False,
        shared_ticket=0.999,
    )

    assert selection.due_lous == (8,)


def test_retry_interval_grows_with_age_and_stops_at_cap() -> None:
    selection = select_missing_floor_retries(
        [2, 8, 14],
        next_postdates_by_gap_end={
            2: int((_NOW - timedelta(days=8)).timestamp()),
            8: int((_NOW - timedelta(days=37)).timestamp()),
            14: int((_NOW - timedelta(days=400)).timestamp()),
        },
        retries=(
            _retry(2, days_ago=2),
            _retry(8, days_ago=2),
            _retry(14, days_ago=30),
        ),
        thread_target_key="123:456",
        now=_NOW,
        immediate_window=_IMMEDIATE_WINDOW,
        max_interval=_MAX_INTERVAL,
        force=False,
        shared_ticket=0.5,
    )

    assert selection.due_lous == (2, 14)
    assert selection.deferred_lous == (8,)


def test_many_gaps_share_one_ticket_without_independent_draws() -> None:
    with patch(
        "nga_tools.backup.missing_floor_retry.stable_retry_ticket",
        return_value=0.9,
    ) as ticket:
        selection = select_missing_floor_retries(
            [2, 8, 14, 20],
            next_postdates_by_gap_end={
                lou: int((_NOW - timedelta(days=400)).timestamp())
                for lou in (2, 8, 14, 20)
            },
            retries=tuple(_retry(lou, days_ago=10) for lou in (2, 8, 14, 20)),
            thread_target_key="123:456",
            now=_NOW,
            immediate_window=_IMMEDIATE_WINDOW,
            max_interval=_MAX_INTERVAL,
            force=False,
        )

    ticket.assert_called_once_with(
        namespace="backup-missing-floor-retry",
        target_key="123:456",
    )
    assert selection.due_lous == ()
    assert selection.deferred_lous == (2, 8, 14, 20)


def test_attempt_result_preserves_deferred_and_prunes_recovered() -> None:
    retries = pending_missing_floor_retries_after_attempt(
        (_retry(2, days_ago=10), _retry(8, days_ago=10)),
        unresolved_lous=[2, 8, 14],
        attempted_lous=[8, 14],
        attempted_at=_NOW,
    )

    assert retries == (
        _retry(2, days_ago=10),
        PendingMissingFloorRetry(8, _NOW),
        PendingMissingFloorRetry(14, _NOW),
    )
    assert pending_missing_floor_retries_after_attempt(
        retries,
        unresolved_lous=[2],
        attempted_lous=[8, 14],
        attempted_at=_NOW,
    ) == (_retry(2, days_ago=10),)


def test_archive_state_lazily_creates_and_round_trips_missing_floor_retries(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "123_456")
    store.ensure_schema()
    store.state.ensure_schema()
    with closing(sqlite3.connect(store.state.db_path)) as connection:
        connection.execute("DROP TABLE backup_pending_missing_floors")
        connection.commit()

    reopened = ThreadArchiveStore(store.thread_folder)
    reopened.state.ensure_schema()
    expected = (_retry(2, days_ago=10), _retry(8, days_ago=5))
    reopened.state.replace_pending_missing_floor_retries(expected)

    assert (
        reopened.state.read_backup_processing_snapshot().pending_missing_floor_retries
        == expected
    )
    reopened.state.clear_backup_processing_state()
    assert (
        reopened.state.read_backup_processing_snapshot().pending_missing_floor_retries
        == ()
    )

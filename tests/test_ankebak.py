from __future__ import annotations

import io
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from rich.console import Console

from nga_tools.commands.ankebak import _jobs_for_threads, backup_auto
from nga_tools.commands.forum import DefaultForumSyncResult
from nga_tools.console import ConsoleReporter, use_reporter
from nga_tools.forum.ankebak_state import (
    AnkebakStateStore,
    ankebak_target_key,
)
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.ngaclient.client import ForumThread
from nga_tools.storage import UnsupportedStorageFormatError


def _thread_config(tid: int = 101, aid: int | None = 201) -> ThreadConfig:
    thread_config: ThreadConfig = {
        "thread_name": f"thread-{tid}",
        "tid": tid,
    }
    if aid is not None:
        thread_config["aid"] = aid
    return thread_config


def _forum_thread(
    tid: int = 101,
    aid: int = 201,
    *,
    replies: int = 100,
    lastpost: int = 2000,
) -> ForumThread:
    return {
        "tid": tid,
        "fid": 784,
        "subject": "测试帖",
        "author": "楼主",
        "authorid": aid,
        "postdate": 1000,
        "lastpost": lastpost,
        "replies": replies,
        "forumname": "测试版面",
    }


@contextmanager
def _captured_reporter() -> Iterator[io.StringIO]:
    output = io.StringIO()
    console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    with use_reporter(ConsoleReporter(console)):
        yield output


class AnkebakStateStoreTest:
    def test_default_store_read_does_not_create_database(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        with patch(
            "nga_tools.forum.ankebak_state.get_config",
            return_value=SimpleNamespace(output_dir=str(output_dir)),
        ):
            store = AnkebakStateStore()
            assert store.load_states() == {}

        assert store.db_path == output_dir / "backup_state.sqlite3"
        assert not store.db_path.exists()
        assert not (output_dir / "forum_threads.sqlite3").exists()

    def test_missing_state_table_is_readonly_until_next_write(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "backup_state.sqlite3")
        completed_at = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )
        with sqlite3.connect(store.db_path) as connection:
            connection.execute("DROP TABLE ankebak_thread_state")
            connection.commit()

        with pytest.raises(UnsupportedStorageFormatError):
            store.load_states()
        with sqlite3.connect(store.db_path) as connection:
            table_before_write = connection.execute(
                """
                SELECT 1 FROM sqlite_schema
                WHERE type = 'table' AND name = 'ankebak_thread_state'
                """
            ).fetchone()
        assert table_before_write is None

        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )
        assert ankebak_target_key(101, 201) in store.load_states()

    def test_records_forum_signature_and_full_success_timestamps(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)

        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )

        state = store.load_states()[ankebak_target_key(101, 201)]
        assert state.forum_replies == 100
        assert state.forum_lastpost == 2000
        assert state.last_backup_success_at == completed_at
        assert state.last_full_backup_success_at == completed_at
        assert state.forum_signature_matches(_forum_thread())

    def test_incremental_success_preserves_full_timestamp(self, tmp_path: Path) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        first = datetime(2026, 7, 1, tzinfo=timezone.utc)
        second = datetime(2026, 7, 2, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=first,
            full_backup=True,
        )
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(replies=101, lastpost=3000),
            completed_at=second,
            full_backup=False,
        )

        state = store.load_states()[ankebak_target_key(101, 201)]
        assert state.last_backup_success_at == second
        assert state.last_full_backup_success_at == first
        assert state.forum_replies == 101
        assert state.forum_lastpost == 3000

    def test_state_without_full_success_runs_full_immediately(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 11, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=False,
        )

        state = store.load_states()[ankebak_target_key(101, 201)]
        decision = state.full_backup_schedule_decision(completed_at, 168)

        assert decision.should_run
        assert decision.reason == "missing_timestamp"


class AnkebakJobSelectionTest:
    def test_missing_state_runs_immediate_full_backup(self, tmp_path: Path) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")

        jobs, skipped = _jobs_for_threads(
            [_thread_config()],
            (_forum_thread(),),
            store.load_states(),
            now=datetime(2026, 7, 11, tzinfo=timezone.utc),
            full_backup_interval_hours=168,
        )

        assert [job.mode for job in jobs] == ["full"]
        assert skipped == 0

    def test_unchanged_signature_skips_until_full_is_due(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )

        with patch(
            "nga_tools.commands.ankebak.backup_local_work_kind",
            return_value=None,
        ):
            jobs, skipped = _jobs_for_threads(
                [_thread_config()],
                (_forum_thread(),),
                store.load_states(),
                now=completed_at + timedelta(hours=24),
                full_backup_interval_hours=168,
            )

        assert jobs == []
        assert skipped == 1

    def test_unchanged_signature_can_run_full_before_deadline(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )

        jobs, skipped = _jobs_for_threads(
            [_thread_config()],
            (_forum_thread(),),
            store.load_states(),
            now=completed_at + timedelta(hours=120),
            full_backup_interval_hours=168,
        )

        assert [job.mode for job in jobs] == ["full"]
        assert skipped == 0

    def test_changed_signature_uses_incremental_backup(self, tmp_path: Path) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )

        jobs, skipped = _jobs_for_threads(
            [_thread_config()],
            (_forum_thread(replies=101, lastpost=3000),),
            store.load_states(),
            now=completed_at + timedelta(hours=24),
            full_backup_interval_hours=168,
        )

        assert [job.mode for job in jobs] == ["sub"]
        assert skipped == 0

    def test_changed_signature_uses_incremental_backup_for_whole_thread(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=None,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )
        fresh_thread = _forum_thread(replies=101, lastpost=3000)

        with patch(
            "nga_tools.commands.ankebak.backup_local_work_kind",
            return_value=None,
        ):
            jobs, skipped = _jobs_for_threads(
                [_thread_config(aid=None)],
                (fresh_thread,),
                store.load_states(),
                now=completed_at + timedelta(hours=24),
                full_backup_interval_hours=168,
            )

        assert [job.mode for job in jobs] == ["sub"]
        assert jobs[0].fresh_thread == fresh_thread
        assert skipped == 0

    def test_local_pending_work_runs_without_fresh_forum_thread(
        self,
        tmp_path: Path,
    ) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
        completed_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
        store.record_success(
            tid=101,
            aid=201,
            forum_thread=_forum_thread(),
            completed_at=completed_at,
            full_backup=True,
        )

        with patch(
            "nga_tools.commands.ankebak.backup_local_work_kind",
            return_value="maintenance",
        ):
            jobs, skipped = _jobs_for_threads(
                [_thread_config()],
                (),
                store.load_states(),
                now=completed_at + timedelta(hours=24),
                full_backup_interval_hours=168,
            )

        assert [job.mode for job in jobs] == ["maintenance"]
        assert skipped == 0


def test_backup_auto_isolates_planning_failure_and_omits_success_detail(
    tmp_path: Path,
) -> None:
    store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")
    completed_at = datetime.fromisoformat(
        (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(
            timespec="milliseconds"
        )
    )
    bad_config = _thread_config(tid=101, aid=201)
    good_config = _thread_config(tid=102, aid=202)
    for tid, aid in ((101, 201), (102, 202)):
        store.record_success(
            tid=tid,
            aid=aid,
            forum_thread=_forum_thread(tid=tid, aid=aid),
            completed_at=completed_at,
            full_backup=True,
        )

    def local_work_side_effect(
        tid: int,
        aid: int | None,
        *,
        now: datetime,
    ) -> str:
        del aid, now
        if tid == 101:
            raise sqlite3.DatabaseError("corrupt archive")
        return "maintenance"

    app_config = SimpleNamespace(
        api_concurrency=4,
        image_concurrency=16,
        audio_concurrency=8,
        backup_configs_workers=1,
        timing_log_enabled=False,
        ankebak_full_backup_interval_hours=168,
    )
    forum_result = DefaultForumSyncResult((), 0, 0, 0, 0, 0)
    with (
        patch(
            "nga_tools.commands.ankebak.configure_network_limits_from_args",
            return_value=app_config,
        ),
        patch(
            "nga_tools.commands.ankebak.sync_default_forum_watch",
            return_value=forum_result,
        ),
        patch("nga_tools.commands.ankebak.NGAThreadConfigs") as configs_cls,
        patch(
            "nga_tools.commands.ankebak.AnkebakStateStore",
            return_value=store,
        ),
        patch(
            "nga_tools.commands.ankebak.backup_local_work_kind",
            side_effect=local_work_side_effect,
        ),
        patch("nga_tools.commands.ankebak.maintain_thread_backup") as maintain,
        _captured_reporter() as output,
    ):
        configs_cls.return_value.get_thread_configs.return_value = [
            bad_config,
            good_config,
        ]
        with pytest.raises(SystemExit) as context:
            backup_auto({"workers": 1})

    assert context.value.code == 1
    maintain.assert_called_once_with(
        102,
        202,
        schedule_missing_floor_retries=True,
    )
    states = store.load_states()
    assert states[ankebak_target_key(101, 201)].last_backup_success_at == completed_at
    assert states[ankebak_target_key(102, 202)].last_backup_success_at > completed_at
    output_text = output.getvalue()
    assert "概率/到期完整备份0个" in output_text
    assert "本地检查失败1个" in output_text
    assert "本地维护完成：" not in output_text
    assert "批量ankebak完成：成功1个，失败1个。" in output_text

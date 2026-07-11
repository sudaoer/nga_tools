from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nga_tools.cli import args_parse
from nga_tools.commands.ankebak import _jobs_for_threads
from nga_tools.forum.ankebak_state import AnkebakStateStore, ankebak_target_key
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.ngaclient.client import ForumThread


def _thread_config(tid: int = 101, aid: int = 201) -> ThreadConfig:
    return {"thread_name": f"thread-{tid}", "tid": tid, "aid": aid}


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


class AnkebakStateStoreTest:
    def test_records_forum_signature_and_full_success(self, tmp_path: Path) -> None:
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
        assert not state.full_backup_is_due(
            completed_at + timedelta(hours=167),
            168,
        )
        assert state.full_backup_is_due(
            completed_at + timedelta(hours=168),
            168,
        )

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


class AnkebakJobSelectionTest:
    def test_missing_state_runs_immediate_full_backup(self, tmp_path: Path) -> None:
        store = AnkebakStateStore(tmp_path / "forum_threads.sqlite3")

        jobs, skipped = _jobs_for_threads(
            [_thread_config()],
            (_forum_thread(),),
            store,
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
                store,
                now=completed_at + timedelta(hours=24),
                full_backup_interval_hours=168,
            )

        assert jobs == []
        assert skipped == 1

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
            store,
            now=completed_at + timedelta(hours=24),
            full_backup_interval_hours=168,
        )

        assert [job.mode for job in jobs] == ["sub"]
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
                store,
                now=completed_at + timedelta(hours=24),
                full_backup_interval_hours=168,
            )

        assert [job.mode for job in jobs] == ["maintenance"]
        assert skipped == 0


def test_backup_auto_cli_parses_network_and_watch_options() -> None:
    args = args_parse(
        [
            "backup",
            "auto",
            "--watch-config",
            "watch.json",
            "--workers",
            "3",
            "--api-concurrency",
            "2",
            "--image-concurrency",
            "20",
        ]
    )

    assert args["watch_config"] == "watch.json"
    assert args["workers"] == 3
    assert args["api_concurrency"] == 2
    assert args["image_concurrency"] == 20

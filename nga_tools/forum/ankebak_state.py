from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nga_tools.core.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS, configure_connection
from nga_tools.forum.thread_store import forum_thread_db_path
from nga_tools.ngaclient.client import ForumThread


@dataclass(frozen=True)
class AnkebakThreadState:
    target_key: str
    tid: int
    aid: int | None
    forum_replies: int | None
    forum_lastpost: int | None
    last_backup_success_at: datetime
    last_full_backup_success_at: datetime | None

    def forum_signature_matches(self, thread: ForumThread) -> bool:
        return (
            self.forum_replies == thread["replies"]
            and self.forum_lastpost == thread["lastpost"]
        )

    def full_backup_is_due(self, now: datetime, interval_hours: int) -> bool:
        if self.last_full_backup_success_at is None:
            return True
        return now - self.last_full_backup_success_at >= timedelta(
            hours=interval_hours
        )


def ankebak_target_key(tid: int, aid: int | None) -> str:
    return f"{tid}:{'all' if aid is None else aid}"


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"ankebak状态时间无效：{value!r}")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"ankebak状态时间缺少时区：{value!r}")
    return parsed


class AnkebakStateStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = forum_thread_db_path() if db_path is None else db_path

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
        return connection

    @staticmethod
    def _ensure_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS ankebak_thread_state (
                target_key TEXT PRIMARY KEY,
                tid INTEGER NOT NULL,
                aid INTEGER,
                forum_replies INTEGER,
                forum_lastpost INTEGER,
                last_backup_success_at TEXT NOT NULL,
                last_full_backup_success_at TEXT
            )
            """
        )
        connection.commit()

    def load_states(self) -> dict[str, AnkebakThreadState]:
        with closing(self._connect()) as connection:
            self._ensure_table(connection)
            rows = connection.execute(
                """
                SELECT
                    target_key,
                    tid,
                    aid,
                    forum_replies,
                    forum_lastpost,
                    last_backup_success_at,
                    last_full_backup_success_at
                FROM ankebak_thread_state
                """
            ).fetchall()

        states: dict[str, AnkebakThreadState] = {}
        for row in rows:
            (
                target_key,
                tid,
                aid,
                forum_replies,
                forum_lastpost,
                last_backup_success_at,
                last_full_backup_success_at,
            ) = row
            if not isinstance(target_key, str) or type(tid) is not int:
                raise ValueError(f"ankebak状态主键无效：{row!r}")
            if aid is not None and type(aid) is not int:
                raise ValueError(f"ankebak状态aid无效：{row!r}")
            if forum_replies is not None and type(forum_replies) is not int:
                raise ValueError(f"ankebak状态replies无效：{row!r}")
            if forum_lastpost is not None and type(forum_lastpost) is not int:
                raise ValueError(f"ankebak状态lastpost无效：{row!r}")
            states[target_key] = AnkebakThreadState(
                target_key=target_key,
                tid=tid,
                aid=aid,
                forum_replies=forum_replies,
                forum_lastpost=forum_lastpost,
                last_backup_success_at=_parse_timestamp(last_backup_success_at),
                last_full_backup_success_at=(
                    None
                    if last_full_backup_success_at is None
                    else _parse_timestamp(last_full_backup_success_at)
                ),
            )
        return states

    def record_success(
        self,
        *,
        tid: int,
        aid: int | None,
        forum_thread: ForumThread | None,
        completed_at: datetime,
        full_backup: bool,
    ) -> None:
        if completed_at.tzinfo is None:
            raise ValueError("ankebak成功时间必须包含时区。")
        target_key = ankebak_target_key(tid, aid)
        completed_text = completed_at.isoformat(timespec="milliseconds")
        replies = None if forum_thread is None else forum_thread["replies"]
        lastpost = None if forum_thread is None else forum_thread["lastpost"]
        full_completed_text = completed_text if full_backup else None

        with closing(self._connect()) as connection:
            self._ensure_table(connection)
            with connection:
                connection.execute(
                    """
                    INSERT INTO ankebak_thread_state (
                        target_key,
                        tid,
                        aid,
                        forum_replies,
                        forum_lastpost,
                        last_backup_success_at,
                        last_full_backup_success_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(target_key) DO UPDATE SET
                        tid = excluded.tid,
                        aid = excluded.aid,
                        forum_replies = COALESCE(
                            excluded.forum_replies,
                            ankebak_thread_state.forum_replies
                        ),
                        forum_lastpost = COALESCE(
                            excluded.forum_lastpost,
                            ankebak_thread_state.forum_lastpost
                        ),
                        last_backup_success_at = excluded.last_backup_success_at,
                        last_full_backup_success_at = COALESCE(
                            excluded.last_full_backup_success_at,
                            ankebak_thread_state.last_full_backup_success_at
                        )
                    """,
                    (
                        target_key,
                        tid,
                        aid,
                        replies,
                        lastpost,
                        completed_text,
                        full_completed_text,
                    ),
                )

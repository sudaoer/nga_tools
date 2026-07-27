from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from nga_tools.core.retry_schedule import (
    RetryScheduleDecision,
    retry_schedule_decision,
)
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
)
from nga_tools.config import get_config
from nga_tools.ngaclient.client import ForumThread
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import require_exact_columns, require_table_names


BACKUP_STATE_DB_FILENAME = "backup_state.sqlite3"
_ANKEBAK_STATE_COLUMNS = (
    ("target_key", "TEXT"), ("tid", "INTEGER"), ("aid", "INTEGER"),
    ("forum_replies", "INTEGER"), ("forum_lastpost", "INTEGER"),
    ("last_backup_success_at", "TEXT"),
    ("last_full_backup_success_at", "TEXT"),
)


def require_current_backup_state_schema(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    source = f"backup_state {db_path}"
    require_storage_metadata(connection, role="backup_state")
    require_table_names(
        connection,
        expected={"storage_metadata", "ankebak_thread_state"},
        source=source,
    )
    require_exact_columns(
        connection,
        "ankebak_thread_state",
        _ANKEBAK_STATE_COLUMNS,
        source=source,
    )


def backup_state_db_path() -> Path:
    return Path(get_config().output_dir) / BACKUP_STATE_DB_FILENAME


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

    def full_backup_schedule_decision(
        self,
        now: datetime,
        interval_hours: int,
    ) -> RetryScheduleDecision:
        return retry_schedule_decision(
            namespace="ankebak-full-backup",
            target_key=self.target_key,
            last_event_at=self.last_full_backup_success_at,
            now=now,
            max_interval=timedelta(hours=interval_hours),
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
        self.db_path = backup_state_db_path() if db_path is None else db_path

    def _open_write_connection(self) -> sqlite3.Connection:
        new_database = not self.db_path.is_file()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
        try:
            with connection:
                if new_database:
                    ensure_storage_metadata(connection, role="backup_state")
                else:
                    require_storage_metadata(connection, role="backup_state")
                self._ensure_table(connection)
                require_current_backup_state_schema(connection, self.db_path)
        except BaseException:
            connection.close()
            raise
        return connection

    def _open_read_connection(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise FileNotFoundError(self.db_path)
        connection = sqlite3.connect(
            f"{self.db_path.resolve().as_uri()}?mode=ro",
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
        try:
            require_current_backup_state_schema(connection, self.db_path)
        except BaseException:
            connection.close()
            raise
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
        require_exact_columns(
            connection,
            "ankebak_thread_state",
            _ANKEBAK_STATE_COLUMNS,
            source="backup_state SQLite",
        )

    def load_states(self) -> dict[str, AnkebakThreadState]:
        if not self.db_path.is_file():
            return {}
        with closing(self._open_read_connection()) as connection:
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

        with closing(self._open_write_connection()) as connection:
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

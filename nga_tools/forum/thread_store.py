from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nga_tools.config import get_config
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
)
from nga_tools.ngaclient.client import ForumThread
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import require_exact_columns, require_table_names

FORUM_THREAD_DB_FILENAME = "forum_threads.sqlite3"
_FORUM_THREAD_COLUMNS = (
    ("tid", "INTEGER"), ("aid", "INTEGER"), ("author", "TEXT"),
    ("subject", "TEXT"), ("postdate", "INTEGER"),
    ("postdate_text", "TEXT"), ("lastpost", "INTEGER"),
    ("lastpost_text", "TEXT"), ("replies", "INTEGER"),
    ("topic_type", "INTEGER"), ("is_forum", "INTEGER"),
)


def require_current_forum_schema(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    source = f"forum_data {db_path}"
    require_storage_metadata(connection, role="forum_data")
    require_table_names(
        connection,
        expected={"storage_metadata"},
        allowed_prefixes=("forum_threads_fid_",),
        source=source,
    )
    rows = connection.execute(
        """
        SELECT name FROM sqlite_schema
        WHERE type = 'table' AND name LIKE 'forum_threads_fid_%'
        """
    ).fetchall()
    for row in rows:
        if isinstance(row[0], str):
            require_exact_columns(
                connection,
                row[0],
                _FORUM_THREAD_COLUMNS,
                source=source,
            )


@dataclass(frozen=True)
class ForumThreadUpsertResult:
    inserted_count: int
    updated_count: int

    @property
    def total_count(self) -> int:
        return self.inserted_count + self.updated_count


def forum_thread_db_path() -> Path:
    return Path(get_config().output_dir) / FORUM_THREAD_DB_FILENAME


def forum_thread_table_name(fid: int) -> str:
    if fid <= 0:
        raise ValueError("fid必须大于0。")
    return f"forum_threads_fid_{fid}"


def timestamp_text(timestamp: int) -> str:
    return datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _thread_row(
    thread: ForumThread,
) -> tuple[int, int, str, str, int, str, int, str, int, int, int]:
    return (
        thread["tid"],
        thread["authorid"],
        thread["author"],
        thread["subject"],
        thread["postdate"],
        timestamp_text(thread["postdate"]),
        thread["lastpost"],
        timestamp_text(thread["lastpost"]),
        thread["replies"],
        thread["topic_type"],
        1 if thread["is_forum"] else 0,
    )


class ForumThreadStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = forum_thread_db_path() if db_path is None else db_path

    def _open_write_connection(self) -> sqlite3.Connection:
        new_database = not self.db_path.is_file()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        configure_connection(connection)
        try:
            with connection:
                if new_database:
                    ensure_storage_metadata(connection, role="forum_data")
                else:
                    require_storage_metadata(connection, role="forum_data")
                require_current_forum_schema(connection, self.db_path)
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
            require_current_forum_schema(connection, self.db_path)
        except BaseException:
            connection.close()
            raise
        return connection

    def _ensure_table(self, connection: sqlite3.Connection, fid: int) -> str:
        table_name = forum_thread_table_name(fid)
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                tid INTEGER PRIMARY KEY,
                aid INTEGER NOT NULL,
                author TEXT NOT NULL,
                subject TEXT NOT NULL,
                postdate INTEGER NOT NULL,
                postdate_text TEXT NOT NULL,
                lastpost INTEGER NOT NULL,
                lastpost_text TEXT NOT NULL,
                replies INTEGER NOT NULL,
                topic_type INTEGER NOT NULL DEFAULT 0,
                is_forum INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        require_exact_columns(
            connection,
            table_name,
            _FORUM_THREAD_COLUMNS,
            source=f"forum_data {self.db_path}",
        )
        return table_name

    @staticmethod
    def _existing_table_name(
        connection: sqlite3.Connection,
        fid: int,
    ) -> str | None:
        table_name = forum_thread_table_name(fid)
        row = connection.execute(
            """
            SELECT 1 FROM sqlite_schema
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        return table_name if row is not None else None

    def existing_tids(self, fid: int) -> set[int]:
        forum_thread_table_name(fid)
        if not self.db_path.is_file():
            return set()
        with closing(self._open_read_connection()) as connection:
            table_name = self._existing_table_name(connection, fid)
            if table_name is None:
                return set()
            rows = connection.execute(f"SELECT tid FROM {table_name}").fetchall()

        tids: set[int] = set()
        for row in rows:
            tid = row[0]
            if isinstance(tid, int):
                tids.add(tid)
        return tids

    def max_normal_lastpost(self, fid: int) -> int | None:
        forum_thread_table_name(fid)
        if not self.db_path.is_file():
            return None
        with closing(self._open_read_connection()) as connection:
            table_name = self._existing_table_name(connection, fid)
            if table_name is None:
                return None
            return self._max_normal_lastpost_in_connection(connection, table_name)

    def upsert_fid_pages_atomically(
        self,
        fid: int,
        page_threads: Iterable[Iterable[ForumThread]],
    ) -> list[ForumThreadUpsertResult]:
        results: list[ForumThreadUpsertResult] = []
        with closing(self._open_write_connection()) as connection:
            with connection:
                self._ensure_table(connection, fid)
                for threads in page_threads:
                    thread_list = list(threads)
                    if not thread_list:
                        results.append(ForumThreadUpsertResult(0, 0))
                        continue
                    results.append(
                        self.upsert_threads(fid, thread_list, connection=connection)
                    )
        return results

    @staticmethod
    def _max_normal_lastpost_in_connection(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> int | None:
        row = connection.execute(
            f"SELECT MAX(lastpost) FROM {table_name} WHERE is_forum = 0"
        ).fetchone()
        if row is None or row[0] is None:
            return None
        value = row[0]
        return value if isinstance(value, int) else None

    def list_threads(self, fid: int, *, forumname: str) -> list[ForumThread]:
        forum_thread_table_name(fid)
        if not self.db_path.is_file():
            return []
        with closing(self._open_read_connection()) as connection:
            table_name = self._existing_table_name(connection, fid)
            if table_name is None:
                return []
            rows = cast(
                list[tuple[int, int, str, str, int, int, int, int, int]],
                connection.execute(
                    f"""
                    SELECT tid, aid, author, subject, postdate, lastpost,
                           replies, topic_type, is_forum
                    FROM {table_name}
                    ORDER BY lastpost DESC, tid DESC
                    """
                ).fetchall(),
            )

        return [
            {
                "tid": tid,
                "fid": fid,
                "subject": subject,
                "author": author,
                "authorid": aid,
                "postdate": postdate,
                "lastpost": lastpost,
                "replies": replies,
                "forumname": forumname,
                "topic_type": topic_type,
                "is_forum": bool(is_forum),
            }
            for tid, aid, author, subject, postdate, lastpost, replies,
            topic_type, is_forum in rows
        ]

    _UPSERT_SQL = (
        """
        INSERT INTO {table_name} (
            tid,
            aid,
            author,
            subject,
            postdate,
            postdate_text,
            lastpost,
            lastpost_text,
            replies,
            topic_type,
            is_forum
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(tid) DO UPDATE SET
            aid = excluded.aid,
            author = excluded.author,
            subject = excluded.subject,
            postdate = excluded.postdate,
            postdate_text = excluded.postdate_text,
            lastpost = excluded.lastpost,
            lastpost_text = excluded.lastpost_text,
            replies = excluded.replies,
            topic_type = excluded.topic_type,
            is_forum = excluded.is_forum
        """
    )

    def upsert_threads(
        self,
        fid: int,
        threads: Iterable[ForumThread],
        *,
        connection: sqlite3.Connection | None = None,
    ) -> ForumThreadUpsertResult:
        rows_by_tid = {thread["tid"]: _thread_row(thread) for thread in threads}
        if not rows_by_tid:
            return ForumThreadUpsertResult(inserted_count=0, updated_count=0)

        rows = list(rows_by_tid.values())
        incoming_tids = set(rows_by_tid)
        if connection is not None:
            table_name = forum_thread_table_name(fid)
            existing_tids = self._existing_tids_in_connection(
                connection,
                table_name,
                incoming_tids,
            )
            connection.executemany(
                self._UPSERT_SQL.format(table_name=table_name),
                rows,
            )
            return ForumThreadUpsertResult(
                inserted_count=len(incoming_tids) - len(existing_tids),
                updated_count=len(existing_tids),
            )

        with closing(self._open_write_connection()) as own_connection:
            with own_connection:
                table_name = self._ensure_table(own_connection, fid)
                existing_tids = self._existing_tids_in_connection(
                    own_connection,
                    table_name,
                    incoming_tids,
                )
                own_connection.executemany(
                    self._UPSERT_SQL.format(table_name=table_name),
                    rows,
                )

        updated_count = len(existing_tids)
        return ForumThreadUpsertResult(
            inserted_count=len(incoming_tids) - updated_count,
            updated_count=updated_count,
        )

    @staticmethod
    def _existing_tids_in_connection(
        connection: sqlite3.Connection,
        table_name: str,
        tids: set[int],
    ) -> set[int]:
        if not tids:
            return set()

        placeholders = ", ".join("?" for _ in tids)
        rows = connection.execute(
            f"SELECT tid FROM {table_name} WHERE tid IN ({placeholders})",
            tuple(tids),
        ).fetchall()
        existing_tids: set[int] = set()
        for row in rows:
            tid = row[0]
            if isinstance(tid, int):
                existing_tids.add(tid)
        return existing_tids

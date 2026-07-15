from __future__ import annotations

import datetime
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, cast

from nga_tools.config import get_config
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
)
from nga_tools.ngaclient.client import ForumThread
from nga_tools.storage import ensure_storage_metadata, read_storage_metadata

FORUM_THREAD_DB_FILENAME = "forum_threads.sqlite3"


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


def _thread_row(thread: ForumThread) -> tuple[int, int, str, str, int, str, int, str, int]:
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
    )


class ForumThreadStore:
    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = forum_thread_db_path() if db_path is None else db_path

    def _connect(self) -> sqlite3.Connection:
        existed = self.db_path.is_file()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        configure_connection(connection)
        existing_metadata = read_storage_metadata(connection)
        if existing_metadata is None and existed:
            existing_tables = connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            ).fetchall()
            if existing_tables:
                connection.close()
                raise ValueError(
                    f"forum_threads仍是旧单库布局：{self.db_path}。"
                    "请先运行 backup migrate-layout --all。"
                )
        ensure_storage_metadata(connection, role="forum_data")
        connection.commit()
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
                replies INTEGER NOT NULL
            )
            """
        )
        connection.commit()
        return table_name

    def existing_tids(self, fid: int) -> set[int]:
        with closing(self._connect()) as connection:
            table_name = self._ensure_table(connection, fid)
            rows = connection.execute(f"SELECT tid FROM {table_name}").fetchall()

        tids: set[int] = set()
        for row in rows:
            tid = row[0]
            if isinstance(tid, int):
                tids.add(tid)
        return tids

    def list_threads(self, fid: int, *, forumname: str) -> list[ForumThread]:
        with closing(self._connect()) as connection:
            table_name = self._ensure_table(connection, fid)
            rows = cast(
                list[tuple[int, int, str, str, int, int, int]],
                connection.execute(
                    f"""
                    SELECT tid, aid, author, subject, postdate, lastpost, replies
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
            }
            for tid, aid, author, subject, postdate, lastpost, replies in rows
        ]

    def upsert_threads(
        self,
        fid: int,
        threads: Iterable[ForumThread],
    ) -> ForumThreadUpsertResult:
        rows_by_tid = {thread["tid"]: _thread_row(thread) for thread in threads}
        if not rows_by_tid:
            return ForumThreadUpsertResult(inserted_count=0, updated_count=0)

        rows = list(rows_by_tid.values())
        incoming_tids = set(rows_by_tid)
        with closing(self._connect()) as connection:
            table_name = self._ensure_table(connection, fid)
            existing_tids = self._existing_tids_in_connection(
                connection,
                table_name,
                incoming_tids,
            )
            with connection:
                connection.executemany(
                    f"""
                    INSERT INTO {table_name} (
                        tid,
                        aid,
                        author,
                        subject,
                        postdate,
                        postdate_text,
                        lastpost,
                        lastpost_text,
                        replies
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(tid) DO UPDATE SET
                        aid = excluded.aid,
                        author = excluded.author,
                        subject = excluded.subject,
                        postdate = excluded.postdate,
                        postdate_text = excluded.postdate_text,
                        lastpost = excluded.lastpost,
                        lastpost_text = excluded.lastpost_text,
                        replies = excluded.replies
                    """,
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

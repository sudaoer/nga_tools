from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from nga_tools.backup.floor_models import AuthorPostRef, PAGE_JSON_RE
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_data import post_data_from_raw, post_source_hash
from nga_tools.core.hashing import hash_object
from nga_tools.ngaclient.client import PageData
from nga_tools.word_count import WORD_COUNT_VERSION, count_post_content

ARCHIVE_DB_FILENAME = "archive.sqlite3"
_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
_LATEST_POST_RECORDS_QUERY = """
    SELECT id, lou, pid, post_json, source_hash
    FROM (
        SELECT
            id,
            lou,
            pid,
            post_json,
            source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou
                ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
        {where_lous}
    )
    WHERE row_number = 1
    ORDER BY lou
    """
_LATEST_POST_RECORD_SUMMARIES_QUERY = """
    SELECT lou, pid, source_hash
    FROM (
        SELECT
            lou,
            pid,
            source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou
                ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
    )
    WHERE row_number = 1
    ORDER BY lou
    """


@dataclass(frozen=True)
class ArchivePageUpsertResult:
    page_snapshot_inserted: bool
    post_versions_inserted: int
    post_observations: int


@dataclass(frozen=True)
class ArchiveMigrationResult:
    page_files: int
    page_snapshots_inserted: int
    post_versions_inserted: int
    post_observations: int


@dataclass(frozen=True)
class ArchiveWordCountTotals:
    total_posts: int
    body_posts: int
    chinese_chars: int
    chinese_with_punctuation: int


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _mtime_utc_iso(path: Path) -> str:
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime,
        datetime.timezone.utc,
    ).isoformat()


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"JSON备份文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON备份文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"JSON备份文件顶层必须是对象：{path}")
    return cast(dict[str, object], raw_data)


def _optional_int(data: dict[str, object], key: str) -> Optional[int]:
    value = data.get(key)
    if type(value) is int:
        return value
    return None


def _optional_str(data: dict[str, object], key: str) -> Optional[str]:
    value = data.get(key)
    if isinstance(value, str):
        return value
    return None


def _page_json_sort_key(path: Path) -> int:
    match = PAGE_JSON_RE.fullmatch(path.name)
    if match is None:
        return 0
    return int(match.group(1))


def _page_number_from_path(path: Path) -> int:
    match = PAGE_JSON_RE.fullmatch(path.name)
    if match is None:
        raise ValueError(f"不是分页JSON文件：{path}")
    return int(match.group(1))


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows if isinstance(row[1], str)}


class ThreadArchiveStore:
    def __init__(self, thread_folder: Path) -> None:
        self.thread_folder = thread_folder
        self.db_path = thread_folder / ARCHIVE_DB_FILENAME

    def exists(self) -> bool:
        return self.db_path.is_file()

    def require_exists(self) -> None:
        if not self.exists():
            raise RuntimeError(
                f"缺少archive.sqlite3：{self.db_path}。"
                "请先运行 backup migrate-store 或重新运行备份初始化。"
            )

    def _connect(self) -> sqlite3.Connection:
        self.thread_folder.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=_SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        connection.execute(f"PRAGMA busy_timeout = {_SQLITE_BUSY_TIMEOUT_MILLISECONDS}")
        self._ensure_schema(connection)
        return connection

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS page_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                page_number INTEGER NOT NULL,
                response_hash TEXT NOT NULL,
                page_json TEXT NOT NULL,
                current_page INTEGER,
                total_page INTEGER,
                vrows INTEGER,
                msg TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                UNIQUE(page_number, response_hash)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                post_hash TEXT NOT NULL,
                source_hash TEXT NOT NULL,
                post_json TEXT NOT NULL,
                content TEXT NOT NULL,
                word_count_version INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_chars INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_with_punctuation INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                UNIQUE(pid, lou, post_hash)
            )
            """
        )
        self._ensure_post_version_word_count_columns(connection)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_observations (
                page_snapshot_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                post_version_id INTEGER NOT NULL,
                PRIMARY KEY(page_snapshot_id, position),
                FOREIGN KEY(page_snapshot_id) REFERENCES page_snapshots(id),
                FOREIGN KEY(post_version_id) REFERENCES post_versions(id)
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_post_versions_latest
            ON post_versions(lou, last_seen_at, id)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_post_observations_version
            ON post_observations(post_version_id)
            """
        )
        connection.commit()

    def _ensure_post_version_word_count_columns(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        columns = _table_columns(connection, "post_versions")
        missing_columns = [
            (
                "word_count_version",
                "ALTER TABLE post_versions ADD COLUMN "
                "word_count_version INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "word_count_chinese_chars",
                "ALTER TABLE post_versions ADD COLUMN "
                "word_count_chinese_chars INTEGER NOT NULL DEFAULT 0",
            ),
            (
                "word_count_chinese_with_punctuation",
                "ALTER TABLE post_versions ADD COLUMN "
                "word_count_chinese_with_punctuation INTEGER NOT NULL DEFAULT 0",
            ),
        ]
        for column_name, alter_sql in missing_columns:
            if column_name not in columns:
                connection.execute(alter_sql)

    def _page_snapshot_id(
        self,
        connection: sqlite3.Connection,
        page_number: int,
        response_hash: str,
    ) -> Optional[int]:
        row = connection.execute(
            """
            SELECT id
            FROM page_snapshots
            WHERE page_number = ? AND response_hash = ?
            """,
            (page_number, response_hash),
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        if type(value) is int:
            return value
        raise ValueError(f"archive page_snapshots.id字段无效：{value!r}")

    def _upsert_page_snapshot(
        self,
        connection: sqlite3.Connection,
        page_number: int,
        page_data: PageData,
        observed_at: str,
        *,
        count_observation: bool,
    ) -> tuple[int, bool]:
        response_hash = hash_object(page_data)
        inserted = self._page_snapshot_id(
            connection,
            page_number,
            response_hash,
        ) is None
        seen_increment = 1 if count_observation else 0
        page_object = cast(dict[str, object], page_data)
        connection.execute(
            """
            INSERT INTO page_snapshots (
                page_number,
                response_hash,
                page_json,
                current_page,
                total_page,
                vrows,
                msg,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(page_number, response_hash) DO UPDATE SET
                page_json = excluded.page_json,
                current_page = excluded.current_page,
                total_page = excluded.total_page,
                vrows = excluded.vrows,
                msg = excluded.msg,
                first_seen_at = CASE
                    WHEN page_snapshots.first_seen_at > excluded.first_seen_at
                    THEN excluded.first_seen_at
                    ELSE page_snapshots.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN page_snapshots.last_seen_at < excluded.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE page_snapshots.last_seen_at
                END,
                seen_count = page_snapshots.seen_count + ?
            """,
            (
                page_number,
                response_hash,
                _json_text(page_data),
                _optional_int(page_object, "currentPage"),
                _optional_int(page_object, "totalPage"),
                _optional_int(page_object, "vrows"),
                _optional_str(page_object, "msg"),
                observed_at,
                observed_at,
                seen_increment,
            ),
        )
        snapshot_id = self._page_snapshot_id(connection, page_number, response_hash)
        if snapshot_id is None:
            raise RuntimeError("写入page_snapshots后无法读取snapshot id。")
        return snapshot_id, inserted

    def _post_version_id(
        self,
        connection: sqlite3.Connection,
        pid: int,
        lou: int,
        post_hash: str,
    ) -> Optional[int]:
        row = connection.execute(
            """
            SELECT id
            FROM post_versions
            WHERE pid = ? AND lou = ? AND post_hash = ?
            """,
            (pid, lou, post_hash),
        ).fetchone()
        if row is None:
            return None
        value = row[0]
        if type(value) is int:
            return value
        raise ValueError(f"archive post_versions.id字段无效：{value!r}")

    def _upsert_post_version(
        self,
        connection: sqlite3.Connection,
        raw_post: object,
        observed_at: str,
        *,
        count_observation: bool,
    ) -> tuple[int, bool]:
        post = post_data_from_raw(raw_post)
        post_hash = hash_object(raw_post)
        word_count = count_post_content(post["content"])
        inserted = (
            self._post_version_id(
                connection,
                post["pid"],
                post["lou"],
                post_hash,
            )
            is None
        )
        seen_increment = 1 if count_observation else 0
        connection.execute(
            """
            INSERT INTO post_versions (
                pid,
                lou,
                post_hash,
                source_hash,
                post_json,
                content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pid, lou, post_hash) DO UPDATE SET
                post_json = excluded.post_json,
                content = excluded.content,
                source_hash = excluded.source_hash,
                word_count_version = excluded.word_count_version,
                word_count_chinese_chars = excluded.word_count_chinese_chars,
                word_count_chinese_with_punctuation =
                    excluded.word_count_chinese_with_punctuation,
                first_seen_at = CASE
                    WHEN post_versions.first_seen_at > excluded.first_seen_at
                    THEN excluded.first_seen_at
                    ELSE post_versions.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN post_versions.last_seen_at < excluded.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE post_versions.last_seen_at
                END,
                seen_count = post_versions.seen_count + ?
            """,
            (
                post["pid"],
                post["lou"],
                post_hash,
                post_source_hash(post),
                _json_text(raw_post),
                post["content"],
                WORD_COUNT_VERSION,
                word_count.chinese_chars,
                word_count.chinese_with_punctuation,
                observed_at,
                observed_at,
                seen_increment,
            ),
        )
        version_id = self._post_version_id(
            connection,
            post["pid"],
            post["lou"],
            post_hash,
        )
        if version_id is None:
            raise RuntimeError("写入post_versions后无法读取version id。")
        return version_id, inserted

    def upsert_page(
        self,
        page_number: int,
        page_data: PageData,
        *,
        observed_at: str | None = None,
        count_observation: bool = True,
    ) -> ArchivePageUpsertResult:
        observed_at = _now_utc_iso() if observed_at is None else observed_at
        raw_posts = page_data.get("result")
        if not isinstance(raw_posts, list):
            raise ValueError("NGA响应中缺少帖子列表。")
        raw_post_items = cast(list[object], raw_posts)

        with closing(self._connect()) as connection:
            with connection:
                snapshot_id, snapshot_inserted = self._upsert_page_snapshot(
                    connection,
                    page_number,
                    page_data,
                    observed_at,
                    count_observation=count_observation,
                )
                connection.execute(
                    "DELETE FROM post_observations WHERE page_snapshot_id = ?",
                    (snapshot_id,),
                )
                post_versions_inserted = 0
                for position, raw_post in enumerate(raw_post_items):
                    post = post_data_from_raw(raw_post)
                    version_id, version_inserted = self._upsert_post_version(
                        connection,
                        raw_post,
                        observed_at,
                        count_observation=count_observation,
                    )
                    if version_inserted:
                        post_versions_inserted += 1
                    connection.execute(
                        """
                        INSERT INTO post_observations (
                            page_snapshot_id,
                            position,
                            pid,
                            lou,
                            post_version_id
                        )
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            position,
                            post["pid"],
                            post["lou"],
                            version_id,
                        ),
                    )

        return ArchivePageUpsertResult(
            page_snapshot_inserted=snapshot_inserted,
            post_versions_inserted=post_versions_inserted,
            post_observations=len(raw_post_items),
        )

    def refresh_stored_word_counts(self) -> int:
        self.require_exists()
        with closing(self._connect()) as connection:
            with connection:
                rows = cast(
                    list[tuple[int, str]],
                    connection.execute(
                        """
                        SELECT id, content
                        FROM post_versions
                        WHERE word_count_version != ?
                        """,
                        (WORD_COUNT_VERSION,),
                    ).fetchall(),
                )
                for row_id, content in rows:
                    word_count = count_post_content(content)
                    connection.execute(
                        """
                        UPDATE post_versions
                        SET
                            word_count_version = ?,
                            word_count_chinese_chars = ?,
                            word_count_chinese_with_punctuation = ?
                        WHERE id = ?
                        """,
                        (
                            WORD_COUNT_VERSION,
                            word_count.chinese_chars,
                            word_count.chinese_with_punctuation,
                            row_id,
                        ),
                    )
        return len(rows)

    def read_latest_word_count_totals(
        self,
        min_body_chars: int,
    ) -> ArchiveWordCountTotals:
        self.require_exists()
        self.refresh_stored_word_counts()

        with closing(self._connect()) as connection:
            row = cast(
                tuple[int, int, int, int],
                connection.execute(
                    """
                    WITH latest AS (
                        SELECT
                            word_count_chinese_chars,
                            word_count_chinese_with_punctuation,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    )
                    SELECT
                        COUNT(*),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_chinese_with_punctuation >= ?
                                    THEN 1
                                    ELSE 0
                                END
                            ),
                            0
                        ),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_chinese_with_punctuation >= ?
                                    THEN word_count_chinese_chars
                                    ELSE 0
                                END
                            ),
                            0
                        ),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_chinese_with_punctuation >= ?
                                    THEN word_count_chinese_with_punctuation
                                    ELSE 0
                                END
                            ),
                            0
                        )
                    FROM latest
                    WHERE row_number = 1
                    """,
                    (min_body_chars, min_body_chars, min_body_chars),
                ).fetchone(),
            )

        return ArchiveWordCountTotals(
            total_posts=row[0],
            body_posts=row[1],
            chinese_chars=row[2],
            chinese_with_punctuation=row[3],
        )

    def read_latest_post_record_summaries(self) -> list[PostRecord]:
        self.require_exists()

        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int, int, str]],
                connection.execute(_LATEST_POST_RECORD_SUMMARIES_QUERY).fetchall(),
            )

        records: list[PostRecord] = []
        for lou, pid, source_hash in rows:
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }
            )
        return records

    def read_latest_post_records(self, lous: set[int] | None = None) -> list[PostRecord]:
        self.require_exists()
        if lous is not None and not lous:
            return []

        params: tuple[int, ...] = ()
        where_lous = ""
        if lous is not None:
            sorted_lous = tuple(sorted(lous))
            placeholders = ",".join("?" for _ in sorted_lous)
            where_lous = f"WHERE lou IN ({placeholders})"
            params = sorted_lous

        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int, int, int, str, str]],
                connection.execute(
                    _LATEST_POST_RECORDS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )

        records: list[PostRecord] = []
        for version_id, lou, pid, post_json, source_hash in rows:
            raw_post: object = json.loads(post_json)
            post = post_data_from_raw(
                raw_post,
                source=f"{self.db_path} post_versions.id={version_id}",
            )
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": post,
                    "html": None,
                    "source_hash": source_hash,
                }
            )
        return records

    def read_latest_author_post_refs(self) -> list[AuthorPostRef]:
        return [
            {"pid": record["pid"], "author_lou": record["lou"]}
            for record in self.read_latest_post_record_summaries()
            if record["pid"] is not None
        ]

    def read_latest_author_total_lou_count(self) -> Optional[int]:
        self.require_exists()
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT vrows
                FROM page_snapshots
                WHERE page_number = 1
                ORDER BY last_seen_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()

        if row is None:
            return None
        value = row[0]
        if value is None:
            return None
        if type(value) is int:
            return value
        raise ValueError(f"archive vrows字段无效：{value!r}")

    def page_count(self) -> int:
        self.require_exists()
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT COUNT(DISTINCT page_number) FROM page_snapshots"
            ).fetchone()
        if row is None:
            return 0
        value = row[0]
        if type(value) is int:
            return value
        raise ValueError(f"archive page count字段无效：{value!r}")

    def read_page_numbers(self) -> set[int]:
        if not self.exists():
            return set()
        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int]],
                connection.execute(
                    "SELECT DISTINCT page_number FROM page_snapshots"
                ).fetchall(),
            )

        page_numbers: set[int] = set()
        for (page_number,) in rows:
            if type(page_number) is not int:
                raise ValueError(f"archive page_number字段无效：{page_number!r}")
            page_numbers.add(page_number)
        return page_numbers

    def migrate_json_pages(self) -> ArchiveMigrationResult:
        folder_json = self.thread_folder / "json"
        if not folder_json.exists():
            raise RuntimeError(f"缺少JSON备份目录：{folder_json}")
        if not folder_json.is_dir():
            raise RuntimeError(f"JSON备份路径不是目录：{folder_json}")

        page_paths = sorted(
            (
                path
                for path in folder_json.iterdir()
                if path.is_file() and PAGE_JSON_RE.fullmatch(path.name)
            ),
            key=_page_json_sort_key,
        )
        if not page_paths:
            raise RuntimeError(f"缺少JSON备份文件：{folder_json}/page_*.json")

        page_snapshots_inserted = 0
        post_versions_inserted = 0
        post_observations = 0
        for path in page_paths:
            page_data = cast(PageData, _read_json_object(path))
            result = self.upsert_page(
                _page_number_from_path(path),
                page_data,
                observed_at=_mtime_utc_iso(path),
                count_observation=False,
            )
            if result.page_snapshot_inserted:
                page_snapshots_inserted += 1
            post_versions_inserted += result.post_versions_inserted
            post_observations += result.post_observations

        self.refresh_stored_word_counts()
        return ArchiveMigrationResult(
            page_files=len(page_paths),
            page_snapshots_inserted=page_snapshots_inserted,
            post_versions_inserted=post_versions_inserted,
            post_observations=post_observations,
        )

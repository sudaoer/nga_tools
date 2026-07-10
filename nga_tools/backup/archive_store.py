from __future__ import annotations

import datetime
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from nga_tools.backup.floor_models import (
    PAGE_JSON_RE,
    AuthorPostRef,
    RecoveredMissingPost,
)
from nga_tools.backup.models import PostData, PostRecord
from nga_tools.backup.archive_posts import (
    image_attachments_from_json,
    metadata_from_raw_post,
)
from nga_tools.backup.post_data import post_data_from_raw, post_source_hash
from nga_tools.backup.post_version_selection import (
    PostVersionSelection,
    load_selections,
)
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.ngaclient.client import PageData
from nga_tools.word_count import WORD_COUNT_VERSION, count_post_content

ARCHIVE_DB_FILENAME = "archive.sqlite3"
_SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
_SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(_SQLITE_BUSY_TIMEOUT_SECONDS * 1000)
_LATEST_POST_RECORDS_QUERY = """
    SELECT
        latest.id,
        latest.lou,
        latest.pid,
        latest.content,
        latest.source_hash,
        post_latest_metadata.image_attachments_json
    FROM (
        SELECT
            id,
            lou,
            pid,
            content,
            source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou
                ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
        {where_lous}
    ) AS latest
    LEFT JOIN post_latest_metadata
        ON post_latest_metadata.pid = latest.pid
        AND post_latest_metadata.lou = latest.lou
    WHERE latest.row_number = 1
    ORDER BY latest.lou
    """
_LATEST_POST_RECORD_SUMMARIES_QUERY = """
    SELECT id, lou, pid, source_hash
    FROM (
        SELECT
            id,
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
_LATEST_POST_ROWS_QUERY = """
    SELECT
        latest.id,
        latest.lou,
        latest.pid,
        latest.content,
        latest.source_hash,
        post_latest_metadata.author_name,
        post_latest_metadata.author_uid,
        post_latest_metadata.postdate_json,
        post_latest_metadata.image_attachments_json
    FROM (
        SELECT
            id,
            lou,
            pid,
            content,
            source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou
                ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
        {where_lous}
    ) AS latest
    LEFT JOIN post_latest_metadata
        ON post_latest_metadata.pid = latest.pid
        AND post_latest_metadata.lou = latest.lou
    WHERE latest.row_number = 1
    ORDER BY latest.lou
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
class ArchiveEffectivePostStats:
    post_count: int
    max_lou: Optional[int]


@dataclass(frozen=True)
class ArchivePostVersionRow:
    version_id: int
    lou: int
    pid: int
    content: str
    source_hash: str
    author_name: Optional[str]
    author_uid: Optional[int]
    postdate_json: Optional[str]
    image_attachments_json: Optional[str]
    manual_selection: bool


@dataclass
class _MergedPostVersion:
    pid: int
    lou: int
    source_hash: str
    content: str
    first_seen_at: str
    last_seen_at: str
    seen_count: int
    old_ids: list[int]


@dataclass
class _LatestPostMetadata:
    pid: int
    lou: int
    author_name: Optional[str]
    author_uid: Optional[int]
    postdate_json: Optional[str]
    image_attachments_json: str
    first_seen_at: str
    last_seen_at: str
    seen_count: int
    selected_last_seen_at: str
    selected_old_id: int


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


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


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

    def ensure_schema(self) -> None:
        with closing(self._connect()):
            pass

    def _create_post_versions_table(
        self,
        connection: sqlite3.Connection,
        table_name: str = "post_versions",
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                word_count_version INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_chars INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_with_punctuation INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                UNIQUE(pid, lou, source_hash)
            )
            """
        )

    def _create_post_latest_metadata_table(
        self,
        connection: sqlite3.Connection,
        table_name: str = "post_latest_metadata",
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                author_name TEXT,
                author_uid INTEGER,
                postdate_json TEXT,
                image_attachments_json TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                PRIMARY KEY(pid, lou)
            )
            """
        )

    def _create_post_observations_table(
        self,
        connection: sqlite3.Connection,
        table_name: str = "post_observations",
    ) -> None:
        connection.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
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
        if not _table_exists(connection, "post_versions"):
            self._create_post_versions_table(connection)
        else:
            columns = _table_columns(connection, "post_versions")
            if "post_hash" in columns or "post_json" in columns:
                self._migrate_post_versions_schema(connection, columns)
            else:
                self._ensure_post_version_word_count_columns(connection)
        self._create_post_latest_metadata_table(connection)
        self._create_post_observations_table(connection)
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

    def _read_old_post_version_rows(
        self,
        connection: sqlite3.Connection,
        columns: set[str],
    ) -> list[tuple[int, int, int, str, str, str, int, Optional[str]]]:
        post_json_expression = "post_json" if "post_json" in columns else "NULL"
        rows = connection.execute(
            f"""
            SELECT
                id,
                pid,
                lou,
                content,
                first_seen_at,
                last_seen_at,
                seen_count,
                {post_json_expression}
            FROM post_versions
            ORDER BY id
            """
        ).fetchall()
        old_rows: list[tuple[int, int, int, str, str, str, int, Optional[str]]] = []
        for row in rows:
            old_id, pid, lou, content, first_seen_at, last_seen_at, seen_count, post_json = row
            if (
                type(old_id) is not int
                or type(pid) is not int
                or type(lou) is not int
                or not isinstance(content, str)
                or not isinstance(first_seen_at, str)
                or not isinstance(last_seen_at, str)
                or type(seen_count) is not int
                or (post_json is not None and not isinstance(post_json, str))
            ):
                raise ValueError(
                    f"archive post_versions旧行字段无效：{self.db_path} row={row!r}"
                )
            old_rows.append(
                (
                    old_id,
                    pid,
                    lou,
                    content,
                    first_seen_at,
                    last_seen_at,
                    seen_count,
                    post_json,
                )
            )
        return old_rows

    def _read_old_post_observation_rows(
        self,
        connection: sqlite3.Connection,
    ) -> list[tuple[int, int, int, int, int]]:
        if not _table_exists(connection, "post_observations"):
            return []
        columns = _table_columns(connection, "post_observations")
        required_columns = {
            "page_snapshot_id",
            "position",
            "pid",
            "lou",
            "post_version_id",
        }
        if not required_columns <= columns:
            return []
        rows = connection.execute(
            """
            SELECT page_snapshot_id, position, pid, lou, post_version_id
            FROM post_observations
            ORDER BY page_snapshot_id, position
            """
        ).fetchall()
        observation_rows: list[tuple[int, int, int, int, int]] = []
        for row in rows:
            page_snapshot_id, position, pid, lou, post_version_id = row
            if (
                type(page_snapshot_id) is int
                and type(position) is int
                and type(pid) is int
                and type(lou) is int
                and type(post_version_id) is int
            ):
                observation_rows.append(
                    (page_snapshot_id, position, pid, lou, post_version_id)
                )
        return observation_rows

    def _merge_old_post_version_row(
        self,
        merged_versions: dict[tuple[int, int, str], _MergedPostVersion],
        *,
        old_id: int,
        pid: int,
        lou: int,
        content: str,
        first_seen_at: str,
        last_seen_at: str,
        seen_count: int,
    ) -> None:
        source_hash = hash_text(content)
        key = (pid, lou, source_hash)
        merged = merged_versions.get(key)
        if merged is None:
            merged_versions[key] = _MergedPostVersion(
                pid=pid,
                lou=lou,
                source_hash=source_hash,
                content=content,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                seen_count=seen_count,
                old_ids=[old_id],
            )
            return

        if first_seen_at < merged.first_seen_at:
            merged.first_seen_at = first_seen_at
        if last_seen_at > merged.last_seen_at:
            merged.last_seen_at = last_seen_at
        merged.seen_count += seen_count
        merged.old_ids.append(old_id)

    def _merge_old_post_metadata_row(
        self,
        metadata_by_post: dict[tuple[int, int], _LatestPostMetadata],
        *,
        old_id: int,
        pid: int,
        lou: int,
        first_seen_at: str,
        last_seen_at: str,
        seen_count: int,
        post_json: Optional[str],
    ) -> None:
        raw_post: object = None
        if post_json is not None:
            try:
                raw_post = json.loads(post_json)
            except json.JSONDecodeError:
                raw_post = None
        metadata = metadata_from_raw_post(raw_post)
        key = (pid, lou)
        current = metadata_by_post.get(key)
        if current is None:
            metadata_by_post[key] = _LatestPostMetadata(
                pid=pid,
                lou=lou,
                author_name=metadata["author_name"],
                author_uid=metadata["author_uid"],
                postdate_json=metadata["postdate_json"],
                image_attachments_json=metadata["image_attachments_json"],
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                seen_count=seen_count,
                selected_last_seen_at=last_seen_at,
                selected_old_id=old_id,
            )
            return

        if first_seen_at < current.first_seen_at:
            current.first_seen_at = first_seen_at
        if last_seen_at > current.last_seen_at:
            current.last_seen_at = last_seen_at
        current.seen_count += seen_count
        if (last_seen_at, old_id) >= (
            current.selected_last_seen_at,
            current.selected_old_id,
        ):
            current.author_name = metadata["author_name"]
            current.author_uid = metadata["author_uid"]
            current.postdate_json = metadata["postdate_json"]
            current.image_attachments_json = metadata["image_attachments_json"]
            current.selected_last_seen_at = last_seen_at
            current.selected_old_id = old_id

    def _migrate_post_versions_schema(
        self,
        connection: sqlite3.Connection,
        columns: set[str],
    ) -> None:
        old_rows = self._read_old_post_version_rows(connection, columns)
        old_observation_rows = self._read_old_post_observation_rows(connection)

        merged_versions: dict[tuple[int, int, str], _MergedPostVersion] = {}
        metadata_by_post: dict[tuple[int, int], _LatestPostMetadata] = {}
        for old_id, pid, lou, content, first_seen_at, last_seen_at, seen_count, post_json in old_rows:
            self._merge_old_post_version_row(
                merged_versions,
                old_id=old_id,
                pid=pid,
                lou=lou,
                content=content,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                seen_count=seen_count,
            )
            self._merge_old_post_metadata_row(
                metadata_by_post,
                old_id=old_id,
                pid=pid,
                lou=lou,
                first_seen_at=first_seen_at,
                last_seen_at=last_seen_at,
                seen_count=seen_count,
                post_json=post_json,
            )

        connection.execute("DROP TABLE IF EXISTS post_observations")
        connection.execute("DROP TABLE IF EXISTS post_versions")
        connection.execute("DROP TABLE IF EXISTS post_latest_metadata")
        self._create_post_versions_table(connection)
        self._create_post_latest_metadata_table(connection)

        old_version_id_to_new_id: dict[int, int] = {}
        for merged in sorted(
            merged_versions.values(),
            key=lambda item: min(item.old_ids),
        ):
            word_count = count_post_content(merged.content)
            cursor = connection.execute(
                """
                INSERT INTO post_versions (
                    pid,
                    lou,
                    source_hash,
                    content,
                    word_count_version,
                    word_count_chinese_chars,
                    word_count_chinese_with_punctuation,
                    first_seen_at,
                    last_seen_at,
                    seen_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    merged.pid,
                    merged.lou,
                    merged.source_hash,
                    merged.content,
                    WORD_COUNT_VERSION,
                    word_count.chinese_chars,
                    word_count.chinese_with_punctuation,
                    merged.first_seen_at,
                    merged.last_seen_at,
                    merged.seen_count,
                ),
            )
            new_id = cursor.lastrowid
            if type(new_id) is not int:
                raise RuntimeError("迁移post_versions后无法读取新version id。")
            for old_id in merged.old_ids:
                old_version_id_to_new_id[old_id] = new_id

        for metadata in metadata_by_post.values():
            connection.execute(
                """
                INSERT INTO post_latest_metadata (
                    pid,
                    lou,
                    author_name,
                    author_uid,
                    postdate_json,
                    image_attachments_json,
                    first_seen_at,
                    last_seen_at,
                    seen_count
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    metadata.pid,
                    metadata.lou,
                    metadata.author_name,
                    metadata.author_uid,
                    metadata.postdate_json,
                    metadata.image_attachments_json,
                    metadata.first_seen_at,
                    metadata.last_seen_at,
                    metadata.seen_count,
                ),
            )

        self._create_post_observations_table(connection)
        for page_snapshot_id, position, pid, lou, old_post_version_id in old_observation_rows:
            new_post_version_id = old_version_id_to_new_id.get(old_post_version_id)
            if new_post_version_id is None:
                continue
            connection.execute(
                """
                INSERT OR REPLACE INTO post_observations (
                    page_snapshot_id,
                    position,
                    pid,
                    lou,
                    post_version_id
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (page_snapshot_id, position, pid, lou, new_post_version_id),
            )

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
        source_hash: str,
    ) -> Optional[int]:
        row = connection.execute(
            """
            SELECT id
            FROM post_versions
            WHERE pid = ? AND lou = ? AND source_hash = ?
            """,
            (pid, lou, source_hash),
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
        post: PostData,
        observed_at: str,
        *,
        count_observation: bool,
    ) -> tuple[int, bool]:
        source_hash = post_source_hash(post)
        word_count = count_post_content(post["content"])
        inserted = (
            self._post_version_id(
                connection,
                post["pid"],
                post["lou"],
                source_hash,
            )
            is None
        )
        seen_increment = 1 if count_observation else 0
        connection.execute(
            """
            INSERT INTO post_versions (
                pid,
                lou,
                source_hash,
                content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pid, lou, source_hash) DO UPDATE SET
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
                source_hash,
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
            source_hash,
        )
        if version_id is None:
            raise RuntimeError("写入post_versions后无法读取version id。")
        return version_id, inserted

    def _upsert_post_latest_metadata(
        self,
        connection: sqlite3.Connection,
        raw_post: object,
        post: PostData,
        observed_at: str,
        *,
        count_observation: bool,
    ) -> None:
        metadata = metadata_from_raw_post(raw_post)
        seen_increment = 1 if count_observation else 0
        connection.execute(
            """
            INSERT INTO post_latest_metadata (
                pid,
                lou,
                author_name,
                author_uid,
                postdate_json,
                image_attachments_json,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pid, lou) DO UPDATE SET
                author_name = excluded.author_name,
                author_uid = excluded.author_uid,
                postdate_json = excluded.postdate_json,
                image_attachments_json = excluded.image_attachments_json,
                first_seen_at = CASE
                    WHEN post_latest_metadata.first_seen_at > excluded.first_seen_at
                    THEN excluded.first_seen_at
                    ELSE post_latest_metadata.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN post_latest_metadata.last_seen_at < excluded.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE post_latest_metadata.last_seen_at
                END,
                seen_count = post_latest_metadata.seen_count + ?
            """,
            (
                post["pid"],
                post["lou"],
                metadata["author_name"],
                metadata["author_uid"],
                metadata["postdate_json"],
                metadata["image_attachments_json"],
                observed_at,
                observed_at,
                seen_increment,
            ),
        )

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
                        post,
                        observed_at,
                        count_observation=count_observation,
                    )
                    self._upsert_post_latest_metadata(
                        connection,
                        raw_post,
                        post,
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

    def upsert_recovered_posts(
        self,
        recovered_posts_by_author_lou: dict[int, RecoveredMissingPost],
        *,
        observed_at: str | None = None,
    ) -> int:
        if not recovered_posts_by_author_lou:
            return 0

        observed_at = _now_utc_iso() if observed_at is None else observed_at
        inserted_count = 0
        with closing(self._connect()) as connection:
            with connection:
                for author_lou, recovered in sorted(
                    recovered_posts_by_author_lou.items()
                ):
                    raw_post = dict(recovered["raw_post"])
                    raw_post["lou"] = author_lou
                    raw_post["pid"] = recovered["original_pid"]
                    raw_post["content"] = recovered["content"]
                    metadata = metadata_from_raw_post(raw_post)
                    if metadata["author_uid"] != -1:
                        raise ValueError(
                            f"恢复第{author_lou}楼时原帖不是匿名帖子。"
                        )

                    post = post_data_from_raw(
                        raw_post,
                        source=f"恢复的匿名原帖第{recovered['original_lou']}楼",
                    )
                    _version_id, inserted = self._upsert_post_version(
                        connection,
                        post,
                        observed_at,
                        count_observation=False,
                    )
                    self._upsert_post_latest_metadata(
                        connection,
                        raw_post,
                        post,
                        observed_at,
                        count_observation=False,
                    )
                    if inserted:
                        inserted_count += 1
        return inserted_count

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

    def read_latest_post_record_summaries(self) -> list[PostRecord]:
        self.require_exists()

        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int, int, int, str]],
                connection.execute(_LATEST_POST_RECORD_SUMMARIES_QUERY).fetchall(),
            )

        records: list[PostRecord] = []
        for _version_id, lou, pid, source_hash in rows:
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

    def _validated_post_version_selections(
        self,
        connection: sqlite3.Connection,
        lous: set[int] | None = None,
    ) -> dict[int, PostVersionSelection]:
        selections = load_selections(self.thread_folder)
        if lous is not None:
            selections = {
                lou: selection
                for lou, selection in selections.items()
                if lou in lous
            }
        if not selections:
            return {}

        valid_selections: dict[int, PostVersionSelection] = {}
        for lou, selection in selections.items():
            version_row = cast(
                Optional[tuple[int, str]],
                connection.execute(
                    """
                    SELECT lou, source_hash
                    FROM post_versions
                    WHERE id = ?
                    """,
                    (selection["version_id"],),
                ).fetchone(),
            )
            if version_row is None:
                continue
            version_lou, source_hash = version_row
            if version_lou != lou or source_hash != selection["source_hash"]:
                continue

            latest_row = cast(
                Optional[tuple[int]],
                connection.execute(
                    """
                    SELECT id
                    FROM post_versions
                    WHERE lou = ?
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT 1
                    """,
                    (lou,),
                ).fetchone(),
            )
            if latest_row is None or latest_row[0] == selection["version_id"]:
                continue
            valid_selections[lou] = selection
        return valid_selections

    def read_valid_post_version_selections(self) -> dict[int, PostVersionSelection]:
        self.require_exists()
        with closing(self._connect()) as connection:
            return self._validated_post_version_selections(connection)

    def read_effective_post_stats(self) -> ArchiveEffectivePostStats:
        self.require_exists()
        with closing(self._connect()) as connection:
            row = cast(
                tuple[int, Optional[int]],
                connection.execute(
                    """
                    WITH latest AS (
                        SELECT
                            lou,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    )
                    SELECT COUNT(*), MAX(lou)
                    FROM latest
                    WHERE row_number = 1
                    """
                ).fetchone(),
            )
        return ArchiveEffectivePostStats(post_count=row[0], max_lou=row[1])

    def read_effective_post_record_summaries(self) -> list[PostRecord]:
        self.require_exists()

        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int, int, int, str]],
                connection.execute(_LATEST_POST_RECORD_SUMMARIES_QUERY).fetchall(),
            )
            records_by_lou: dict[int, PostRecord] = {
                lou: {
                    "lou": lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }
                for _version_id, lou, pid, source_hash in rows
            }

            valid_selections = self._validated_post_version_selections(connection)
            for _lou, selection in valid_selections.items():
                selected_row = cast(
                    Optional[tuple[int, int, str]],
                    connection.execute(
                        """
                        SELECT lou, pid, source_hash
                        FROM post_versions
                        WHERE id = ?
                        """,
                        (selection["version_id"],),
                    ).fetchone(),
                )
                if selected_row is None:
                    continue
                selected_lou, pid, source_hash = selected_row
                records_by_lou[selected_lou] = {
                    "lou": selected_lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }

        return [records_by_lou[lou] for lou in sorted(records_by_lou)]

    def _image_attachments_json_for_version(
        self,
        connection: sqlite3.Connection,
        version_id: int,
        fallback_json: Optional[str],
    ) -> Optional[str]:
        snapshot_row = cast(
            Optional[tuple[str, int]],
            connection.execute(
                """
                SELECT page_snapshots.page_json, post_observations.position
                FROM post_observations
                JOIN page_snapshots
                    ON page_snapshots.id = post_observations.page_snapshot_id
                WHERE post_observations.post_version_id = ?
                ORDER BY page_snapshots.last_seen_at DESC, page_snapshots.id DESC
                LIMIT 1
                """,
                (version_id,),
            ).fetchone(),
        )
        if snapshot_row is None:
            return fallback_json

        page_json, position = snapshot_row
        try:
            raw_page: object = json.loads(page_json)
        except json.JSONDecodeError:
            return fallback_json
        if not isinstance(raw_page, dict):
            return fallback_json
        raw_posts = cast(dict[str, object], raw_page).get("result")
        if not isinstance(raw_posts, list):
            return fallback_json
        post_items = cast(list[object], raw_posts)
        if position < 0 or position >= len(post_items):
            return fallback_json
        metadata = metadata_from_raw_post(post_items[position])
        return metadata["image_attachments_json"]

    def _effective_post_row_from_sql_row(
        self,
        connection: sqlite3.Connection,
        row: tuple[
            int,
            int,
            int,
            str,
            str,
            Optional[str],
            Optional[int],
            Optional[str],
            Optional[str],
        ],
        *,
        manual_selection: bool,
        use_version_snapshot: bool | None = None,
    ) -> ArchivePostVersionRow:
        (
            version_id,
            lou,
            pid,
            content,
            source_hash,
            author_name,
            author_uid,
            postdate_json,
            image_attachments_json,
        ) = row
        if use_version_snapshot is None:
            use_version_snapshot = manual_selection
        if use_version_snapshot:
            image_attachments_json = self._image_attachments_json_for_version(
                connection,
                version_id,
                image_attachments_json,
            )
        return ArchivePostVersionRow(
            version_id=version_id,
            lou=lou,
            pid=pid,
            content=content,
            source_hash=source_hash,
            author_name=author_name,
            author_uid=author_uid,
            postdate_json=postdate_json,
            image_attachments_json=image_attachments_json,
            manual_selection=manual_selection,
        )

    def read_effective_post_rows(
        self,
        lous: set[int] | None = None,
    ) -> list[ArchivePostVersionRow]:
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
            latest_rows = cast(
                list[
                    tuple[
                        int,
                        int,
                        int,
                        str,
                        str,
                        Optional[str],
                        Optional[int],
                        Optional[str],
                        Optional[str],
                    ]
                ],
                connection.execute(
                    _LATEST_POST_ROWS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )
            rows_by_lou = {
                row[1]: self._effective_post_row_from_sql_row(
                    connection,
                    row,
                    manual_selection=False,
                )
                for row in latest_rows
            }

            valid_selections = self._validated_post_version_selections(
                connection,
                lous,
            )
            for lou, selection in valid_selections.items():
                selected_row = cast(
                    Optional[
                        tuple[
                            int,
                            int,
                            int,
                            str,
                            str,
                            Optional[str],
                            Optional[int],
                            Optional[str],
                            Optional[str],
                        ]
                    ],
                    connection.execute(
                        """
                        SELECT
                            post_versions.id,
                            post_versions.lou,
                            post_versions.pid,
                            post_versions.content,
                            post_versions.source_hash,
                            post_latest_metadata.author_name,
                            post_latest_metadata.author_uid,
                            post_latest_metadata.postdate_json,
                            post_latest_metadata.image_attachments_json
                        FROM post_versions
                        LEFT JOIN post_latest_metadata
                            ON post_latest_metadata.pid = post_versions.pid
                            AND post_latest_metadata.lou = post_versions.lou
                        WHERE post_versions.id = ?
                        """,
                        (selection["version_id"],),
                    ).fetchone(),
                )
                if selected_row is None:
                    continue
                rows_by_lou[lou] = self._effective_post_row_from_sql_row(
                    connection,
                    selected_row,
                    manual_selection=True,
                )

        return [rows_by_lou[lou] for lou in sorted(rows_by_lou)]

    def read_post_row_for_version(
        self,
        version_id: int,
    ) -> ArchivePostVersionRow | None:
        self.require_exists()
        with closing(self._connect()) as connection:
            row = cast(
                Optional[
                    tuple[
                        int,
                        int,
                        int,
                        str,
                        str,
                        Optional[str],
                        Optional[int],
                        Optional[str],
                        Optional[str],
                    ]
                ],
                connection.execute(
                    """
                    SELECT
                        post_versions.id,
                        post_versions.lou,
                        post_versions.pid,
                        post_versions.content,
                        post_versions.source_hash,
                        post_latest_metadata.author_name,
                        post_latest_metadata.author_uid,
                        post_latest_metadata.postdate_json,
                        post_latest_metadata.image_attachments_json
                    FROM post_versions
                    LEFT JOIN post_latest_metadata
                        ON post_latest_metadata.pid = post_versions.pid
                        AND post_latest_metadata.lou = post_versions.lou
                    WHERE post_versions.id = ?
                    """,
                    (version_id,),
                ).fetchone(),
            )
            if row is None:
                return None
            return self._effective_post_row_from_sql_row(
                connection,
                row,
                manual_selection=False,
                use_version_snapshot=True,
            )

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
                list[tuple[int, int, int, str, str, Optional[str]]],
                connection.execute(
                    _LATEST_POST_RECORDS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )

        records: list[PostRecord] = []
        for _version_id, lou, pid, content, source_hash, image_attachments_json in rows:
            image_attachments = image_attachments_from_json(image_attachments_json)
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": {
                        "lou": lou,
                        "pid": pid,
                        "content": content,
                        "image_attachments": image_attachments,
                    },
                    "html": None,
                    "source_hash": source_hash,
                }
            )
        return records

    def read_effective_post_records(
        self,
        lous: set[int] | None = None,
    ) -> list[PostRecord]:
        records: list[PostRecord] = []
        for row in self.read_effective_post_rows(lous):
            image_attachments = image_attachments_from_json(row.image_attachments_json)
            records.append(
                {
                    "lou": row.lou,
                    "pid": row.pid,
                    "post": {
                        "lou": row.lou,
                        "pid": row.pid,
                        "content": row.content,
                        "image_attachments": image_attachments,
                    },
                    "html": None,
                    "source_hash": row.source_hash,
                }
            )
        return records

    def read_latest_author_post_refs(self) -> list[AuthorPostRef]:
        self.require_exists()
        with closing(self._connect()) as connection:
            rows = cast(
                list[tuple[int, int, Optional[int]]],
                connection.execute(
                    """
                    SELECT latest.pid, latest.lou, metadata.author_uid
                    FROM (
                        SELECT
                            pid,
                            lou,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    ) AS latest
                    LEFT JOIN post_latest_metadata AS metadata
                        ON metadata.pid = latest.pid
                        AND metadata.lou = latest.lou
                    WHERE latest.row_number = 1
                    ORDER BY latest.lou
                    """
                ).fetchall(),
            )

        return [
            {"pid": pid, "author_lou": lou}
            for pid, lou, author_uid in rows
            if author_uid != -1
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

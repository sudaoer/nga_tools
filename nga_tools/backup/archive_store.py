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
    FloorMapEntry,
    RecoveredMissingPost,
    StoredFloorMap,
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
from nga_tools.backup.processing_state import (
    ArchiveChangeState,
    BackupProcessingSnapshot,
    BackupProcessingState,
)
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
)
from nga_tools.ngaclient.client import PageData
from nga_tools.word_count import WORD_COUNT_VERSION, count_post_content

ARCHIVE_DB_FILENAME = "archive.sqlite3"
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
    effective_processing_inputs_changed: bool


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


@dataclass(frozen=True)
class PostImageReferenceCacheEntry:
    cache_key: str
    source_hash: str
    extractor_version: int
    references_json: str


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
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
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

    def _create_floor_map_tables(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS floor_map_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                tid INTEGER NOT NULL,
                aid INTEGER NOT NULL,
                format_version INTEGER NOT NULL,
                generation_version INTEGER NOT NULL,
                hash_algorithm TEXT NOT NULL,
                input_signature TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS floor_map_entries (
                author_lou INTEGER PRIMARY KEY,
                pid INTEGER,
                original_lou INTEGER,
                original_pid INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS floor_map_candidates (
                author_lou INTEGER NOT NULL,
                candidate_index INTEGER NOT NULL,
                original_lou INTEGER NOT NULL,
                PRIMARY KEY(author_lou, candidate_index),
                FOREIGN KEY(author_lou) REFERENCES floor_map_entries(author_lou)
            )
            """
        )

    def _create_post_image_reference_cache_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_image_reference_cache (
                cache_key TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                extractor_version INTEGER NOT NULL,
                references_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _create_backup_processing_tables(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_change_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                archive_revision INTEGER NOT NULL CHECK(archive_revision >= 0),
                floor_map_revision INTEGER NOT NULL CHECK(floor_map_revision >= 0)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO archive_change_state (
                singleton,
                archive_revision,
                floor_map_revision
            )
            VALUES (1, 0, 0)
            ON CONFLICT(singleton) DO NOTHING
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_processing_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL,
                processed_floor_map_revision INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                author_total_lou_count INTEGER,
                post_overlays_fingerprint TEXT NOT NULL,
                post_version_selections_fingerprint TEXT NOT NULL,
                floor_map_format_version INTEGER NOT NULL,
                floor_map_generation_version INTEGER NOT NULL,
                floor_map_hash_algorithm TEXT NOT NULL,
                image_reference_extractor_version INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_pending_images (
                url TEXT PRIMARY KEY
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
        self._create_floor_map_tables(connection)
        self._create_post_image_reference_cache_table(connection)
        self._create_backup_processing_tables(connection)
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

    @staticmethod
    def _read_archive_change_state(
        connection: sqlite3.Connection,
    ) -> ArchiveChangeState:
        row = cast(
            Optional[tuple[object, object]],
            connection.execute(
                """
                SELECT archive_revision, floor_map_revision
                FROM archive_change_state
                WHERE singleton = 1
                """
            ).fetchone(),
        )
        if row is None or type(row[0]) is not int or type(row[1]) is not int:
            raise ValueError(f"archive修订状态无效：{row!r}")
        return ArchiveChangeState(
            archive_revision=row[0],
            floor_map_revision=row[1],
        )

    @staticmethod
    def _backup_processing_state_from_row(
        row: tuple[
            object,
            object,
            object,
            object,
            object,
            object,
            object,
            object,
            object,
            object,
            object,
            object,
        ],
    ) -> BackupProcessingState:
        (
            format_version,
            processed_archive_revision,
            processed_floor_map_revision,
            page_count,
            author_total_lou_count,
            post_overlays_fingerprint,
            post_version_selections_fingerprint,
            floor_map_format_version,
            floor_map_generation_version,
            floor_map_hash_algorithm,
            image_reference_extractor_version,
            completed_at,
        ) = row
        integer_values = (
            format_version,
            processed_archive_revision,
            processed_floor_map_revision,
            page_count,
            floor_map_format_version,
            floor_map_generation_version,
            image_reference_extractor_version,
        )
        if any(type(value) is not int for value in integer_values):
            raise ValueError(f"backup处理状态整数列无效：{row!r}")
        if author_total_lou_count is not None and type(author_total_lou_count) is not int:
            raise ValueError(f"backup处理状态vrows无效：{row!r}")
        string_values = (
            post_overlays_fingerprint,
            post_version_selections_fingerprint,
            floor_map_hash_algorithm,
            completed_at,
        )
        if any(not isinstance(value, str) or not value for value in string_values):
            raise ValueError(f"backup处理状态文本列无效：{row!r}")
        return BackupProcessingState(
            format_version=cast(int, format_version),
            processed_archive_revision=cast(int, processed_archive_revision),
            processed_floor_map_revision=cast(int, processed_floor_map_revision),
            page_count=cast(int, page_count),
            author_total_lou_count=author_total_lou_count,
            post_overlays_fingerprint=cast(str, post_overlays_fingerprint),
            post_version_selections_fingerprint=(
                cast(str, post_version_selections_fingerprint)
            ),
            floor_map_format_version=cast(int, floor_map_format_version),
            floor_map_generation_version=cast(int, floor_map_generation_version),
            floor_map_hash_algorithm=cast(str, floor_map_hash_algorithm),
            image_reference_extractor_version=cast(
                int,
                image_reference_extractor_version,
            ),
            completed_at=cast(str, completed_at),
        )

    def read_backup_processing_snapshot(self) -> BackupProcessingSnapshot:
        self.require_exists()
        with closing(self._connect()) as connection:
            change_state = self._read_archive_change_state(connection)
            state_row = cast(
                Optional[
                    tuple[
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                        object,
                    ]
                ],
                connection.execute(
                    """
                    SELECT
                        format_version,
                        processed_archive_revision,
                        processed_floor_map_revision,
                        page_count,
                        author_total_lou_count,
                        post_overlays_fingerprint,
                        post_version_selections_fingerprint,
                        floor_map_format_version,
                        floor_map_generation_version,
                        floor_map_hash_algorithm,
                        image_reference_extractor_version,
                        completed_at
                    FROM backup_processing_state
                    WHERE singleton = 1
                    """
                ).fetchone(),
            )
            pending_rows = cast(
                list[tuple[object]],
                connection.execute(
                    "SELECT url FROM backup_pending_images ORDER BY url"
                ).fetchall(),
            )

        pending_image_urls: list[str] = []
        for (url,) in pending_rows:
            if not isinstance(url, str) or not url:
                raise ValueError(f"backup待重试图片URL无效：{url!r}")
            pending_image_urls.append(url)
        return BackupProcessingSnapshot(
            change_state=change_state,
            processing_state=(
                None
                if state_row is None
                else self._backup_processing_state_from_row(state_row)
            ),
            pending_image_urls=tuple(pending_image_urls),
        )

    @staticmethod
    def _replace_pending_images(
        connection: sqlite3.Connection,
        pending_image_urls: set[str],
    ) -> None:
        if any(not url for url in pending_image_urls):
            raise ValueError("backup待重试图片URL不能为空。")
        connection.execute("DELETE FROM backup_pending_images")
        connection.executemany(
            "INSERT INTO backup_pending_images (url) VALUES (?)",
            [(url,) for url in sorted(pending_image_urls)],
        )

    def clear_backup_processing_state(self) -> None:
        if not self.exists():
            return
        with closing(self._connect()) as connection:
            with connection:
                connection.execute("DELETE FROM backup_pending_images")
                connection.execute("DELETE FROM backup_processing_state")

    def commit_backup_processing_state(
        self,
        state: BackupProcessingState,
        pending_image_urls: set[str],
    ) -> bool:
        self.require_exists()
        with closing(self._connect()) as connection:
            with connection:
                change_state = self._read_archive_change_state(connection)
                if (
                    change_state.archive_revision
                    != state.processed_archive_revision
                    or change_state.floor_map_revision
                    != state.processed_floor_map_revision
                ):
                    return False
                connection.execute("DELETE FROM backup_processing_state")
                connection.execute(
                    """
                    INSERT INTO backup_processing_state (
                        singleton,
                        format_version,
                        processed_archive_revision,
                        processed_floor_map_revision,
                        page_count,
                        author_total_lou_count,
                        post_overlays_fingerprint,
                        post_version_selections_fingerprint,
                        floor_map_format_version,
                        floor_map_generation_version,
                        floor_map_hash_algorithm,
                        image_reference_extractor_version,
                        completed_at
                    )
                    VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.format_version,
                        state.processed_archive_revision,
                        state.processed_floor_map_revision,
                        state.page_count,
                        state.author_total_lou_count,
                        state.post_overlays_fingerprint,
                        state.post_version_selections_fingerprint,
                        state.floor_map_format_version,
                        state.floor_map_generation_version,
                        state.floor_map_hash_algorithm,
                        state.image_reference_extractor_version,
                        state.completed_at,
                    ),
                )
                self._replace_pending_images(connection, pending_image_urls)
        return True

    def replace_backup_pending_images(
        self,
        expected_state: BackupProcessingState,
        pending_image_urls: set[str],
    ) -> bool:
        self.require_exists()
        with closing(self._connect()) as connection:
            with connection:
                change_state = self._read_archive_change_state(connection)
                if (
                    change_state.archive_revision
                    != expected_state.processed_archive_revision
                    or change_state.floor_map_revision
                    != expected_state.processed_floor_map_revision
                ):
                    return False
                state_identity = cast(
                    Optional[tuple[object, object, object, object]],
                    connection.execute(
                        """
                        SELECT
                            format_version,
                            processed_archive_revision,
                            processed_floor_map_revision,
                            completed_at
                        FROM backup_processing_state
                        WHERE singleton = 1
                        """
                    ).fetchone(),
                )
                if state_identity != (
                    expected_state.format_version,
                    expected_state.processed_archive_revision,
                    expected_state.processed_floor_map_revision,
                    expected_state.completed_at,
                ):
                    return False
                self._replace_pending_images(connection, pending_image_urls)
        return True

    @staticmethod
    def _increment_archive_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE archive_change_state
            SET archive_revision = archive_revision + 1
            WHERE singleton = 1
            """
        )

    @staticmethod
    def _increment_floor_map_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE archive_change_state
            SET floor_map_revision = floor_map_revision + 1
            WHERE singleton = 1
            """
        )

    def read_post_image_reference_cache(
        self,
        cache_keys: set[str],
    ) -> dict[str, PostImageReferenceCacheEntry]:
        if not cache_keys:
            return {}
        self.require_exists()

        entries: dict[str, PostImageReferenceCacheEntry] = {}
        sorted_cache_keys = sorted(cache_keys)
        with closing(self._connect()) as connection:
            for start in range(0, len(sorted_cache_keys), 900):
                chunk = sorted_cache_keys[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = cast(
                    list[tuple[object, object, object, object]],
                    connection.execute(
                        f"""
                        SELECT
                            cache_key,
                            source_hash,
                            extractor_version,
                            references_json
                        FROM post_image_reference_cache
                        WHERE cache_key IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall(),
                )
                for cache_key, source_hash, extractor_version, references_json in rows:
                    if (
                        not isinstance(cache_key, str)
                        or not isinstance(source_hash, str)
                        or type(extractor_version) is not int
                        or not isinstance(references_json, str)
                    ):
                        raise ValueError(
                            "archive图片引用缓存行字段无效："
                            f"{(cache_key, source_hash, extractor_version)!r}"
                        )
                    entries[cache_key] = PostImageReferenceCacheEntry(
                        cache_key=cache_key,
                        source_hash=source_hash,
                        extractor_version=extractor_version,
                        references_json=references_json,
                    )
        return entries

    def upsert_post_image_reference_cache(
        self,
        entries: list[PostImageReferenceCacheEntry],
    ) -> None:
        if not entries:
            return
        self.require_exists()

        now = _now_utc_iso()
        rows = [
            (
                entry.cache_key,
                entry.source_hash,
                entry.extractor_version,
                entry.references_json,
                now,
                now,
            )
            for entry in entries
        ]
        with closing(self._connect()) as connection:
            with connection:
                connection.executemany(
                    """
                    INSERT INTO post_image_reference_cache (
                        cache_key,
                        source_hash,
                        extractor_version,
                        references_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        source_hash = excluded.source_hash,
                        extractor_version = excluded.extractor_version,
                        references_json = excluded.references_json,
                        updated_at = excluded.updated_at
                    """,
                    rows,
                )

    @staticmethod
    def _validate_floor_map(floor_map: StoredFloorMap) -> None:
        integer_fields = {
            "version": floor_map.version,
            "generation_version": floor_map.generation_version,
            "tid": floor_map.tid,
            "aid": floor_map.aid,
        }
        for field_name, value in integer_fields.items():
            if type(value) is not int:
                raise ValueError(f"楼层映射字段必须是整数：{field_name}")
        if not floor_map.algorithm:
            raise ValueError("楼层映射algorithm不能为空。")
        if not floor_map.input_signature:
            raise ValueError("楼层映射input_signature不能为空。")

        seen_author_lous: set[int] = set()
        for entry in floor_map.entries:
            author_lou = entry["author_lou"]
            pid = entry["pid"]
            original_lou = entry["original_lou"]
            original_pid = entry.get("original_pid")
            candidates = entry.get("candidate_original_lous", [])
            if type(author_lou) is not int:
                raise ValueError("楼层映射author_lou必须是整数。")
            if author_lou in seen_author_lous:
                raise ValueError(f"楼层映射author_lou重复：{author_lou}")
            seen_author_lous.add(author_lou)
            for field_name, value in (
                ("pid", pid),
                ("original_lou", original_lou),
                ("original_pid", original_pid),
            ):
                if value is not None and type(value) is not int:
                    raise ValueError(
                        f"楼层映射{field_name}必须是整数或null：author_lou={author_lou}"
                    )
            if original_pid is not None and (pid is not None or original_lou is None):
                raise ValueError(
                    "楼层映射original_pid仅允许用于已确定原楼层的缺失楼："
                    f"author_lou={author_lou}"
                )
            if original_lou is not None and candidates:
                raise ValueError(
                    f"楼层映射不能同时有确定楼层和候选楼层：author_lou={author_lou}"
                )
            if any(type(candidate) is not int for candidate in candidates):
                raise ValueError(
                    f"楼层映射候选楼层必须都是整数：author_lou={author_lou}"
                )

    @staticmethod
    def _normalized_floor_map(floor_map: StoredFloorMap) -> StoredFloorMap:
        return StoredFloorMap(
            version=floor_map.version,
            generation_version=floor_map.generation_version,
            algorithm=floor_map.algorithm,
            tid=floor_map.tid,
            aid=floor_map.aid,
            input_signature=floor_map.input_signature,
            entries=sorted(
                floor_map.entries,
                key=lambda entry: entry["author_lou"],
            ),
        )

    def replace_floor_map(self, floor_map: StoredFloorMap) -> bool:
        self._validate_floor_map(floor_map)
        normalized_floor_map = self._normalized_floor_map(floor_map)
        self.require_exists()
        with closing(self._connect()) as connection:
            with connection:
                try:
                    current_floor_map = self._read_floor_map(connection)
                except ValueError:
                    current_floor_map = None
                if current_floor_map == normalized_floor_map:
                    return False
                connection.execute("DELETE FROM floor_map_candidates")
                connection.execute("DELETE FROM floor_map_entries")
                connection.execute("DELETE FROM floor_map_state")
                connection.execute(
                    """
                    INSERT INTO floor_map_state (
                        singleton,
                        tid,
                        aid,
                        format_version,
                        generation_version,
                        hash_algorithm,
                        input_signature
                    )
                    VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_floor_map.tid,
                        normalized_floor_map.aid,
                        normalized_floor_map.version,
                        normalized_floor_map.generation_version,
                        normalized_floor_map.algorithm,
                        normalized_floor_map.input_signature,
                    ),
                )
                for entry in normalized_floor_map.entries:
                    author_lou = entry["author_lou"]
                    connection.execute(
                        """
                        INSERT INTO floor_map_entries (
                            author_lou,
                            pid,
                            original_lou,
                            original_pid
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            author_lou,
                            entry["pid"],
                            entry["original_lou"],
                            entry.get("original_pid"),
                        ),
                    )
                    for candidate_index, original_lou in enumerate(
                        entry.get("candidate_original_lous", [])
                    ):
                        connection.execute(
                            """
                            INSERT INTO floor_map_candidates (
                                author_lou,
                                candidate_index,
                                original_lou
                            )
                            VALUES (?, ?, ?)
                            """,
                            (author_lou, candidate_index, original_lou),
                        )
                self._increment_floor_map_revision(connection)
        return True

    def _read_floor_map(
        self,
        connection: sqlite3.Connection,
    ) -> StoredFloorMap | None:
        state_row = connection.execute(
            """
            SELECT
                format_version,
                generation_version,
                hash_algorithm,
                tid,
                aid,
                input_signature
            FROM floor_map_state
            WHERE singleton = 1
            """
        ).fetchone()
        if state_row is None:
            return None
        entry_rows = connection.execute(
            """
            SELECT author_lou, pid, original_lou, original_pid
            FROM floor_map_entries
            ORDER BY author_lou
            """
        ).fetchall()
        candidate_rows = connection.execute(
            """
            SELECT author_lou, candidate_index, original_lou
            FROM floor_map_candidates
            ORDER BY author_lou, candidate_index
            """
        ).fetchall()

        candidates_by_author_lou: dict[int, list[int]] = {}
        for author_lou, candidate_index, original_lou in candidate_rows:
            if (
                type(author_lou) is not int
                or type(candidate_index) is not int
                or type(original_lou) is not int
            ):
                raise ValueError(f"archive楼层映射候选行无效：{candidate_rows!r}")
            candidates = candidates_by_author_lou.setdefault(author_lou, [])
            if candidate_index != len(candidates):
                raise ValueError(
                    "archive楼层映射候选序号不连续："
                    f"author_lou={author_lou}, candidate_index={candidate_index}"
                )
            candidates.append(original_lou)

        entries: list[FloorMapEntry] = []
        author_lous: set[int] = set()
        for author_lou, pid, original_lou, original_pid in entry_rows:
            if type(author_lou) is not int:
                raise ValueError(f"archive楼层映射author_lou无效：{author_lou!r}")
            entry: FloorMapEntry = {
                "pid": pid,
                "author_lou": author_lou,
                "original_lou": original_lou,
            }
            if original_pid is not None:
                entry["original_pid"] = original_pid
            candidates = candidates_by_author_lou.get(author_lou)
            if candidates:
                entry["candidate_original_lous"] = candidates
            entries.append(entry)
            author_lous.add(author_lou)
        orphan_candidates = set(candidates_by_author_lou) - author_lous
        if orphan_candidates:
            raise ValueError(
                "archive楼层映射候选缺少对应entry："
                f"{sorted(orphan_candidates)}"
            )

        version, generation_version, algorithm, tid, aid, input_signature = state_row
        if not isinstance(algorithm, str) or not algorithm:
            raise ValueError(f"archive楼层映射algorithm无效：{algorithm!r}")
        if not isinstance(input_signature, str) or not input_signature:
            raise ValueError(
                f"archive楼层映射input_signature无效：{input_signature!r}"
            )
        floor_map = StoredFloorMap(
            version=version,
            generation_version=generation_version,
            algorithm=algorithm,
            tid=tid,
            aid=aid,
            input_signature=input_signature,
            entries=entries,
        )
        self._validate_floor_map(floor_map)
        return floor_map

    def read_floor_map(self) -> StoredFloorMap | None:
        if not self.exists():
            return None
        with closing(self._connect()) as connection:
            return self._read_floor_map(connection)

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

    def _read_effective_processing_inputs(
        self,
        connection: sqlite3.Connection,
        lous: set[int],
    ) -> dict[int, tuple[int, int, str, Optional[int], Optional[str]]]:
        inputs_by_lou: dict[
            int,
            tuple[int, int, str, Optional[int], Optional[str]],
        ] = {}
        sorted_lous = sorted(lous)
        for start in range(0, len(sorted_lous), 900):
            chunk = sorted_lous[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = cast(
                list[tuple[object, object, object, object, object]],
                connection.execute(
                    f"""
                    SELECT
                        latest.lou,
                        latest.pid,
                        latest.source_hash,
                        metadata.author_uid,
                        metadata.image_attachments_json
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
                        WHERE lou IN ({placeholders})
                    ) AS latest
                    LEFT JOIN post_latest_metadata AS metadata
                        ON metadata.pid = latest.pid
                        AND metadata.lou = latest.lou
                    WHERE latest.row_number = 1
                    """,
                    chunk,
                ).fetchall(),
            )
            for lou, pid, source_hash, author_uid, image_attachments_json in rows:
                if (
                    type(lou) is not int
                    or type(pid) is not int
                    or not isinstance(source_hash, str)
                    or (author_uid is not None and type(author_uid) is not int)
                    or (
                        image_attachments_json is not None
                        and not isinstance(image_attachments_json, str)
                    )
                ):
                    raise ValueError(f"archive有效处理输入无效：{rows!r}")
                inputs_by_lou[lou] = (
                    lou,
                    pid,
                    source_hash,
                    author_uid,
                    image_attachments_json,
                )
        return inputs_by_lou

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
        parsed_post_items = [
            (raw_post, post_data_from_raw(raw_post)) for raw_post in raw_post_items
        ]
        affected_lous = {post["lou"] for _raw_post, post in parsed_post_items}

        with closing(self._connect()) as connection:
            with connection:
                inputs_before = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
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
                for position, (raw_post, post) in enumerate(parsed_post_items):
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
                inputs_after = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                effective_processing_inputs_changed = inputs_before != inputs_after
                if effective_processing_inputs_changed:
                    self._increment_archive_revision(connection)

        return ArchivePageUpsertResult(
            page_snapshot_inserted=snapshot_inserted,
            post_versions_inserted=post_versions_inserted,
            post_observations=len(raw_post_items),
            effective_processing_inputs_changed=(
                effective_processing_inputs_changed
            ),
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
        affected_lous = set(recovered_posts_by_author_lou)
        with closing(self._connect()) as connection:
            with connection:
                inputs_before = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
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
                inputs_after = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                if inputs_before != inputs_after:
                    self._increment_archive_revision(connection)
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

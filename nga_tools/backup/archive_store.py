from __future__ import annotations

import datetime
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from nga_tools.backup.archive_posts import (
    ArchivePostMetadata,
    metadata_from_raw_post,
)
from nga_tools.backup.content_codec import decode_content, encode_content
from nga_tools.backup.archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    require_current_archive_schema,
)
from nga_tools.backup.floor_models import (
    AuthorPostRef,
    FloorMapEntry,
    RecoveredMissingPost,
    StoredFloorMap,
)
from nga_tools.backup.models import PostData, PostRecord
from nga_tools.backup.post_data import post_data_from_raw, post_source_hash
from nga_tools.backup.post_overlay import (
    PostOverlay,
    post_overlay_from_storage,
    post_overlays_fingerprint,
)
from nga_tools.backup.post_version_selection import (
    PostVersionSelection,
    post_version_selections_fingerprint,
)
from nga_tools.backup.processing_state import (
    IMAGE_REFERENCE_MANIFEST_VERSION,
    ArchiveChangeState,
    AudioProcessingState,
    BackupProcessingSnapshot,
    FloorProcessingState,
    ImageReferenceManifestEntry,
    ImageReferenceManifestPost,
    ImageReferenceManifestSnapshot,
    ImageReferenceManifestState,
    ImageReferenceState,
    PendingAudioRetry,
    PendingImageRetry,
)
from nga_tools.backup.thread_stores import (
    ThreadArchiveCacheStore,
    ThreadArchiveStateStore,
)
from nga_tools.core.download_types import DOWNLOAD_FAILURE_KINDS
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.ngaclient.client import PageData
from nga_tools.storage import UnsupportedStorageFormatError, ensure_storage_metadata
from nga_tools.timing import time_section
from nga_tools.word_count import (
    WORD_COUNT_VERSION,
    TextWordCount,
    count_post_content,
)

ARCHIVE_DB_FILENAME = "archive.sqlite3"
_EMPTY_IMAGE_ATTACHMENTS_JSON = "[]"
_LATEST_POST_RECORDS_QUERY = """
    SELECT
        latest.id,
        latest.lou,
        latest.pid,
        latest.content,
        latest.source_hash
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
        post_latest_metadata.postdate_json
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
_LATEST_AUTHOR_POST_REFS_QUERY = """
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


@dataclass(frozen=True)
class ArchivePageUpsertResult:
    post_versions_inserted: int
    effective_processing_inputs_changed: bool
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class ArchivePagesUpsertResult:
    pages_processed: int
    post_versions_inserted: int
    effective_processing_inputs_changed: bool
    effective_changed_pages: int
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class RecoveredPostsUpsertResult:
    inserted_count: int
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class ArchivePagePagination:
    page_count: int
    vrows: Optional[int]


@dataclass(frozen=True)
class ArchiveEffectivePostStats:
    post_count: int
    max_lou: Optional[int]


@dataclass(frozen=True)
class AuthorFloorRefreshInputs:
    post_refs: tuple[AuthorPostRef, ...]
    stored_floor_map: StoredFloorMap | None
    floor_map_error: str | None


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
    manual_selection: bool


@dataclass(frozen=True)
class PostImageReferenceCacheEntry:
    cache_key: str
    source_hash: str
    extractor_version: int
    references_json: str


@dataclass(frozen=True)
class _PreparedArchivePost:
    raw_post: object
    post: PostData
    source_hash: str
    word_count: TextWordCount
    metadata: ArchivePostMetadata


@dataclass(frozen=True)
class _PreparedArchivePage:
    page_number: int
    total_page: Optional[int]
    vrows: Optional[int]
    observed_at: str
    count_observation: bool
    posts: tuple[_PreparedArchivePost, ...]


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _optional_int(data: dict[str, object], key: str) -> Optional[int]:
    value = data.get(key)
    if type(value) is int:
        return value
    return None


class ThreadArchiveStore:
    def __init__(
        self,
        thread_folder: Path,
    ) -> None:
        self.thread_folder = thread_folder
        self.db_path = thread_folder / ARCHIVE_DB_FILENAME
        self.state_store = ThreadArchiveStateStore(thread_folder)
        self.cache_store = ThreadArchiveCacheStore(thread_folder)
        self._schema_initialized = False
        self._store_id: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def require_exists(self) -> None:
        if not self.exists():
            raise RuntimeError(
                f"缺少archive.sqlite3：{self.db_path}。请重新运行备份初始化。"
            )

    def _connect_write(self) -> sqlite3.Connection:
        new_database = not self.db_path.is_file()
        self.thread_folder.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
        if not self._schema_initialized:
            try:
                self._ensure_schema(connection, new_database=new_database)
            except BaseException:
                connection.close()
                raise
            self._schema_initialized = True
        return connection

    def _connect_read(self) -> sqlite3.Connection:
        self.require_exists()
        database_uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
        try:
            metadata = require_current_archive_schema(connection, self.db_path)
            self._store_id = metadata.store_id
        except BaseException:
            connection.close()
            raise
        return connection

    def archive_store_id(self) -> str:
        if self._store_id is not None:
            return self._store_id
        with closing(self._connect_read()):
            pass
        if self._store_id is None:
            raise RuntimeError(f"archive无法读取store_id：{self.db_path}")
        return self._store_id

    def _connect_state_write(self) -> sqlite3.Connection:
        return self.state_store.connect_write(self.archive_store_id())

    def _connect_state_read(self) -> sqlite3.Connection:
        return self.state_store.connect_read(self.archive_store_id())

    def _connect_cache_write(self) -> sqlite3.Connection:
        return self.cache_store.connect_write(self.archive_store_id())

    def _connect_cache_read(self) -> sqlite3.Connection:
        return self.cache_store.connect_read(self.archive_store_id())

    def ensure_schema(self) -> None:
        with closing(self._connect_write()):
            pass

    def ensure_backup_processing_schema(self) -> None:
        """Ensure the data, state, and cache databases use layout v2."""
        if not self.exists():
            self.ensure_schema()
        source_store_id = self.archive_store_id()
        self.state_store.ensure_schema(source_store_id)
        self.cache_store.ensure_schema(source_store_id)

    def _create_archive_pages_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS archive_pages (
                page_number INTEGER PRIMARY KEY CHECK(page_number >= 1),
                total_page INTEGER,
                vrows INTEGER,
                last_seen_at TEXT NOT NULL
            )
            """
        )

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
                content BLOB NOT NULL CHECK(typeof(content) = 'blob'),
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

    def _create_post_version_selections_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_version_selections (
                lou INTEGER PRIMARY KEY CHECK(lou >= 0),
                version_id INTEGER NOT NULL UNIQUE,
                selected_at TEXT NOT NULL CHECK(selected_at != ''),
                FOREIGN KEY(version_id) REFERENCES post_versions(id)
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

    def _create_post_overlays_table(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS post_overlays (
                lou INTEGER PRIMARY KEY CHECK(lou >= 0),
                mode TEXT NOT NULL CHECK(mode = 'replace'),
                bbcode TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _create_archive_change_state_table(
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
    def _ensure_schema(
        self,
        connection: sqlite3.Connection,
        *,
        new_database: bool,
    ) -> None:
        if not new_database:
            metadata = require_current_archive_schema(connection, self.db_path)
            self._store_id = metadata.store_id
            return

        with connection:
            metadata = ensure_storage_metadata(connection, role="archive_data")
            self._store_id = metadata.store_id
            self._create_archive_pages_table(connection)
            self._create_post_versions_table(connection)
            self._create_post_latest_metadata_table(connection)
            self._create_post_version_selections_table(connection)
            self._create_floor_map_tables(connection)
            self._create_post_overlays_table(connection)
            self._create_archive_change_state_table(connection)
            connection.execute(
                """
                CREATE INDEX idx_post_versions_latest_covering
                ON post_versions(lou, last_seen_at DESC, id DESC, pid)
                """
            )
            connection.execute(f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}")

    @staticmethod
    def _post_overlays_from_rows(
        rows: list[tuple[object, object, object, object, object]],
    ) -> dict[int, PostOverlay]:
        overlays: dict[int, PostOverlay] = {}
        for row in rows:
            lou, mode, bbcode, content_hash, updated_at = row
            if type(lou) is not int or lou < 0:
                raise ValueError(f"archive post overlay楼层无效：{lou!r}")
            try:
                overlays[lou] = post_overlay_from_storage(
                    mode=mode,
                    bbcode=bbcode,
                    content_hash=content_hash,
                    updated_at=updated_at,
                )
            except ValueError as error:
                raise ValueError(
                    f"archive第{lou}楼post overlay无效：{error}"
                ) from error
        return overlays

    def read_post_overlays(
        self,
        lous: set[int] | None = None,
    ) -> dict[int, PostOverlay]:
        if not self.exists() or (lous is not None and not lous):
            return {}

        params: tuple[int, ...] = ()
        where_lous = ""
        if lous is not None:
            sorted_lous = tuple(sorted(lous))
            placeholders = ",".join("?" for _ in sorted_lous)
            where_lous = f"WHERE lou IN ({placeholders})"
            params = sorted_lous

        with closing(self._connect_read()) as connection:
            rows = cast(
                list[tuple[object, object, object, object, object]],
                connection.execute(
                    f"""
                    SELECT lou, mode, bbcode, content_hash, updated_at
                    FROM post_overlays
                    {where_lous}
                    ORDER BY lou
                    """,
                    params,
                ).fetchall(),
            )
        return self._post_overlays_from_rows(rows)

    def upsert_post_overlay(self, lou: int, overlay: PostOverlay) -> PostOverlay:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"overlay楼层必须是非负整数：{lou!r}")
        normalized_overlay = post_overlay_from_storage(
            mode=overlay["mode"],
            bbcode=overlay["bbcode"],
            content_hash=overlay["content_hash"],
            updated_at=overlay["updated_at"],
        )
        with closing(self._connect_write()) as connection:
            with connection:
                connection.execute(
                    """
                    INSERT INTO post_overlays (
                        lou,
                        mode,
                        bbcode,
                        content_hash,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(lou) DO UPDATE SET
                        mode = excluded.mode,
                        bbcode = excluded.bbcode,
                        content_hash = excluded.content_hash,
                        updated_at = excluded.updated_at
                    """,
                    (
                        lou,
                        normalized_overlay["mode"],
                        normalized_overlay["bbcode"],
                        normalized_overlay["content_hash"],
                        normalized_overlay["updated_at"],
                    ),
                )
        return normalized_overlay

    def delete_post_overlay(self, lou: int) -> bool:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"overlay楼层必须是非负整数：{lou!r}")
        with closing(self._connect_write()) as connection:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM post_overlays WHERE lou = ?",
                    (lou,),
                )
        return cursor.rowcount > 0

    def post_overlays_fingerprint(self) -> str:
        return post_overlays_fingerprint(self.read_post_overlays())

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

    def _read_current_archive_change_state(self) -> ArchiveChangeState:
        with closing(self._connect_read()) as connection:
            return self._read_archive_change_state(connection)

    def max_post_version_id(self) -> int:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM post_versions"
            ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not int:
            raise ValueError(f"archive帖子版本最大ID无效：{row!r}")
        if row[0] < 0:
            raise ValueError(f"archive帖子版本最大ID为负数：{row[0]}")
        return row[0]

    def read_post_version_contents(
        self,
        *,
        after_id: int,
        through_id: int,
    ) -> list[tuple[int, str]]:
        if after_id < 0 or through_id < after_id:
            raise ValueError(
                "archive帖子版本扫描范围无效："
                f"after={after_id}, through={through_id}"
            )
        if after_id == through_id:
            return []
        with closing(self._connect_read()) as connection:
            rows = connection.execute(
                """
                SELECT id, content
                FROM post_versions
                WHERE id > ? AND id <= ?
                ORDER BY id
                """,
                (after_id, through_id),
            ).fetchall()
        result: list[tuple[int, str]] = []
        for row in rows:
            if (
                len(row) != 2
                or type(row[0]) is not int
                or not isinstance(row[1], bytes)
            ):
                raise ValueError(f"archive帖子版本正文行无效：{row!r}")
            result.append(
                (
                    row[0],
                    decode_content(row[1], source=f"archive帖子版本{row[0]}正文"),
                )
            )
        return result

    def read_backup_processing_snapshot(self) -> BackupProcessingSnapshot:
        self.require_exists()
        change_state = self._read_current_archive_change_state()
        source_store_id = self.archive_store_id()
        if not self.state_store.exists():
            self.state_store.ensure_schema(source_store_id)
        try:
            with closing(self._connect_state_read()):
                pass
        except UnsupportedStorageFormatError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            self.state_store.recreate_after_error(source_store_id)
            return BackupProcessingSnapshot(
                change_state=change_state,
                pending_image_retries=(),
            )
        try:
            return self._read_backup_processing_snapshot_from_state(change_state)
        except (OSError, sqlite3.Error):
            self.state_store.recreate_after_error(source_store_id)
            return BackupProcessingSnapshot(
                change_state=change_state,
                pending_image_retries=(),
            )

    def _read_backup_processing_snapshot_from_state(
        self,
        change_state: ArchiveChangeState,
    ) -> BackupProcessingSnapshot:
        with closing(self._connect_state_read()) as connection:
            pending_rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    """
                    SELECT url, last_attempt_at, failure_kind, http_status
                    FROM backup_pending_images
                    ORDER BY url
                    """
                ).fetchall(),
            )
            pending_audio_rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    """
                    SELECT url, last_attempt_at, failure_kind, http_status
                    FROM backup_pending_audio
                    ORDER BY url
                    """
                ).fetchall(),
            )
            floor_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision,
                       processed_floor_map_revision, page_count,
                       author_total_lou_count, floor_map_format_version,
                       floor_map_generation_version, floor_map_hash_algorithm,
                       completed_at
                FROM backup_floor_processing_state WHERE singleton = 1
                """
            ).fetchone()
            image_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision,
                       post_overlays_fingerprint,
                       post_version_selections_fingerprint,
                       image_reference_extractor_version, completed_at
                FROM backup_image_reference_state WHERE singleton = 1
                """
            ).fetchone()
            audio_row = connection.execute(
                """
                SELECT format_version, extractor_version,
                       processed_max_post_version_id, completed_at
                FROM backup_audio_processing_state WHERE singleton = 1
                """
            ).fetchone()
        pending_image_retries: list[PendingImageRetry] = []
        for url, last_attempt_at, failure_kind, http_status in pending_rows:
            if not isinstance(url, str) or not url:
                raise ValueError(f"backup待重试图片URL无效：{url!r}")
            if last_attempt_at is None:
                if failure_kind is not None or http_status is not None:
                    raise ValueError(
                        f"backup待重试图片旧状态无效：{(url, failure_kind, http_status)!r}"
                    )
                parsed_last_attempt_at = None
                parsed_failure_kind = None
            else:
                if not isinstance(last_attempt_at, str):
                    raise ValueError(
                        f"backup待重试图片时间无效：{last_attempt_at!r}"
                    )
                try:
                    parsed_last_attempt_at = datetime.datetime.fromisoformat(
                        last_attempt_at
                    )
                except ValueError as error:
                    raise ValueError(
                        f"backup待重试图片时间无效：{last_attempt_at!r}"
                    ) from error
                if (
                    parsed_last_attempt_at.tzinfo is None
                    or parsed_last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        f"backup待重试图片时间缺少时区：{last_attempt_at!r}"
                    )
                if (
                    not isinstance(failure_kind, str)
                    or failure_kind not in DOWNLOAD_FAILURE_KINDS
                ):
                    raise ValueError(
                        f"backup待重试图片失败类别无效：{failure_kind!r}"
                    )
                parsed_failure_kind = failure_kind
            if http_status is not None and (
                type(http_status) is not int
                or http_status < 100
                or http_status > 599
            ):
                raise ValueError(
                    f"backup待重试图片HTTP状态无效：{http_status!r}"
                )
            pending_image_retries.append(
                PendingImageRetry(
                    url=url,
                    last_attempt_at=parsed_last_attempt_at,
                    failure_kind=parsed_failure_kind,
                    http_status=http_status,
                )
            )
        floor_state: FloorProcessingState | None = None
        if floor_row is not None:
            if any(type(value) is not int for value in floor_row[:4] + floor_row[5:7]):
                raise ValueError(f"backup楼层处理状态整数列无效：{floor_row!r}")
            if floor_row[4] is not None and type(floor_row[4]) is not int:
                raise ValueError(f"backup楼层处理状态vrows无效：{floor_row!r}")
            if not isinstance(floor_row[7], str) or not floor_row[7] or not isinstance(floor_row[8], str) or not floor_row[8]:
                raise ValueError(f"backup楼层处理状态文本列无效：{floor_row!r}")
            floor_state = FloorProcessingState(*floor_row)

        image_state: ImageReferenceState | None = None
        if image_row is not None:
            if type(image_row[0]) is not int or type(image_row[1]) is not int or type(image_row[4]) is not int:
                raise ValueError(f"backup图片引用状态整数列无效：{image_row!r}")
            if any(not isinstance(value, str) or not value for value in (image_row[2], image_row[3], image_row[5])):
                raise ValueError(f"backup图片引用状态文本列无效：{image_row!r}")
            image_state = ImageReferenceState(*image_row)

        pending_audio_retries: list[PendingAudioRetry] = []
        for url, last_attempt_at, failure_kind, http_status in pending_audio_rows:
            if not isinstance(url, str) or not url:
                raise ValueError(f"backup待重试音频URL无效：{url!r}")
            if last_attempt_at is None:
                if failure_kind is not None or http_status is not None:
                    raise ValueError(
                        "backup待重试音频旧状态无效："
                        f"{(url, failure_kind, http_status)!r}"
                    )
                parsed_last_attempt_at = None
                parsed_failure_kind = None
            else:
                if not isinstance(last_attempt_at, str):
                    raise ValueError(
                        f"backup待重试音频时间无效：{last_attempt_at!r}"
                    )
                try:
                    parsed_last_attempt_at = datetime.datetime.fromisoformat(
                        last_attempt_at
                    )
                except ValueError as error:
                    raise ValueError(
                        f"backup待重试音频时间无效：{last_attempt_at!r}"
                    ) from error
                if (
                    parsed_last_attempt_at.tzinfo is None
                    or parsed_last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        "backup待重试音频时间缺少时区："
                        f"{last_attempt_at!r}"
                    )
                if (
                    not isinstance(failure_kind, str)
                    or failure_kind not in DOWNLOAD_FAILURE_KINDS
                ):
                    raise ValueError(
                        "backup待重试音频失败类别无效："
                        f"{failure_kind!r}"
                    )
                parsed_failure_kind = failure_kind
            if http_status is not None and (
                type(http_status) is not int
                or http_status < 100
                or http_status > 599
            ):
                raise ValueError(
                    f"backup待重试音频HTTP状态无效：{http_status!r}"
                )
            pending_audio_retries.append(
                PendingAudioRetry(
                    url=url,
                    last_attempt_at=parsed_last_attempt_at,
                    failure_kind=parsed_failure_kind,
                    http_status=http_status,
                )
            )

        audio_state: AudioProcessingState | None = None
        if audio_row is not None:
            if any(type(value) is not int for value in audio_row[:3]):
                raise ValueError(
                    f"backup音频处理状态整数列无效：{audio_row!r}"
                )
            if audio_row[2] < 0:
                raise ValueError(
                    f"backup音频处理水位无效：{audio_row!r}"
                )
            if not isinstance(audio_row[3], str) or not audio_row[3]:
                raise ValueError(
                    f"backup音频处理状态时间无效：{audio_row!r}"
                )
            audio_state = AudioProcessingState(*audio_row)
        return BackupProcessingSnapshot(
            change_state=change_state,
            pending_image_retries=tuple(pending_image_retries),
            floor_state=floor_state,
            image_state=image_state,
            audio_state=audio_state,
            pending_audio_retries=tuple(pending_audio_retries),
        )

    @staticmethod
    def _replace_pending_images(
        connection: sqlite3.Connection,
        pending_image_retries: tuple[PendingImageRetry, ...],
    ) -> None:
        rows: list[tuple[str, str | None, str | None, int | None]] = []
        seen_urls: set[str] = set()
        for retry in sorted(pending_image_retries, key=lambda item: item.url):
            if not retry.url:
                raise ValueError("backup待重试图片URL不能为空。")
            if retry.url in seen_urls:
                raise ValueError(f"backup待重试图片URL重复：{retry.url}")
            seen_urls.add(retry.url)
            if retry.last_attempt_at is None:
                if retry.failure_kind is not None or retry.http_status is not None:
                    raise ValueError(
                        f"backup待重试图片旧状态无效：{retry.url}"
                    )
                last_attempt_text = None
            else:
                if (
                    retry.last_attempt_at.tzinfo is None
                    or retry.last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        f"backup待重试图片时间缺少时区：{retry.url}"
                    )
                if retry.failure_kind not in DOWNLOAD_FAILURE_KINDS:
                    raise ValueError(
                        f"backup待重试图片失败类别无效：{retry.failure_kind!r}"
                    )
                last_attempt_text = retry.last_attempt_at.astimezone(
                    datetime.timezone.utc
                ).isoformat(timespec="microseconds")
            if retry.http_status is not None and (
                type(retry.http_status) is not int
                or retry.http_status < 100
                or retry.http_status > 599
            ):
                raise ValueError(
                    f"backup待重试图片HTTP状态无效：{retry.http_status!r}"
                )
            rows.append(
                (
                    retry.url,
                    last_attempt_text,
                    retry.failure_kind,
                    retry.http_status,
                )
            )
        connection.execute("DELETE FROM backup_pending_images")
        connection.executemany(
            """
            INSERT INTO backup_pending_images (
                url,
                last_attempt_at,
                failure_kind,
                http_status
            )
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )

    @staticmethod
    def _replace_pending_audio(
        connection: sqlite3.Connection,
        pending_audio_retries: tuple[PendingAudioRetry, ...],
    ) -> None:
        rows: list[tuple[str, str | None, str | None, int | None]] = []
        seen_urls: set[str] = set()
        for retry in sorted(pending_audio_retries, key=lambda item: item.url):
            if not retry.url:
                raise ValueError("backup待重试音频URL不能为空。")
            if retry.url in seen_urls:
                raise ValueError(f"backup待重试音频URL重复：{retry.url}")
            seen_urls.add(retry.url)
            if retry.last_attempt_at is None:
                if retry.failure_kind is not None or retry.http_status is not None:
                    raise ValueError(
                        f"backup待重试音频旧状态无效：{retry.url}"
                    )
                last_attempt_text = None
            else:
                if (
                    retry.last_attempt_at.tzinfo is None
                    or retry.last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        f"backup待重试音频时间缺少时区：{retry.url}"
                    )
                if retry.failure_kind not in DOWNLOAD_FAILURE_KINDS:
                    raise ValueError(
                        "backup待重试音频失败类别无效："
                        f"{retry.failure_kind!r}"
                    )
                last_attempt_text = retry.last_attempt_at.astimezone(
                    datetime.timezone.utc
                ).isoformat(timespec="microseconds")
            if retry.http_status is not None and (
                type(retry.http_status) is not int
                or retry.http_status < 100
                or retry.http_status > 599
            ):
                raise ValueError(
                    "backup待重试音频HTTP状态无效："
                    f"{retry.http_status!r}"
                )
            rows.append(
                (
                    retry.url,
                    last_attempt_text,
                    retry.failure_kind,
                    retry.http_status,
                )
            )
        connection.execute("DELETE FROM backup_pending_audio")
        connection.executemany(
            """
            INSERT INTO backup_pending_audio (
                url,
                last_attempt_at,
                failure_kind,
                http_status
            )
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )

    @staticmethod
    def _clear_image_reference_manifest(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM backup_image_reference_manifest_entries")
        connection.execute("DELETE FROM backup_image_reference_manifest_posts")
        connection.execute("DELETE FROM backup_image_reference_manifest_urls")
        connection.execute("DELETE FROM backup_image_reference_manifest_state")

    @staticmethod
    def _validated_image_reference_manifest_posts(
        posts: tuple[ImageReferenceManifestPost, ...],
    ) -> tuple[
        tuple[ImageReferenceManifestPost, ...],
        Counter[str],
        dict[str, bool],
    ]:
        posts_by_lou: dict[int, ImageReferenceManifestPost] = {}
        reference_counts: Counter[str] = Counter()
        validity_by_url: dict[str, bool] = {}
        for post in posts:
            if type(post.lou) is not int or post.lou < 0:
                raise ValueError(f"图片引用清单楼层无效：{post.lou!r}")
            if not post.cache_key:
                raise ValueError(f"图片引用清单第{post.lou}楼缓存键为空。")
            if post.lou in posts_by_lou:
                raise ValueError(f"图片引用清单楼层重复：{post.lou}")
            previous_image_index = 0
            for reference in post.references:
                if (
                    type(reference.image_index) is not int
                    or reference.image_index <= previous_image_index
                ):
                    raise ValueError(
                        f"图片引用清单第{post.lou}楼序号无效："
                        f"{reference.image_index!r}"
                    )
                if not reference.url or type(reference.valid) is not bool:
                    raise ValueError(
                        f"图片引用清单第{post.lou}楼引用无效："
                        f"{reference!r}"
                    )
                previous_validity = validity_by_url.setdefault(
                    reference.url,
                    reference.valid,
                )
                if previous_validity != reference.valid:
                    raise ValueError(
                        f"图片引用清单URL合法性冲突：{reference.url}"
                    )
                reference_counts[reference.url] += 1
                previous_image_index = reference.image_index
            posts_by_lou[post.lou] = post
        return (
            tuple(posts_by_lou[lou] for lou in sorted(posts_by_lou)),
            reference_counts,
            validity_by_url,
        )

    @classmethod
    def _replace_image_reference_manifest(
        cls,
        connection: sqlite3.Connection,
        state: ImageReferenceManifestState,
        posts: tuple[ImageReferenceManifestPost, ...],
    ) -> None:
        if (
            type(state.format_version) is not int
            or type(state.processed_archive_revision) is not int
            or state.processed_archive_revision < 0
        ):
            raise ValueError(f"图片引用清单状态无效：{state!r}")
        ordered_posts, reference_counts, validity_by_url = (
            cls._validated_image_reference_manifest_posts(posts)
        )
        cls._clear_image_reference_manifest(connection)
        connection.execute(
            """
            INSERT INTO backup_image_reference_manifest_state
            (singleton, format_version, processed_archive_revision)
            VALUES (1, ?, ?)
            """,
            (state.format_version, state.processed_archive_revision),
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_posts (lou, cache_key)
            VALUES (?, ?)
            """,
            [(post.lou, post.cache_key) for post in ordered_posts],
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_entries
            (lou, image_index, url, valid)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    post.lou,
                    reference.image_index,
                    reference.url,
                    int(reference.valid),
                )
                for post in ordered_posts
                for reference in post.references
            ],
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_urls
            (url, reference_count, valid)
            VALUES (?, ?, ?)
            """,
            [
                (url, reference_counts[url], int(validity_by_url[url]))
                for url in sorted(reference_counts)
            ],
        )

    def read_image_reference_manifest(
        self,
    ) -> ImageReferenceManifestSnapshot | None:
        self.require_exists()
        with closing(self._connect_state_read()) as connection:
            state_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision
                FROM backup_image_reference_manifest_state
                WHERE singleton = 1
                """
            ).fetchone()
            post_rows = cast(
                list[tuple[object, object]],
                connection.execute(
                    """
                    SELECT lou, cache_key
                    FROM backup_image_reference_manifest_posts
                    ORDER BY lou
                    """
                ).fetchall(),
            )
            entry_rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    """
                    SELECT lou, image_index, url, valid
                    FROM backup_image_reference_manifest_entries
                    ORDER BY lou, image_index
                    """
                ).fetchall(),
            )
            url_rows = cast(
                list[tuple[object, object, object]],
                connection.execute(
                    """
                    SELECT url, reference_count, valid
                    FROM backup_image_reference_manifest_urls
                    ORDER BY url
                    """
                ).fetchall(),
            )

        if state_row is None:
            if post_rows or entry_rows or url_rows:
                raise ValueError("图片引用清单缺少状态行。")
            return None
        if (
            len(state_row) != 2
            or type(state_row[0]) is not int
            or type(state_row[1]) is not int
            or state_row[1] < 0
        ):
            raise ValueError(f"图片引用清单状态行无效：{state_row!r}")
        state = ImageReferenceManifestState(state_row[0], state_row[1])

        references_by_lou: dict[int, list[ImageReferenceManifestEntry]] = {}
        cache_key_by_lou: dict[int, str] = {}
        for lou, cache_key in post_rows:
            if type(lou) is not int or lou < 0 or not isinstance(cache_key, str) or not cache_key:
                raise ValueError(f"图片引用清单帖子行无效：{(lou, cache_key)!r}")
            if lou in cache_key_by_lou:
                raise ValueError(f"图片引用清单楼层重复：{lou}")
            cache_key_by_lou[lou] = cache_key
            references_by_lou[lou] = []
        for lou, image_index, url, valid in entry_rows:
            if (
                type(lou) is not int
                or lou not in references_by_lou
                or type(image_index) is not int
                or not isinstance(url, str)
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单引用行无效："
                    f"{(lou, image_index, url, valid)!r}"
                )
            references_by_lou[lou].append(
                ImageReferenceManifestEntry(image_index, url, bool(valid))
            )
        posts = tuple(
            ImageReferenceManifestPost(
                lou=lou,
                cache_key=cache_key_by_lou[lou],
                references=tuple(references_by_lou[lou]),
            )
            for lou in sorted(cache_key_by_lou)
        )
        ordered_posts, reference_counts, validity_by_url = (
            self._validated_image_reference_manifest_posts(posts)
        )

        stored_url_counts: list[tuple[str, int, bool]] = []
        for url, reference_count, valid in url_rows:
            if (
                not isinstance(url, str)
                or not url
                or type(reference_count) is not int
                or reference_count <= 0
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单URL行无效："
                    f"{(url, reference_count, valid)!r}"
                )
            stored_url_counts.append((url, reference_count, bool(valid)))
        expected_url_counts = [
            (url, reference_counts[url], validity_by_url[url])
            for url in sorted(reference_counts)
        ]
        if stored_url_counts != expected_url_counts:
            raise ValueError("图片引用清单URL引用计数不一致。")
        return ImageReferenceManifestSnapshot(
            state=state,
            posts=ordered_posts,
            url_reference_counts=tuple(stored_url_counts),
        )

    def read_image_reference_manifest_state(
        self,
    ) -> ImageReferenceManifestState | None:
        self.require_exists()
        with closing(self._connect_state_read()) as connection:
            row = connection.execute(
                """
                SELECT format_version, processed_archive_revision
                FROM backup_image_reference_manifest_state
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        if (
            len(row) != 2
            or type(row[0]) is not int
            or type(row[1]) is not int
            or row[1] < 0
        ):
            raise ValueError(f"图片引用清单状态行无效：{row!r}")
        return ImageReferenceManifestState(row[0], row[1])

    def read_image_reference_manifest_posts(
        self,
        lous: set[int],
    ) -> dict[int, ImageReferenceManifestPost]:
        self.require_exists()
        if not lous:
            return {}
        post_rows: list[tuple[object, object]] = []
        entry_rows: list[tuple[object, object, object, object]] = []
        with closing(self._connect_state_read()) as connection:
            for chunk in iter_in_clause_chunks(sorted(lous)):
                placeholders = ",".join("?" for _value in chunk)
                post_rows.extend(
                    cast(
                        list[tuple[object, object]],
                        connection.execute(
                            """
                            SELECT lou, cache_key
                            FROM backup_image_reference_manifest_posts
                            WHERE lou IN ("""
                            + placeholders
                            + ") ORDER BY lou",
                            chunk,
                        ).fetchall(),
                    )
                )
                entry_rows.extend(
                    cast(
                        list[tuple[object, object, object, object]],
                        connection.execute(
                            """
                            SELECT lou, image_index, url, valid
                            FROM backup_image_reference_manifest_entries
                            WHERE lou IN ("""
                            + placeholders
                            + ") ORDER BY lou, image_index",
                            chunk,
                        ).fetchall(),
                    )
                )

        cache_key_by_lou: dict[int, str] = {}
        references_by_lou: dict[int, list[ImageReferenceManifestEntry]] = {}
        for lou, cache_key in post_rows:
            if (
                type(lou) is not int
                or lou not in lous
                or not isinstance(cache_key, str)
                or not cache_key
                or lou in cache_key_by_lou
            ):
                raise ValueError(
                    f"图片引用清单帖子行无效：{(lou, cache_key)!r}"
                )
            cache_key_by_lou[lou] = cache_key
            references_by_lou[lou] = []
        for lou, image_index, url, valid in entry_rows:
            if (
                type(lou) is not int
                or lou not in references_by_lou
                or type(image_index) is not int
                or not isinstance(url, str)
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单引用行无效："
                    f"{(lou, image_index, url, valid)!r}"
                )
            references_by_lou[lou].append(
                ImageReferenceManifestEntry(image_index, url, bool(valid))
            )
        posts = tuple(
            ImageReferenceManifestPost(
                lou=lou,
                cache_key=cache_key_by_lou[lou],
                references=tuple(references_by_lou[lou]),
            )
            for lou in sorted(cache_key_by_lou)
        )
        ordered_posts, _counts, _validity = (
            self._validated_image_reference_manifest_posts(posts)
        )
        return {post.lou: post for post in ordered_posts}

    def read_image_reference_manifest_url_counts(
        self,
        urls: set[str],
    ) -> dict[str, tuple[int, bool]]:
        self.require_exists()
        if not urls:
            return {}
        rows: list[tuple[object, object, object]] = []
        with closing(self._connect_state_read()) as connection:
            for chunk in iter_in_clause_chunks(sorted(urls)):
                placeholders = ",".join("?" for _value in chunk)
                rows.extend(
                    cast(
                        list[tuple[object, object, object]],
                        connection.execute(
                            """
                            SELECT url, reference_count, valid
                            FROM backup_image_reference_manifest_urls
                            WHERE url IN ("""
                            + placeholders
                            + ") ORDER BY url",
                            chunk,
                        ).fetchall(),
                    )
                )
        result: dict[str, tuple[int, bool]] = {}
        for url, reference_count, valid in rows:
            if (
                not isinstance(url, str)
                or url not in urls
                or type(reference_count) is not int
                or reference_count <= 0
                or type(valid) is not int
                or valid not in (0, 1)
                or url in result
            ):
                raise ValueError(
                    f"图片引用清单URL行无效："
                    f"{(url, reference_count, valid)!r}"
                )
            result[url] = (reference_count, bool(valid))
        return result

    def clear_backup_processing_state(self) -> None:
        if not self.exists():
            return
        with closing(self._connect_state_write()) as connection:
            with connection:
                connection.execute("DELETE FROM backup_pending_images")
                connection.execute("DELETE FROM backup_floor_processing_state")
                connection.execute("DELETE FROM backup_image_reference_state")
                connection.execute("DELETE FROM backup_pending_audio")
                connection.execute("DELETE FROM backup_audio_processing_state")
                self._clear_image_reference_manifest(connection)

    def replace_pending_image_retries(
        self,
        pending_image_retries: tuple[PendingImageRetry, ...],
    ) -> None:
        """Replace rebuildable retry state without marking image processing current."""
        self.require_exists()
        with closing(self._connect_state_write()) as connection:
            with connection:
                self._replace_pending_images(connection, pending_image_retries)

    def commit_audio_processing_state(
        self,
        state: AudioProcessingState,
        pending_audio_retries: tuple[PendingAudioRetry, ...],
    ) -> bool:
        self.require_exists()
        if (
            type(state.format_version) is not int
            or state.format_version <= 0
            or type(state.extractor_version) is not int
            or state.extractor_version <= 0
            or type(state.processed_max_post_version_id) is not int
            or state.processed_max_post_version_id < 0
            or not state.completed_at
        ):
            raise ValueError(f"backup音频处理状态无效：{state!r}")
        expected_max_id = state.processed_max_post_version_id
        if self.max_post_version_id() != expected_max_id:
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                connection.execute(
                    "DELETE FROM backup_audio_processing_state"
                )
                connection.execute(
                    """
                    INSERT INTO backup_audio_processing_state (
                        singleton,
                        format_version,
                        extractor_version,
                        processed_max_post_version_id,
                        completed_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        state.format_version,
                        state.extractor_version,
                        state.processed_max_post_version_id,
                        state.completed_at,
                    ),
                )
                self._replace_pending_audio(
                    connection,
                    pending_audio_retries,
                )
        return self.max_post_version_id() == expected_max_id

    def commit_floor_processing_state(self, state: FloorProcessingState) -> bool:
        self.require_exists()
        expected_change_state = ArchiveChangeState(
            state.processed_archive_revision,
            state.processed_floor_map_revision,
        )
        if self._read_current_archive_change_state() != expected_change_state:
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                connection.execute("DELETE FROM backup_floor_processing_state")
                connection.execute(
                    """
                    INSERT INTO backup_floor_processing_state VALUES
                    (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.format_version,
                        state.processed_archive_revision,
                        state.processed_floor_map_revision,
                        state.page_count,
                        state.author_total_lou_count,
                        state.floor_map_format_version,
                        state.floor_map_generation_version,
                        state.floor_map_hash_algorithm,
                        state.completed_at,
                    ),
                )
        return self._read_current_archive_change_state() == expected_change_state

    def commit_image_reference_state(
        self,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingImageRetry, ...],
        *,
        manifest_posts: tuple[ImageReferenceManifestPost, ...] | None = None,
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
                if manifest_posts is None:
                    self._clear_image_reference_manifest(connection)
                else:
                    self._replace_image_reference_manifest(
                        connection,
                        ImageReferenceManifestState(
                            format_version=IMAGE_REFERENCE_MANIFEST_VERSION,
                            processed_archive_revision=(
                                state.processed_archive_revision
                            ),
                        ),
                        manifest_posts,
                    )
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )

    @staticmethod
    def _image_reference_state_values(
        state: ImageReferenceState,
    ) -> tuple[int, int, str, str, int, str]:
        return (
            state.format_version,
            state.processed_archive_revision,
            state.post_overlays_fingerprint,
            state.post_version_selections_fingerprint,
            state.image_reference_extractor_version,
            state.completed_at,
        )

    @classmethod
    def _stored_image_reference_state_matches(
        cls,
        connection: sqlite3.Connection,
        expected_state: ImageReferenceState,
    ) -> bool:
        row = connection.execute(
            """
            SELECT format_version, processed_archive_revision,
                   post_overlays_fingerprint,
                   post_version_selections_fingerprint,
                   image_reference_extractor_version, completed_at
            FROM backup_image_reference_state
            WHERE singleton = 1
            """
        ).fetchone()
        return row == cls._image_reference_state_values(expected_state)

    @classmethod
    def _replace_image_reference_state(
        cls,
        connection: sqlite3.Connection,
        state: ImageReferenceState,
    ) -> None:
        connection.execute("DELETE FROM backup_image_reference_state")
        connection.execute(
            """
            INSERT INTO backup_image_reference_state
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            cls._image_reference_state_values(state),
        )

    def commit_bootstrapped_image_reference_state(
        self,
        expected_state: ImageReferenceState,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingImageRetry, ...],
        manifest_posts: tuple[ImageReferenceManifestPost, ...],
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                if not self._stored_image_reference_state_matches(
                    connection,
                    expected_state,
                ):
                    return False
                manifest_state = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision
                    FROM backup_image_reference_manifest_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if manifest_state is not None:
                    return False
                manifest_data_exists = any(
                    connection.execute(
                        f"SELECT EXISTS(SELECT 1 FROM {table_name} LIMIT 1)"
                    ).fetchone()
                    != (0,)
                    for table_name in (
                        "backup_image_reference_manifest_posts",
                        "backup_image_reference_manifest_entries",
                        "backup_image_reference_manifest_urls",
                    )
                )
                if manifest_data_exists:
                    raise ValueError("图片引用清单缺少状态行。")
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
                self._replace_image_reference_manifest(
                    connection,
                    ImageReferenceManifestState(
                        format_version=IMAGE_REFERENCE_MANIFEST_VERSION,
                        processed_archive_revision=state.processed_archive_revision,
                    ),
                    manifest_posts,
                )
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )

    def commit_incremental_image_reference_state(
        self,
        expected_state: ImageReferenceState,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingImageRetry, ...],
        changed_posts: tuple[ImageReferenceManifestPost, ...],
    ) -> bool:
        if not changed_posts:
            raise ValueError("增量图片引用清单不能为空。")
        ordered_posts, new_reference_counts, new_validity_by_url = (
            self._validated_image_reference_manifest_posts(changed_posts)
        )
        changed_lous = [post.lou for post in ordered_posts]

        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                if not self._stored_image_reference_state_matches(
                    connection,
                    expected_state,
                ):
                    return False
                manifest_state = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision
                    FROM backup_image_reference_manifest_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if manifest_state != (
                    IMAGE_REFERENCE_MANIFEST_VERSION,
                    expected_state.processed_archive_revision,
                ):
                    return False

                old_reference_counts: Counter[str] = Counter()
                old_validity_by_url: dict[str, bool] = {}
                for chunk in iter_in_clause_chunks(changed_lous):
                    placeholders = ",".join("?" for _value in chunk)
                    old_rows = cast(
                        list[tuple[object, object]],
                        connection.execute(
                            """
                            SELECT url, valid
                            FROM backup_image_reference_manifest_entries
                            WHERE lou IN ("""
                            + placeholders
                            + ")",
                            chunk,
                        ).fetchall(),
                    )
                    for url, valid in old_rows:
                        if (
                            not isinstance(url, str)
                            or type(valid) is not int
                            or valid not in (0, 1)
                        ):
                            raise ValueError(
                                f"图片引用清单引用行无效："
                                f"{(url, valid)!r}"
                            )
                        old_reference_counts[url] += 1
                        previous_validity = old_validity_by_url.setdefault(
                            url,
                            bool(valid),
                        )
                        if previous_validity != bool(valid):
                            raise ValueError(
                                f"图片引用清单URL合法性冲突：{url}"
                            )

                for url, removed_count in old_reference_counts.items():
                    stored_row = connection.execute(
                        """
                        SELECT reference_count, valid
                        FROM backup_image_reference_manifest_urls
                        WHERE url = ?
                        """,
                        (url,),
                    ).fetchone()
                    if (
                        stored_row is None
                        or type(stored_row[0]) is not int
                        or stored_row[0] < removed_count
                        or type(stored_row[1]) is not int
                        or stored_row[1] not in (0, 1)
                        or bool(stored_row[1]) != old_validity_by_url[url]
                    ):
                        raise ValueError(
                            f"图片引用清单URL计数无效：{url}"
                        )
                    remaining_count = stored_row[0] - removed_count
                    if remaining_count == 0:
                        connection.execute(
                            "DELETE FROM backup_image_reference_manifest_urls WHERE url = ?",
                            (url,),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE backup_image_reference_manifest_urls
                            SET reference_count = ?
                            WHERE url = ?
                            """,
                            (remaining_count, url),
                        )

                connection.executemany(
                    "DELETE FROM backup_image_reference_manifest_entries WHERE lou = ?",
                    [(lou,) for lou in changed_lous],
                )
                connection.executemany(
                    "DELETE FROM backup_image_reference_manifest_posts WHERE lou = ?",
                    [(lou,) for lou in changed_lous],
                )
                connection.executemany(
                    """
                    INSERT INTO backup_image_reference_manifest_posts
                    (lou, cache_key) VALUES (?, ?)
                    """,
                    [(post.lou, post.cache_key) for post in ordered_posts],
                )
                connection.executemany(
                    """
                    INSERT INTO backup_image_reference_manifest_entries
                    (lou, image_index, url, valid) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            post.lou,
                            reference.image_index,
                            reference.url,
                            int(reference.valid),
                        )
                        for post in ordered_posts
                        for reference in post.references
                    ],
                )
                for url, added_count in new_reference_counts.items():
                    stored_row = connection.execute(
                        """
                        SELECT reference_count, valid
                        FROM backup_image_reference_manifest_urls
                        WHERE url = ?
                        """,
                        (url,),
                    ).fetchone()
                    if stored_row is None:
                        connection.execute(
                            """
                            INSERT INTO backup_image_reference_manifest_urls
                            (url, reference_count, valid) VALUES (?, ?, ?)
                            """,
                            (url, added_count, int(new_validity_by_url[url])),
                        )
                    else:
                        if (
                            type(stored_row[0]) is not int
                            or stored_row[0] <= 0
                            or type(stored_row[1]) is not int
                            or stored_row[1] not in (0, 1)
                            or bool(stored_row[1]) != new_validity_by_url[url]
                        ):
                            raise ValueError(
                                f"图片引用清单URL计数无效：{url}"
                            )
                        connection.execute(
                            """
                            UPDATE backup_image_reference_manifest_urls
                            SET reference_count = reference_count + ?
                            WHERE url = ?
                            """,
                            (added_count, url),
                        )

                connection.execute(
                    """
                    UPDATE backup_image_reference_manifest_state
                    SET processed_archive_revision = ?
                    WHERE singleton = 1
                    """,
                    (state.processed_archive_revision,),
                )
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )

    def replace_pending_images_for_image_state(
        self,
        expected_state: ImageReferenceState,
        pending_image_retries: tuple[PendingImageRetry, ...],
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != expected_state.processed_archive_revision
        ):
            return False
        with closing(self._connect_state_write()) as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision, completed_at
                    FROM backup_image_reference_state WHERE singleton = 1
                    """
                ).fetchone()
                if row != (
                    expected_state.format_version,
                    expected_state.processed_archive_revision,
                    expected_state.completed_at,
                ):
                    return False
                self._replace_pending_images(connection, pending_image_retries)
        return (
            self._read_current_archive_change_state().archive_revision
            == expected_state.processed_archive_revision
        )

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
        if not cache_keys or not self.cache_store.exists():
            return {}
        try:
            return self._read_post_image_reference_cache(cache_keys)
        except UnsupportedStorageFormatError:
            raise
        except (OSError, sqlite3.Error, ValueError):
            self.cache_store.recreate_after_error(self.archive_store_id())
            return {}

    def _read_post_image_reference_cache(
        self,
        cache_keys: set[str],
    ) -> dict[str, PostImageReferenceCacheEntry]:
        if not cache_keys:
            return {}
        self.require_exists()

        entries: dict[str, PostImageReferenceCacheEntry] = {}
        sorted_cache_keys = sorted(cache_keys)
        with closing(self._connect_cache_read()) as connection:
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
        with closing(self._connect_cache_write()) as connection:
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
        with closing(self._connect_write()) as connection:
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
        with closing(self._connect_read()) as connection:
            return self._read_floor_map(connection)

    def _upsert_archive_page(
        self,
        connection: sqlite3.Connection,
        page: _PreparedArchivePage,
    ) -> None:
        connection.execute(
            """
            INSERT INTO archive_pages (
                page_number,
                total_page,
                vrows,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(page_number) DO UPDATE SET
                total_page = excluded.total_page,
                vrows = excluded.vrows,
                last_seen_at = excluded.last_seen_at
            WHERE excluded.last_seen_at >= archive_pages.last_seen_at
            """,
            (
                page.page_number,
                page.total_page,
                page.vrows,
                page.observed_at,
            ),
        )

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
        source_hash: str | None = None,
        word_count: TextWordCount | None = None,
    ) -> tuple[int, bool]:
        if source_hash is None:
            source_hash = post_source_hash(post)
        if word_count is None:
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
                encode_content(post["content"]),
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
        metadata: ArchivePostMetadata | None = None,
    ) -> None:
        if metadata is None:
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
                _EMPTY_IMAGE_ATTACHMENTS_JSON,
                observed_at,
                observed_at,
                seen_increment,
            ),
        )

    def _read_effective_processing_inputs(
        self,
        connection: sqlite3.Connection,
        lous: set[int],
    ) -> dict[int, tuple[int, int, str, Optional[int]]]:
        inputs_by_lou: dict[
            int,
            tuple[int, int, str, Optional[int]],
        ] = {}
        sorted_lous = sorted(lous)
        for start in range(0, len(sorted_lous), 900):
            chunk = sorted_lous[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    f"""
                    SELECT
                        latest.lou,
                        latest.pid,
                        latest.source_hash,
                        metadata.author_uid
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
            for lou, pid, source_hash, author_uid in rows:
                if (
                    type(lou) is not int
                    or type(pid) is not int
                    or not isinstance(source_hash, str)
                    or (author_uid is not None and type(author_uid) is not int)
                ):
                    raise ValueError(f"archive有效处理输入无效：{rows!r}")
                inputs_by_lou[lou] = (
                    lou,
                    pid,
                    source_hash,
                    author_uid,
                )
        return inputs_by_lou

    @staticmethod
    def _prepare_archive_page(
        page_number: int,
        page_data: PageData,
        *,
        observed_at: str,
        count_observation: bool,
    ) -> _PreparedArchivePage:
        raw_posts = page_data.get("result")
        if not isinstance(raw_posts, list):
            raise ValueError("NGA响应中缺少帖子列表。")
        raw_post_items = cast(list[object], raw_posts)
        prepared_posts: list[_PreparedArchivePost] = []
        for raw_post in raw_post_items:
            post = post_data_from_raw(raw_post)
            prepared_posts.append(
                _PreparedArchivePost(
                    raw_post=raw_post,
                    post=post,
                    source_hash=post_source_hash(post),
                    word_count=count_post_content(post["content"]),
                    metadata=metadata_from_raw_post(raw_post),
                )
            )
        return _PreparedArchivePage(
            page_number=page_number,
            total_page=_optional_int(page_data, "totalPage"),
            vrows=_optional_int(page_data, "vrows"),
            observed_at=observed_at,
            count_observation=count_observation,
            posts=tuple(prepared_posts),
        )

    def page_effective_processing_inputs_changed(
        self,
        page_number: int,
        page_data: PageData,
    ) -> bool:
        prepared_page = self._prepare_archive_page(
            page_number,
            page_data,
            observed_at=_now_utc_iso(),
            count_observation=False,
        )
        affected_lous = {item.post["lou"] for item in prepared_page.posts}
        with closing(self._connect_read()) as connection:
            inputs_before = self._read_effective_processing_inputs(
                connection,
                affected_lous,
            )
        inputs_after = {
            item.post["lou"]: (
                item.post["lou"],
                item.post["pid"],
                item.source_hash,
                item.metadata["author_uid"],
            )
            for item in prepared_page.posts
        }
        return any(
            inputs_before.get(lou) != inputs_after.get(lou)
            for lou in affected_lous
        )

    def upsert_pages(
        self,
        page_data_by_page: dict[int, PageData],
        *,
        observed_at: str | None = None,
        count_observation: bool = True,
    ) -> ArchivePagesUpsertResult:
        prepared_pages = [
            self._prepare_archive_page(
                page_number,
                page_data_by_page[page_number],
                observed_at=(
                    _now_utc_iso() if observed_at is None else observed_at
                ),
                count_observation=count_observation,
            )
            for page_number in sorted(page_data_by_page)
        ]
        if not prepared_pages:
            return ArchivePagesUpsertResult(
                pages_processed=0,
                post_versions_inserted=0,
                effective_processing_inputs_changed=False,
                effective_changed_pages=0,
                effective_changed_lous=frozenset(),
                effective_added_lous=frozenset(),
            )

        affected_lous_by_page = {
            page.page_number: {item.post["lou"] for item in page.posts}
            for page in prepared_pages
        }
        affected_lous: set[int] = {
            lou
            for page_lous in affected_lous_by_page.values()
            for lou in page_lous
        }
        post_versions_inserted = 0
        changed_lous: set[int] = set()

        with closing(self._connect_write()) as connection:
            with connection:
                inputs_before = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                for page in prepared_pages:
                    self._upsert_archive_page(connection, page)
                    for prepared_post in page.posts:
                        post = prepared_post.post
                        _version_id, version_inserted = self._upsert_post_version(
                            connection,
                            post,
                            page.observed_at,
                            count_observation=page.count_observation,
                            source_hash=prepared_post.source_hash,
                            word_count=prepared_post.word_count,
                        )
                        self._upsert_post_latest_metadata(
                            connection,
                            prepared_post.raw_post,
                            post,
                            page.observed_at,
                            count_observation=page.count_observation,
                            metadata=prepared_post.metadata,
                        )
                        if version_inserted:
                            post_versions_inserted += 1

                inputs_after = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                changed_lous = {
                    lou
                    for lou in affected_lous
                    if inputs_before.get(lou) != inputs_after.get(lou)
                }
                if changed_lous:
                    self._increment_archive_revision(connection)

        return ArchivePagesUpsertResult(
            pages_processed=len(prepared_pages),
            post_versions_inserted=post_versions_inserted,
            effective_processing_inputs_changed=bool(changed_lous),
            effective_changed_pages=sum(
                bool(page_lous & changed_lous)
                for page_lous in affected_lous_by_page.values()
            ),
            effective_changed_lous=frozenset(changed_lous),
            effective_added_lous=frozenset(
                lou for lou in changed_lous if lou not in inputs_before
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
        result = self.upsert_pages(
            {page_number: page_data},
            observed_at=observed_at,
            count_observation=count_observation,
        )

        return ArchivePageUpsertResult(
            post_versions_inserted=result.post_versions_inserted,
            effective_processing_inputs_changed=(
                result.effective_processing_inputs_changed
            ),
            effective_changed_lous=result.effective_changed_lous,
            effective_added_lous=result.effective_added_lous,
        )

    def upsert_recovered_posts(
        self,
        recovered_posts_by_author_lou: dict[int, RecoveredMissingPost],
        *,
        observed_at: str | None = None,
    ) -> RecoveredPostsUpsertResult:
        if not recovered_posts_by_author_lou:
            return RecoveredPostsUpsertResult(
                0,
                frozenset(),
                frozenset(),
            )

        observed_at = _now_utc_iso() if observed_at is None else observed_at
        inserted_count = 0
        affected_lous = set(recovered_posts_by_author_lou)
        changed_lous: set[int] = set()
        with closing(self._connect_write()) as connection:
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
                changed_lous = {
                    lou
                    for lou in affected_lous
                    if inputs_before.get(lou) != inputs_after.get(lou)
                }
                if changed_lous:
                    self._increment_archive_revision(connection)
        return RecoveredPostsUpsertResult(
            inserted_count,
            frozenset(changed_lous),
            frozenset(
                lou for lou in changed_lous if lou not in inputs_before
            ),
        )

    def refresh_stored_word_counts(self) -> int:
        self.require_exists()
        with closing(self._connect_write()) as connection:
            with connection:
                rows = cast(
                    list[tuple[int, object]],
                    connection.execute(
                        """
                        SELECT id, content
                        FROM post_versions
                        WHERE word_count_version != ?
                        """,
                        (WORD_COUNT_VERSION,),
                    ).fetchall(),
                )
                for row_id, raw_content in rows:
                    content = decode_content(
                        raw_content,
                        source=f"archive帖子版本{row_id}正文",
                    )
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

        with closing(self._connect_read()) as connection:
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
        rows = cast(
            list[tuple[object, object, object]],
            connection.execute(
                """
                SELECT lou, version_id, selected_at
                FROM post_version_selections
                ORDER BY lou
                """
            ).fetchall(),
        )

        valid_selections: dict[int, PostVersionSelection] = {}
        for raw_lou, raw_version_id, raw_selected_at in rows:
            if (
                type(raw_lou) is not int
                or raw_lou < 0
                or type(raw_version_id) is not int
                or not isinstance(raw_selected_at, str)
                or not raw_selected_at
            ):
                continue
            lou = raw_lou
            version_id = raw_version_id
            if lous is not None and lou not in lous:
                continue
            version_row = cast(
                Optional[tuple[object, object]],
                connection.execute(
                    """
                    SELECT lou, source_hash
                    FROM post_versions
                    WHERE id = ?
                    """,
                    (version_id,),
                ).fetchone(),
            )
            if version_row is None:
                continue
            version_lou, source_hash = version_row
            if version_lou != lou or not isinstance(source_hash, str):
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
            if latest_row is None or latest_row[0] == version_id:
                continue
            valid_selections[lou] = {
                "version_id": version_id,
                "source_hash": source_hash,
                "selected_at": raw_selected_at,
            }
        return valid_selections

    def read_valid_post_version_selections(self) -> dict[int, PostVersionSelection]:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            return self._validated_post_version_selections(connection)

    def post_version_selections_fingerprint(self) -> str:
        return post_version_selections_fingerprint(
            self.read_valid_post_version_selections()
        )

    def upsert_post_version_selection(
        self,
        lou: int,
        version_id: int,
    ) -> PostVersionSelection:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"正文版本选择楼层必须是非负整数：{lou!r}")
        if type(version_id) is not int or version_id < 1:
            raise ValueError(f"正文版本ID必须是正整数：{version_id!r}")

        selected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with closing(self._connect_write()) as connection:
            with connection:
                version_row = cast(
                    Optional[tuple[object, object]],
                    connection.execute(
                        """
                        SELECT lou, source_hash
                        FROM post_versions
                        WHERE id = ?
                        """,
                        (version_id,),
                    ).fetchone(),
                )
                if version_row is None:
                    raise ValueError("未知帖子正文版本。")
                version_lou, source_hash = version_row
                if version_lou != lou:
                    raise ValueError("帖子正文版本不属于指定楼层。")
                if not isinstance(source_hash, str):
                    raise ValueError("帖子正文版本哈希无效。")

                latest_row = cast(
                    Optional[tuple[object]],
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
                if latest_row is None or type(latest_row[0]) is not int:
                    raise ValueError("未知楼层。")
                if latest_row[0] == version_id:
                    raise ValueError("不能手动选择当前最新版。")

                connection.execute(
                    """
                    INSERT INTO post_version_selections (
                        lou,
                        version_id,
                        selected_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(lou) DO UPDATE SET
                        version_id = excluded.version_id,
                        selected_at = excluded.selected_at
                    """,
                    (lou, version_id, selected_at),
                )

        return {
            "version_id": version_id,
            "source_hash": source_hash,
            "selected_at": selected_at,
        }

    def delete_post_version_selection(self, lou: int) -> int:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"正文版本选择楼层必须是非负整数：{lou!r}")
        with closing(self._connect_write()) as connection:
            with connection:
                latest_row = cast(
                    Optional[tuple[object]],
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
                if latest_row is None or type(latest_row[0]) is not int:
                    raise ValueError("未知楼层。")
                connection.execute(
                    "DELETE FROM post_version_selections WHERE lou = ?",
                    (lou,),
                )
        return latest_row[0]

    def read_effective_post_stats(self) -> ArchiveEffectivePostStats:
        self.require_exists()
        with closing(self._connect_read()) as connection:
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

        with closing(self._connect_read()) as connection:
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

    @staticmethod
    def _effective_post_row_from_sql_row(
        row: tuple[
            int,
            int,
            int,
            object,
            str,
            Optional[str],
            Optional[int],
            Optional[str],
        ],
        *,
        manual_selection: bool,
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
        ) = row
        return ArchivePostVersionRow(
            version_id=version_id,
            lou=lou,
            pid=pid,
            content=decode_content(
                content,
                source=f"archive帖子版本{version_id}正文",
            ),
            source_hash=source_hash,
            author_name=author_name,
            author_uid=author_uid,
            postdate_json=postdate_json,
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

        with closing(self._connect_read()) as connection:
            latest_rows = cast(
                list[
                    tuple[
                        int,
                        int,
                        int,
                        object,
                        str,
                        Optional[str],
                        Optional[int],
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
                            object,
                            str,
                            Optional[str],
                            Optional[int],
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
                            post_latest_metadata.postdate_json
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
                    selected_row,
                    manual_selection=True,
                )

        return [rows_by_lou[lou] for lou in sorted(rows_by_lou)]

    def read_post_row_for_version(
        self,
        version_id: int,
    ) -> ArchivePostVersionRow | None:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            row = cast(
                Optional[
                    tuple[
                        int,
                        int,
                        int,
                        object,
                        str,
                        Optional[str],
                        Optional[int],
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
                        post_latest_metadata.postdate_json
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
                row,
                manual_selection=False,
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

        with closing(self._connect_read()) as connection:
            rows = cast(
                list[tuple[int, int, int, object, str]],
                connection.execute(
                    _LATEST_POST_RECORDS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )

        records: list[PostRecord] = []
        for _version_id, lou, pid, raw_content, source_hash in rows:
            content = decode_content(
                raw_content,
                source=f"archive帖子版本{_version_id}正文",
            )
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": {
                        "lou": lou,
                        "pid": pid,
                        "content": content,
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
            records.append(
                {
                    "lou": row.lou,
                    "pid": row.pid,
                    "post": {
                        "lou": row.lou,
                        "pid": row.pid,
                        "content": row.content,
                    },
                    "html": None,
                    "source_hash": row.source_hash,
                }
            )
        return records

    @staticmethod
    def _read_latest_author_post_refs(
        connection: sqlite3.Connection,
    ) -> list[AuthorPostRef]:
        rows = cast(
            list[tuple[int, int, Optional[int]]],
            connection.execute(_LATEST_AUTHOR_POST_REFS_QUERY).fetchall(),
        )
        return [
            {"pid": pid, "author_lou": lou}
            for pid, lou, author_uid in rows
            if author_uid != -1
        ]

    def read_latest_author_post_refs(self) -> list[AuthorPostRef]:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            return self._read_latest_author_post_refs(connection)

    def read_author_floor_refresh_inputs(self) -> AuthorFloorRefreshInputs:
        """Read author refs and the prior floor map from one SQLite snapshot."""
        self.require_exists()
        with closing(self._connect_read()) as connection:
            connection.execute("BEGIN")
            with time_section("楼主最新回复索引读取"):
                post_refs = self._read_latest_author_post_refs(connection)
            try:
                with time_section("历史未恢复缺失楼读取"):
                    stored_floor_map = self._read_floor_map(connection)
            except ValueError as error:
                return AuthorFloorRefreshInputs(
                    tuple(post_refs),
                    None,
                    str(error),
                )
        return AuthorFloorRefreshInputs(tuple(post_refs), stored_floor_map, None)

    def read_latest_author_total_lou_count(self) -> Optional[int]:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            row = connection.execute(
                """
                SELECT vrows
                FROM archive_pages
                WHERE page_number = 1
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

    def read_latest_page_one_pagination(
        self,
    ) -> ArchivePagePagination | None:
        self.require_exists()
        with closing(self._connect_read()) as connection:
            row = cast(
                Optional[tuple[object, object]],
                connection.execute(
                    """
                    SELECT total_page, vrows
                    FROM archive_pages
                    WHERE page_number = 1
                    """
                ).fetchone(),
            )

        if row is None:
            return None
        total_page, vrows = row
        if total_page is None:
            page_count = 1
        elif type(total_page) is int:
            page_count = total_page
        else:
            raise ValueError(f"archive totalPage字段无效：{total_page!r}")
        if vrows is not None and type(vrows) is not int:
            raise ValueError(f"archive vrows字段无效：{vrows!r}")
        return ArchivePagePagination(page_count, vrows)

    def read_page_numbers(self) -> set[int]:
        if not self.exists():
            return set()
        with closing(self._connect_read()) as connection:
            rows = cast(
                list[tuple[int]],
                connection.execute(
                    "SELECT page_number FROM archive_pages"
                ).fetchall(),
            )

        page_numbers: set[int] = set()
        for (page_number,) in rows:
            if type(page_number) is not int:
                raise ValueError(f"archive page_number字段无效：{page_number!r}")
            page_numbers.add(page_number)
        return page_numbers

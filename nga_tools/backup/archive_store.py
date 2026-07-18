from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional, cast

from nga_tools.backup.archive_cache_store import ArchiveCacheRepository
from nga_tools.backup.archive_floor_store import ArchiveFloorMapRepository
from nga_tools.backup.archive_ingest_store import ArchiveIngestRepository
from nga_tools.backup.archive_overlay_store import ArchiveOverlayRepository
from nga_tools.backup.archive_post_store import ArchivePostRepository
from nga_tools.backup.archive_state_store import ArchiveStateRepository
from nga_tools.backup.archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    require_current_archive_identity,
    require_current_archive_schema,
)
from nga_tools.backup.processing_state import ArchiveChangeState
from nga_tools.backup.thread_stores import (
    ThreadArchiveCacheStore,
    ThreadArchiveStateStore,
)
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
)
from nga_tools.storage import ensure_storage_metadata

ARCHIVE_DB_FILENAME = "archive.sqlite3"

class ThreadArchiveStore:
    def __init__(
        self,
        thread_folder: Path,
    ) -> None:
        self.thread_folder = thread_folder
        self.db_path = thread_folder / ARCHIVE_DB_FILENAME
        state_store = ThreadArchiveStateStore(thread_folder)
        cache_store = ThreadArchiveCacheStore(thread_folder)
        self.overlays = ArchiveOverlayRepository(self)
        self.floor_maps = ArchiveFloorMapRepository(self)
        self.ingest = ArchiveIngestRepository(self)
        self.posts = ArchivePostRepository(self, self.floor_maps)
        self.state = ArchiveStateRepository(self, state_store, self.posts)
        self.cache = ArchiveCacheRepository(self, cache_store)
        self._schema_initialized = False
        self._store_id: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def require_exists(self) -> None:
        if not self.exists():
            raise RuntimeError(
                f"缺少archive.sqlite3：{self.db_path}。请重新运行备份初始化。"
            )

    def connect_write(self) -> sqlite3.Connection:
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

    def connect_read(self) -> sqlite3.Connection:
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
        with closing(self.connect_read()):
            pass
        if self._store_id is None:
            raise RuntimeError(f"archive无法读取store_id：{self.db_path}")
        return self._store_id

    def ensure_schema(self) -> None:
        with closing(self.connect_write()):
            pass

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
        with connection:
            if new_database:
                ensure_storage_metadata(connection, role="archive_data")
            else:
                require_current_archive_identity(connection, self.db_path)
            self._create_archive_pages_table(connection)
            self._create_post_versions_table(connection)
            self._create_post_latest_metadata_table(connection)
            self._create_post_version_selections_table(connection)
            self._create_floor_map_tables(connection)
            self._create_post_overlays_table(connection)
            self._create_archive_change_state_table(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_post_versions_latest_covering
                ON post_versions(lou, last_seen_at DESC, id DESC, pid)
                """
            )
            if new_database:
                connection.execute(
                    f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}"
                )
            metadata = require_current_archive_schema(connection, self.db_path)
            self._store_id = metadata.store_id

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

    def read_current_archive_change_state(self) -> ArchiveChangeState:
        with closing(self.connect_read()) as connection:
            return self._read_archive_change_state(connection)

    @staticmethod
    def increment_archive_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE archive_change_state
            SET archive_revision = archive_revision + 1
            WHERE singleton = 1
            """
        )

    @staticmethod
    def increment_floor_map_revision(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            UPDATE archive_change_state
            SET floor_map_revision = floor_map_revision + 1
            WHERE singleton = 1
            """
        )

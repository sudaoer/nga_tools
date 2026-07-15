from __future__ import annotations

import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
)
from nga_tools.storage import ensure_storage_metadata, read_storage_metadata


ARCHIVE_STATE_DB_FILENAME = "archive_state.sqlite3"
ARCHIVE_CACHE_DB_FILENAME = "archive_cache.sqlite3"


def _open_writable(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        path,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    configure_connection(connection)
    return connection


def _open_readonly(path: Path) -> sqlite3.Connection:
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        database_uri,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        uri=True,
    )
    configure_readonly_connection(connection)
    return connection


def _quarantine_database(path: Path) -> None:
    quarantine_token = uuid.uuid4().hex
    for suffix in ("", "-wal", "-shm"):
        source = Path(f"{path}{suffix}")
        if not source.exists():
            continue
        target = path.with_name(
            f"{path.name}.corrupt-{quarantine_token}{suffix}"
        )
        source.replace(target)


def _validate_source_binding(
    connection: sqlite3.Connection,
    *,
    expected_role: str,
    source_store_id: str,
) -> None:
    metadata = read_storage_metadata(connection)
    if metadata is None:
        raise ValueError(f"{expected_role}缺少storage_metadata。")
    if metadata.role != expected_role:
        raise ValueError(
            f"SQLite存储角色不匹配：期望{expected_role}，实际{metadata.role}。"
        )
    if metadata.source_store_id != source_store_id:
        raise ValueError(
            f"{expected_role}不属于当前archive："
            f"{metadata.source_store_id!r} != {source_store_id!r}。"
        )


class ThreadArchiveStateStore:
    def __init__(self, thread_folder: Path) -> None:
        self.db_path = thread_folder / ARCHIVE_STATE_DB_FILENAME
        self._schema_initialized_for: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def connect_write(self, source_store_id: str) -> sqlite3.Connection:
        connection = _open_writable(self.db_path)
        if self._schema_initialized_for != source_store_id:
            try:
                self._ensure_schema(connection, source_store_id)
            except BaseException:
                connection.close()
                raise
            self._schema_initialized_for = source_store_id
        return connection

    def connect_read(self, source_store_id: str) -> sqlite3.Connection:
        if not self.exists():
            raise FileNotFoundError(f"缺少archive_state.sqlite3：{self.db_path}")
        connection = _open_readonly(self.db_path)
        try:
            _validate_source_binding(
                connection,
                expected_role="archive_state",
                source_store_id=source_store_id,
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def ensure_schema(self, source_store_id: str) -> None:
        with closing(self.connect_write(source_store_id)):
            pass

    def recreate_after_error(self, source_store_id: str) -> None:
        _quarantine_database(self.db_path)
        self._schema_initialized_for = None
        self.ensure_schema(source_store_id)

    @staticmethod
    def _ensure_schema(
        connection: sqlite3.Connection,
        source_store_id: str,
    ) -> None:
        ensure_storage_metadata(
            connection,
            role="archive_state",
            source_store_id=source_store_id,
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_floor_processing_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL,
                processed_floor_map_revision INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                author_total_lou_count INTEGER,
                floor_map_format_version INTEGER NOT NULL,
                floor_map_generation_version INTEGER NOT NULL,
                floor_map_hash_algorithm TEXT NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_image_reference_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL,
                post_overlays_fingerprint TEXT NOT NULL,
                post_version_selections_fingerprint TEXT NOT NULL,
                image_reference_extractor_version INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_image_reference_manifest_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_image_reference_manifest_posts (
                lou INTEGER PRIMARY KEY CHECK(lou >= 0),
                cache_key TEXT NOT NULL CHECK(cache_key != '')
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_image_reference_manifest_entries (
                lou INTEGER NOT NULL CHECK(lou >= 0),
                image_index INTEGER NOT NULL CHECK(image_index > 0),
                url TEXT NOT NULL CHECK(url != ''),
                valid INTEGER NOT NULL CHECK(valid IN (0, 1)),
                PRIMARY KEY(lou, image_index)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_image_reference_manifest_urls (
                url TEXT PRIMARY KEY CHECK(url != ''),
                reference_count INTEGER NOT NULL CHECK(reference_count > 0),
                valid INTEGER NOT NULL CHECK(valid IN (0, 1))
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_image_reference_manifest_entries_url
            ON backup_image_reference_manifest_entries(url)
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_pending_images (
                url TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                failure_kind TEXT,
                http_status INTEGER
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_audio_processing_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                format_version INTEGER NOT NULL,
                extractor_version INTEGER NOT NULL,
                processed_max_post_version_id INTEGER NOT NULL
                    CHECK(processed_max_post_version_id >= 0),
                completed_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_pending_audio (
                url TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                failure_kind TEXT,
                http_status INTEGER
            )
            """
        )
        connection.commit()


class ThreadArchiveCacheStore:
    def __init__(self, thread_folder: Path) -> None:
        self.db_path = thread_folder / ARCHIVE_CACHE_DB_FILENAME
        self._schema_initialized_for: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def connect_write(self, source_store_id: str) -> sqlite3.Connection:
        connection = _open_writable(self.db_path)
        if self._schema_initialized_for != source_store_id:
            try:
                self._ensure_schema(connection, source_store_id)
            except BaseException:
                connection.close()
                raise
            self._schema_initialized_for = source_store_id
        return connection

    def connect_read(self, source_store_id: str) -> sqlite3.Connection:
        if not self.exists():
            raise FileNotFoundError(f"缺少archive_cache.sqlite3：{self.db_path}")
        connection = _open_readonly(self.db_path)
        try:
            _validate_source_binding(
                connection,
                expected_role="archive_cache",
                source_store_id=source_store_id,
            )
        except BaseException:
            connection.close()
            raise
        return connection

    def ensure_schema(self, source_store_id: str) -> None:
        with closing(self.connect_write(source_store_id)):
            pass

    def recreate_after_error(self, source_store_id: str) -> None:
        _quarantine_database(self.db_path)
        self._schema_initialized_for = None
        self.ensure_schema(source_store_id)

    @staticmethod
    def _ensure_schema(
        connection: sqlite3.Connection,
        source_store_id: str,
    ) -> None:
        ensure_storage_metadata(
            connection,
            role="archive_cache",
            source_store_id=source_store_id,
        )
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
        connection.commit()

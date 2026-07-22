from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    sqlite_operation,
)
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import (
    require_exact_columns,
    require_index_names,
    require_table_names,
    table_names,
)


ARCHIVE_STATE_DB_FILENAME = "archive_state.sqlite3"
ARCHIVE_CACHE_DB_FILENAME = "archive_cache.sqlite3"

_STATE_COLUMNS = {
    "backup_current_pagination_state": (
        ("singleton", "INTEGER"), ("page_count", "INTEGER"),
        ("author_total_lou_count", "INTEGER"),
        ("source_page_number", "INTEGER"), ("observed_at", "TEXT"),
    ),
    "backup_floor_processing_state": (
        ("singleton", "INTEGER"), ("format_version", "INTEGER"),
        ("processed_archive_revision", "INTEGER"),
        ("processed_floor_map_revision", "INTEGER"), ("page_count", "INTEGER"),
        ("author_total_lou_count", "INTEGER"),
        ("floor_map_format_version", "INTEGER"),
        ("floor_map_generation_version", "INTEGER"),
        ("floor_map_hash_algorithm", "TEXT"), ("completed_at", "TEXT"),
    ),
    "backup_image_reference_state": (
        ("singleton", "INTEGER"), ("format_version", "INTEGER"),
        ("processed_archive_revision", "INTEGER"),
        ("post_overlays_fingerprint", "TEXT"),
        ("post_version_selections_fingerprint", "TEXT"),
        ("image_reference_extractor_version", "INTEGER"),
        ("completed_at", "TEXT"),
    ),
    "backup_image_reference_manifest_state": (
        ("singleton", "INTEGER"), ("format_version", "INTEGER"),
        ("processed_archive_revision", "INTEGER"),
    ),
    "backup_image_reference_manifest_posts": (
        ("lou", "INTEGER"), ("cache_key", "TEXT"),
    ),
    "backup_image_reference_manifest_entries": (
        ("lou", "INTEGER"), ("image_index", "INTEGER"),
        ("url", "TEXT"), ("valid", "INTEGER"),
    ),
    "backup_image_reference_manifest_urls": (
        ("url", "TEXT"), ("reference_count", "INTEGER"),
        ("valid", "INTEGER"),
    ),
    "backup_pending_images": (
        ("url", "TEXT"), ("last_attempt_at", "TEXT"),
        ("failure_kind", "TEXT"), ("http_status", "INTEGER"),
    ),
    "backup_pending_missing_floors": (
        ("author_lou", "INTEGER"), ("last_attempt_at", "TEXT"),
    ),
    "backup_audio_processing_state": (
        ("singleton", "INTEGER"), ("format_version", "INTEGER"),
        ("extractor_version", "INTEGER"),
        ("processed_max_post_version_id", "INTEGER"),
        ("completed_at", "TEXT"),
    ),
    "backup_pending_audio": (
        ("url", "TEXT"), ("last_attempt_at", "TEXT"),
        ("failure_kind", "TEXT"), ("http_status", "INTEGER"),
    ),
}
_CACHE_COLUMNS = {
    "post_image_reference_cache": (
        ("cache_key", "TEXT"), ("source_hash", "TEXT"),
        ("extractor_version", "INTEGER"), ("references_json", "TEXT"),
        ("created_at", "TEXT"), ("updated_at", "TEXT"),
    ),
}


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


def _validate_source_binding(
    connection: sqlite3.Connection,
    *,
    expected_role: str,
    source_store_id: str,
) -> None:
    role = "archive_state" if expected_role == "archive_state" else "archive_cache"
    require_storage_metadata(
        connection,
        role=role,
        source_store_id=source_store_id,
    )


def _require_current_schema(
    connection: sqlite3.Connection,
    *,
    role: str,
    source_store_id: str,
    columns_by_table: dict[str, tuple[tuple[str, str], ...]],
) -> None:
    _validate_source_binding(
        connection,
        expected_role=role,
        source_store_id=source_store_id,
    )
    source = f"{role} SQLite"
    require_table_names(
        connection,
        expected={"storage_metadata", *columns_by_table},
        source=source,
    )
    for table_name, columns in columns_by_table.items():
        require_exact_columns(connection, table_name, columns, source=source)
    if role == "archive_state":
        require_index_names(
            connection,
            required={"idx_image_reference_manifest_entries_url"},
            forbidden=set(),
            source=source,
        )


def _schema_objects_present(
    connection: sqlite3.Connection,
    *,
    columns_by_table: dict[str, tuple[tuple[str, str], ...]],
    required_indexes: set[str],
) -> bool:
    if table_names(connection) != {"storage_metadata", *columns_by_table}:
        return False
    index_names = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
            """
        )
        if isinstance(row[0], str)
    }
    return required_indexes <= index_names


def require_current_archive_state_schema(
    connection: sqlite3.Connection,
    source_store_id: str,
) -> None:
    _require_current_schema(
        connection,
        role="archive_state",
        source_store_id=source_store_id,
        columns_by_table=_STATE_COLUMNS,
    )


def require_current_archive_cache_schema(
    connection: sqlite3.Connection,
    source_store_id: str,
) -> None:
    _require_current_schema(
        connection,
        role="archive_cache",
        source_store_id=source_store_id,
        columns_by_table=_CACHE_COLUMNS,
    )


class ThreadArchiveStateStore:
    def __init__(self, thread_folder: Path) -> None:
        self.db_path = thread_folder / ARCHIVE_STATE_DB_FILENAME
        self._schema_initialized_for: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def connect_write(self, source_store_id: str) -> sqlite3.Connection:
        with sqlite_operation("write"):
            new_database = not self.db_path.is_file()
            if not new_database and self._schema_initialized_for != source_store_id:
                with closing(_open_readonly(self.db_path)) as read_connection:
                    if _schema_objects_present(
                        read_connection,
                        columns_by_table=_STATE_COLUMNS,
                        required_indexes={
                            "idx_image_reference_manifest_entries_url"
                        },
                    ):
                        _require_current_schema(
                            read_connection,
                            role="archive_state",
                            source_store_id=source_store_id,
                            columns_by_table=_STATE_COLUMNS,
                        )
                        self._schema_initialized_for = source_store_id
            connection = _open_writable(self.db_path)
            if self._schema_initialized_for != source_store_id:
                try:
                    with connection:
                        if new_database:
                            ensure_storage_metadata(
                                connection,
                                role="archive_state",
                                source_store_id=source_store_id,
                            )
                        else:
                            _validate_source_binding(
                                connection,
                                expected_role="archive_state",
                                source_store_id=source_store_id,
                            )
                        self._create_schema_objects(connection)
                        _require_current_schema(
                            connection,
                            role="archive_state",
                            source_store_id=source_store_id,
                            columns_by_table=_STATE_COLUMNS,
                        )
                except BaseException:
                    connection.close()
                    raise
                self._schema_initialized_for = source_store_id
            return connection

    def connect_read(self, source_store_id: str) -> sqlite3.Connection:
        with sqlite_operation("read"):
            if not self.exists():
                raise FileNotFoundError(f"缺少archive_state.sqlite3：{self.db_path}")
            connection = _open_readonly(self.db_path)
            try:
                _require_current_schema(
                    connection,
                    role="archive_state",
                    source_store_id=source_store_id,
                    columns_by_table=_STATE_COLUMNS,
                )
            except BaseException:
                connection.close()
                raise
            return connection

    def open_session_write(self) -> sqlite3.Connection:
        return _open_writable(self.db_path)

    def open_session_read(self) -> sqlite3.Connection:
        if not self.exists():
            raise FileNotFoundError(f"缺少archive_state.sqlite3：{self.db_path}")
        return _open_readonly(self.db_path)

    def ensure_schema(self, source_store_id: str) -> None:
        with closing(self.connect_write(source_store_id)):
            pass

    @staticmethod
    def _create_schema_objects(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS backup_current_pagination_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                page_count INTEGER NOT NULL CHECK(page_count >= 1),
                author_total_lou_count INTEGER
                    CHECK(author_total_lou_count IS NULL
                          OR author_total_lou_count >= 0),
                source_page_number INTEGER NOT NULL
                    CHECK(source_page_number >= 1),
                observed_at TEXT NOT NULL CHECK(observed_at != '')
            )
            """
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
            CREATE TABLE IF NOT EXISTS backup_pending_missing_floors (
                author_lou INTEGER PRIMARY KEY CHECK(author_lou >= 0),
                last_attempt_at TEXT NOT NULL CHECK(last_attempt_at != '')
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


class ThreadArchiveCacheStore:
    def __init__(self, thread_folder: Path) -> None:
        self.db_path = thread_folder / ARCHIVE_CACHE_DB_FILENAME
        self._schema_initialized_for: str | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def connect_write(self, source_store_id: str) -> sqlite3.Connection:
        with sqlite_operation("write"):
            new_database = not self.db_path.is_file()
            if not new_database and self._schema_initialized_for != source_store_id:
                with closing(_open_readonly(self.db_path)) as read_connection:
                    if _schema_objects_present(
                        read_connection,
                        columns_by_table=_CACHE_COLUMNS,
                        required_indexes=set(),
                    ):
                        _require_current_schema(
                            read_connection,
                            role="archive_cache",
                            source_store_id=source_store_id,
                            columns_by_table=_CACHE_COLUMNS,
                        )
                        self._schema_initialized_for = source_store_id
            connection = _open_writable(self.db_path)
            if self._schema_initialized_for != source_store_id:
                try:
                    with connection:
                        if new_database:
                            ensure_storage_metadata(
                                connection,
                                role="archive_cache",
                                source_store_id=source_store_id,
                            )
                        else:
                            _validate_source_binding(
                                connection,
                                expected_role="archive_cache",
                                source_store_id=source_store_id,
                            )
                        self._create_schema_objects(connection)
                        _require_current_schema(
                            connection,
                            role="archive_cache",
                            source_store_id=source_store_id,
                            columns_by_table=_CACHE_COLUMNS,
                        )
                except BaseException:
                    connection.close()
                    raise
                self._schema_initialized_for = source_store_id
            return connection

    def connect_read(self, source_store_id: str) -> sqlite3.Connection:
        with sqlite_operation("read"):
            if not self.exists():
                raise FileNotFoundError(f"缺少archive_cache.sqlite3：{self.db_path}")
            connection = _open_readonly(self.db_path)
            try:
                _require_current_schema(
                    connection,
                    role="archive_cache",
                    source_store_id=source_store_id,
                    columns_by_table=_CACHE_COLUMNS,
                )
            except BaseException:
                connection.close()
                raise
            return connection

    def open_session_write(self) -> sqlite3.Connection:
        return _open_writable(self.db_path)

    def open_session_read(self) -> sqlite3.Connection:
        if not self.exists():
            raise FileNotFoundError(f"缺少archive_cache.sqlite3：{self.db_path}")
        return _open_readonly(self.db_path)

    def ensure_schema(self, source_store_id: str) -> None:
        with closing(self.connect_write(source_store_id)):
            pass

    @staticmethod
    def _create_schema_objects(connection: sqlite3.Connection) -> None:
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

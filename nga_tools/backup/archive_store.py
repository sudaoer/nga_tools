from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager, contextmanager, closing
from pathlib import Path
from typing import Literal, Optional, cast

from nga_tools.backup.archive_cache_store import ArchiveCacheRepository
from nga_tools.backup.archive_floor_store import ArchiveFloorMapRepository
from nga_tools.backup.archive_ingest_store import ArchiveIngestRepository
from nga_tools.backup.archive_overlay_store import ArchiveOverlayRepository
from nga_tools.backup.archive_post_store import ArchivePostRepository
from nga_tools.backup.archive_state_store import ArchiveStateRepository
from nga_tools.backup.archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    ARCHIVE_FORBIDDEN_INDEXES,
    ARCHIVE_TABLES,
    ARCHIVE_WRITE_INDEXES,
    require_archive_write_ready_schema,
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
    sqlite_operation,
)
from nga_tools.storage import ensure_storage_metadata
from nga_tools.storage.schema import table_names

ARCHIVE_DB_FILENAME = "archive.sqlite3"


class _ArchiveConnectionSession:
    def __init__(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._connections: dict[str, sqlite3.Connection] = {}
        self._read_validated_databases: set[str] = set()
        self._write_ready_databases: set[str] = set()

    def require_owner(self) -> None:
        if threading.get_ident() != self._owner_thread_id:
            raise RuntimeError("archive连接会话不能跨线程使用。")

    def connection(
        self,
        *,
        key: str,
        database: str,
        validation: Literal["read", "write"],
        read_is_write_ready: bool,
        validated_factory: Callable[[], sqlite3.Connection],
        unchecked_factory: Callable[[], sqlite3.Connection],
    ) -> sqlite3.Connection:
        self.require_owner()
        existing = self._connections.get(key)
        if existing is not None:
            return existing
        already_validated = (
            database in self._write_ready_databases
            or (
                validation == "read"
                and database in self._read_validated_databases
            )
        )
        factory = unchecked_factory if already_validated else validated_factory
        connection = factory()
        self._connections[key] = connection
        self._read_validated_databases.add(database)
        if validation == "write" or read_is_write_ready:
            self._write_ready_databases.add(database)
        return connection

    def close(self) -> None:
        self.require_owner()
        first_error: sqlite3.Error | None = None
        for connection in reversed(tuple(self._connections.values())):
            try:
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error as error:
                if first_error is None:
                    first_error = error
            try:
                connection.close()
            except sqlite3.Error as error:
                if first_error is None:
                    first_error = error
        self._connections.clear()
        if first_error is not None:
            raise first_error


class ThreadArchiveStore:
    def __init__(
        self,
        thread_folder: Path,
    ) -> None:
        self.thread_folder = thread_folder
        self.db_path = thread_folder / ARCHIVE_DB_FILENAME
        self._state_store = ThreadArchiveStateStore(thread_folder)
        self._cache_store = ThreadArchiveCacheStore(thread_folder)
        self.overlays = ArchiveOverlayRepository(self)
        self.floor_maps = ArchiveFloorMapRepository(self)
        self.ingest = ArchiveIngestRepository(self)
        self.posts = ArchivePostRepository(self, self.floor_maps)
        self.state = ArchiveStateRepository(self, self._state_store, self.posts)
        self.cache = ArchiveCacheRepository(self, self._cache_store)
        self._schema_initialized = False
        self._store_id: str | None = None
        self._connection_session: _ArchiveConnectionSession | None = None

    def exists(self) -> bool:
        return self.db_path.is_file()

    def require_exists(self) -> None:
        if not self.exists():
            raise RuntimeError(
                f"缺少archive.sqlite3：{self.db_path}。请重新运行备份初始化。"
            )

    def _open_write_connection(self) -> sqlite3.Connection:
        self.thread_folder.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(
            self.db_path,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        )
        configure_connection(connection)
        return connection

    def connect_write(self) -> sqlite3.Connection:
        with sqlite_operation():
            new_database = not self.db_path.is_file()
            if (
                not new_database
                and not self._schema_initialized
                and self._existing_archive_is_write_ready()
            ):
                self._schema_initialized = True
            connection = self._open_write_connection()
            if not self._schema_initialized:
                try:
                    self._ensure_schema(connection, new_database=new_database)
                except BaseException:
                    connection.close()
                    raise
                self._schema_initialized = True
            return connection

    def _existing_archive_is_write_ready(self) -> bool:
        with closing(self._open_read_connection()) as connection:
            if table_names(connection) != ARCHIVE_TABLES:
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
            if (
                not ARCHIVE_WRITE_INDEXES <= index_names
                or ARCHIVE_FORBIDDEN_INDEXES & index_names
            ):
                return False
            metadata = require_archive_write_ready_schema(
                connection,
                self.db_path,
            )
            self._store_id = metadata.store_id
            return True

    def _open_read_connection(self) -> sqlite3.Connection:
        self.require_exists()
        database_uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        connection = sqlite3.connect(
            database_uri,
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
        return connection

    def connect_read(self) -> sqlite3.Connection:
        with sqlite_operation():
            connection = self._open_read_connection()
            try:
                metadata = require_current_archive_schema(connection, self.db_path)
                self._store_id = metadata.store_id
            except BaseException:
                connection.close()
                raise
            return connection

    @contextmanager
    def _borrow_connection(
        self,
        *,
        key: str,
        database: str,
        validation: Literal["read", "write"],
        read_is_write_ready: bool = False,
        validated_factory: Callable[[], sqlite3.Connection],
        unchecked_factory: Callable[[], sqlite3.Connection],
    ) -> Generator[sqlite3.Connection]:
        with sqlite_operation():
            session = self._connection_session
            if session is None:
                with closing(validated_factory()) as connection:
                    yield connection
                return
            connection = session.connection(
                key=key,
                database=database,
                validation=validation,
                read_is_write_ready=read_is_write_ready,
                validated_factory=validated_factory,
                unchecked_factory=unchecked_factory,
            )
            yield connection

    def read_connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._borrow_connection(
            key="archive_read",
            database="archive",
            validation="read",
            validated_factory=self.connect_read,
            unchecked_factory=self._open_read_connection,
        )

    def write_connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._borrow_connection(
            key="archive_write",
            database="archive",
            validation="write",
            validated_factory=self.connect_write,
            unchecked_factory=self._open_write_connection,
        )

    def state_read_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        source_store_id = self.archive_store_id()
        return self._borrow_connection(
            key="state_read",
            database="state",
            validation="read",
            read_is_write_ready=True,
            validated_factory=lambda: self._state_store.connect_read(
                source_store_id
            ),
            unchecked_factory=self._state_store.open_session_read,
        )

    def state_write_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        source_store_id = self.archive_store_id()
        return self._borrow_connection(
            key="state_write",
            database="state",
            validation="write",
            validated_factory=lambda: self._state_store.connect_write(
                source_store_id
            ),
            unchecked_factory=self._state_store.open_session_write,
        )

    def cache_read_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        source_store_id = self.archive_store_id()
        return self._borrow_connection(
            key="cache_read",
            database="cache",
            validation="read",
            read_is_write_ready=True,
            validated_factory=lambda: self._cache_store.connect_read(
                source_store_id
            ),
            unchecked_factory=self._cache_store.open_session_read,
        )

    def cache_write_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        source_store_id = self.archive_store_id()
        return self._borrow_connection(
            key="cache_write",
            database="cache",
            validation="write",
            validated_factory=lambda: self._cache_store.connect_write(
                source_store_id
            ),
            unchecked_factory=self._cache_store.open_session_write,
        )

    @contextmanager
    def connection_session(self) -> Generator[None]:
        current = self._connection_session
        if current is not None:
            current.require_owner()
            yield
            return
        session = _ArchiveConnectionSession()
        self._connection_session = session
        body_failed = False
        try:
            yield
        except BaseException:
            body_failed = True
            raise
        finally:
            self._connection_session = None
            try:
                with sqlite_operation():
                    session.close()
            except sqlite3.Error:
                if not body_failed:
                    raise

    def archive_store_id(self) -> str:
        if self._store_id is not None:
            return self._store_id
        with self.read_connection():
            pass
        if self._store_id is None:
            raise RuntimeError(f"archive无法读取store_id：{self.db_path}")
        return self._store_id

    def ensure_schema(self) -> None:
        with self.write_connection():
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
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_floor_map_entries_unresolved
                ON floor_map_entries(author_lou)
                WHERE pid IS NULL AND original_pid IS NULL
                """
            )
            if new_database:
                connection.execute(
                    f"PRAGMA user_version = {ARCHIVE_SCHEMA_VERSION}"
                )
            metadata = require_archive_write_ready_schema(connection, self.db_path)
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
        with self.read_connection() as connection:
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

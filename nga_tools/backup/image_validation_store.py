from __future__ import annotations

import datetime
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import TypedDict

import nga_tools.config as config
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import require_exact_columns, require_table_names

IMAGE_CACHE_FILENAME = "image_cache.sqlite3"
_IMAGE_CACHE_COLUMNS = (
    ("relative_path", "TEXT"),
    ("size", "INTEGER"),
    ("mtime_ns", "INTEGER"),
    ("valid", "INTEGER"),
    ("updated_at", "TEXT"),
)
_IMAGE_CACHE_LOCK = threading.RLock()
_INITIALIZED_IMAGE_CACHE_PATHS: set[Path] = set()


class PersistentValidationEntry(TypedDict):
    canonical_path: str
    size: int
    mtime_ns: int
    valid: bool


def _configured_output_root() -> Path:
    return Path(config.get_config().output_dir)


def image_cache_path(output_root: Path | None = None) -> Path:
    root = _configured_output_root() if output_root is None else output_root
    return root / IMAGE_CACHE_FILENAME


def require_current_image_cache(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    source = f"image_cache {db_path}"
    require_storage_metadata(connection, role="image_cache")
    require_table_names(
        connection,
        expected={"storage_metadata", "image_validation_cache"},
        source=source,
    )
    require_exact_columns(
        connection,
        "image_validation_cache",
        _IMAGE_CACHE_COLUMNS,
        source=source,
    )


def _initialize_image_cache() -> Path:
    db_path = image_cache_path().resolve()
    with _IMAGE_CACHE_LOCK:
        if db_path in _INITIALIZED_IMAGE_CACHE_PATHS and db_path.is_file():
            return db_path

        new_database = not db_path.is_file()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        ) as connection:
            configure_connection(connection)
            if new_database:
                ensure_storage_metadata(connection, role="image_cache")
                connection.execute(
                    """
                    CREATE TABLE image_validation_cache (
                        relative_path TEXT PRIMARY KEY,
                        size INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        valid INTEGER NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
            else:
                require_current_image_cache(connection, db_path)
            connection.commit()
        _INITIALIZED_IMAGE_CACHE_PATHS.add(db_path)
    return db_path


def _connect_image_cache_writable() -> sqlite3.Connection:
    connection = sqlite3.connect(
        _initialize_image_cache(),
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    configure_connection(connection)
    return connection


def _connect_image_cache_readonly() -> sqlite3.Connection:
    db_uri = f"{_initialize_image_cache().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        db_uri,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        uri=True,
    )
    configure_readonly_connection(connection)
    return connection


def _validation_storage_key(canonical_path: str) -> str | None:
    try:
        path = Path(canonical_path).resolve(strict=False)
        relative_path = path.relative_to(_configured_output_root().resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative_path.parts or relative_path.parts[0] != "images_unique":
        return None
    return relative_path.as_posix()


def load_persistent_validation_cache(
    canonical_paths: set[str],
) -> dict[str, tuple[int, int, bool]]:
    if not canonical_paths:
        return {}
    canonical_by_storage_key = {
        storage_key: canonical_path
        for canonical_path in canonical_paths
        if (storage_key := _validation_storage_key(canonical_path)) is not None
    }
    sorted_paths = sorted(canonical_by_storage_key)
    if not sorted_paths:
        return {}

    entries: dict[str, tuple[int, int, bool]] = {}
    try:
        with closing(_connect_image_cache_readonly()) as connection:
            for chunk in iter_in_clause_chunks(sorted_paths):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT relative_path, size, mtime_ns, valid
                    FROM image_validation_cache
                    WHERE relative_path IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for path, size, mtime_ns, valid in rows:
                    if (
                        isinstance(path, str)
                        and path in canonical_by_storage_key
                        and type(size) is int
                        and type(mtime_ns) is int
                        and type(valid) is int
                    ):
                        entries[canonical_by_storage_key[path]] = (
                            size,
                            mtime_ns,
                            bool(valid),
                        )
    except (OSError, sqlite3.Error, ValueError):
        return {}
    return entries


def save_persistent_validation_entries(
    entries: list[PersistentValidationEntry],
) -> None:
    if not entries:
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    rows = [
        (storage_key, entry["size"], entry["mtime_ns"], int(entry["valid"]), now)
        for entry in entries
        if (
            storage_key := _validation_storage_key(entry["canonical_path"])
        )
        is not None
    ]
    if not rows:
        return
    with _IMAGE_CACHE_LOCK:
        try:
            with closing(_connect_image_cache_writable()) as connection:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO image_validation_cache (
                            relative_path,
                            size,
                            mtime_ns,
                            valid,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            size = excluded.size,
                            mtime_ns = excluded.mtime_ns,
                            valid = excluded.valid,
                            updated_at = excluded.updated_at
                        """,
                        rows,
                    )
        except (OSError, sqlite3.Error, ValueError):
            pass


def delete_persistent_validation_entry(canonical_path: str) -> None:
    storage_key = _validation_storage_key(canonical_path)
    if storage_key is None:
        return
    with _IMAGE_CACHE_LOCK:
        try:
            with closing(_connect_image_cache_writable()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM image_validation_cache WHERE relative_path = ?",
                        (storage_key,),
                    )
        except (OSError, sqlite3.Error, ValueError):
            pass

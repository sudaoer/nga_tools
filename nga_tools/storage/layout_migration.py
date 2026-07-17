from __future__ import annotations

import datetime
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from nga_tools.backup.archive_schema import (
    ARCHIVE_SCHEMA_VERSION,
    require_current_archive_schema,
)
from nga_tools.backup.archive_store import (
    ARCHIVE_DB_FILENAME,
    PostImageReferenceCacheEntry,
    ThreadArchiveStore,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
)
from nga_tools.backup.image_reference_cache import (
    IMAGE_REFERENCE_EXTRACTOR_VERSION,
    deserialize_image_references,
)
from nga_tools.backup.image_store import (
    IMAGE_CACHE_FILENAME,
    IMAGE_INDEX_FILENAME,
    ensure_image_mappings_schema,
)
from nga_tools.backup.audio_store import AUDIO_INDEX_FILENAME
from nga_tools.backup.processing_state import (
    FLOOR_PROCESSING_STATE_VERSION,
    IMAGE_REFERENCE_MANIFEST_VERSION,
    IMAGE_REFERENCE_STATE_VERSION,
    FloorProcessingState,
    ImageReferenceManifestEntry,
    ImageReferenceManifestPost,
    ImageReferenceState,
    PendingImageRetry,
)
from nga_tools.backup.thread_stores import (
    ARCHIVE_CACHE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
)
from nga_tools.core.atomic import replace_temp_file, write_json_atomically
from nga_tools.core.downloads import DOWNLOAD_FAILURE_KINDS, DownloadFailureKind
from nga_tools.core.output_lock import use_output_folder_lock
from nga_tools.forum.ankebak_state import BACKUP_STATE_DB_FILENAME
from nga_tools.forum.thread_store import FORUM_THREAD_DB_FILENAME
from nga_tools.storage import (
    STORAGE_LAYOUT_VERSION,
    ensure_storage_metadata,
    read_storage_metadata,
)


MIGRATION_BACKUP_DIRNAME = ".migration-backups"
MIGRATION_MANIFEST_FILENAME = "manifest.json"
MIGRATION_MANIFEST_VERSION = 1

_THREAD_DATABASE_FILENAMES = (
    ARCHIVE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
    ARCHIVE_CACHE_DB_FILENAME,
)
_GLOBAL_TARGET_NAME = "@global"
_GLOBAL_DATABASE_FILENAMES = (
    FORUM_THREAD_DB_FILENAME,
    BACKUP_STATE_DB_FILENAME,
    IMAGE_INDEX_FILENAME,
    IMAGE_CACHE_FILENAME,
    AUDIO_INDEX_FILENAME,
)
_LEGACY_ARCHIVE_TABLES = (
    "backup_processing_state",
    "backup_floor_processing_state",
    "backup_image_reference_state",
    "backup_image_reference_manifest_state",
    "backup_image_reference_manifest_posts",
    "backup_image_reference_manifest_entries",
    "backup_image_reference_manifest_urls",
    "backup_pending_images",
    "post_image_reference_cache",
    "backup_image_references",
    "post_observations",
)
_DURABLE_ARCHIVE_TABLES = (
    "archive_pages",
    "post_versions",
    "post_latest_metadata",
    "post_version_selections",
    "post_overlays",
    "floor_map_state",
    "floor_map_entries",
    "floor_map_candidates",
    "archive_change_state",
)


@dataclass(frozen=True)
class ThreadLayoutMigrationStats:
    migrated_floor_state: bool
    migrated_image_state: bool
    migrated_manifest_posts: int
    migrated_pending_images: int
    migrated_cache_entries: int
    page_snapshot_rows_removed: int = 0
    page_snapshot_json_bytes_removed: int = 0
    post_observation_rows_removed: int = 0
    archive_page_rows: int = 0


@dataclass(frozen=True)
class _PageSchemaMigrationStats:
    page_snapshot_rows_removed: int
    page_snapshot_json_bytes_removed: int
    post_observation_rows_removed: int
    archive_page_rows: int
    expected_rows: tuple[tuple[object, object, object, object], ...]


@dataclass(frozen=True)
class LayoutMigrationResult:
    run_id: str
    migrated_count: int
    skipped_count: int
    failures: tuple[tuple[Path, str], ...]


@dataclass(frozen=True)
class LayoutRollbackResult:
    run_id: str
    restored_count: int


class _Hasher(Protocol):
    def update(self, data: bytes, /) -> object: ...


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_run_id() -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    return (
        connection.execute(
            """
            SELECT 1 FROM sqlite_schema
            WHERE type = 'table' AND name = ?
            """,
            (table_name,),
        ).fetchone()
        is not None
    )


def _quick_check(path: Path) -> None:
    database_uri = f"{path.resolve().as_uri()}?mode=ro"
    with closing(sqlite3.connect(database_uri, uri=True)) as connection:
        row = connection.execute("PRAGMA quick_check").fetchone()
    if row != ("ok",):
        raise ValueError(f"SQLite quick_check失败：{path}：{row!r}")


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise FileNotFoundError(f"缺少待快照SQLite：{source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp_path = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        source_uri = f"{source.resolve().as_uri()}?mode=ro"
        with (
            closing(sqlite3.connect(source_uri, uri=True)) as source_connection,
            closing(sqlite3.connect(temp_path)) as destination_connection,
        ):
            source_connection.backup(destination_connection)
        _quick_check(temp_path)
        replace_temp_file(temp_path, destination)
    finally:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass


def _remove_sqlite_file(path: Path) -> None:
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(f"{path}{suffix}").unlink()
        except FileNotFoundError:
            pass


def _finalize_sqlite(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.commit()
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        foreign_key_rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    if quick_check != ("ok",):
        raise ValueError(f"SQLite quick_check失败：{path}：{quick_check!r}")
    if foreign_key_rows:
        raise ValueError(
            f"SQLite foreign_key_check失败：{path}：{foreign_key_rows[:5]!r}"
        )


def _hash_value(digest: _Hasher, value: object) -> None:
    if value is None:
        digest.update(b"n;")
        return
    if type(value) is int:
        payload = str(value).encode("ascii")
        prefix = b"i"
    elif isinstance(value, float):
        payload = value.hex().encode("ascii")
        prefix = b"f"
    elif isinstance(value, str):
        payload = value.encode("utf-8")
        prefix = b"s"
    elif isinstance(value, bytes):
        payload = value
        prefix = b"b"
    else:
        raise ValueError(f"SQLite字段类型不受支持：{type(value).__name__}")
    digest.update(prefix)
    digest.update(str(len(payload)).encode("ascii"))
    digest.update(b":")
    digest.update(payload)
    digest.update(b";")


def _table_fingerprint(
    connection: sqlite3.Connection,
    table_name: str,
) -> str:
    if not _table_exists(connection, table_name):
        return "missing"
    columns = connection.execute(f'PRAGMA table_info("{table_name}")').fetchall()
    column_names = [
        row[1] for row in columns if len(row) > 1 and isinstance(row[1], str)
    ]
    if not column_names:
        raise ValueError(f"SQLite表没有字段：{table_name}")
    primary_key_columns = [
        (row[5], row[1])
        for row in columns
        if len(row) > 5
        and type(row[5]) is int
        and row[5] > 0
        and isinstance(row[1], str)
    ]
    order_names = [
        name for _index, name in sorted(primary_key_columns)
    ] or column_names
    quoted_columns = ", ".join(f'"{name}"' for name in column_names)
    quoted_order = ", ".join(f'"{name}"' for name in order_names)
    cursor = connection.execute(
        f'SELECT {quoted_columns} FROM "{table_name}" ORDER BY {quoted_order}'
    )
    hasher = hashlib.sha256()
    hasher.update(table_name.encode("utf-8"))
    for row in cursor:
        hasher.update(b"[")
        for value in row:
            _hash_value(hasher, value)
        hasher.update(b"]")
    return hasher.hexdigest()


def _durable_fingerprints(path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            table_name: _table_fingerprint(connection, table_name)
            for table_name in _DURABLE_ARCHIVE_TABLES
        }


def _read_page_schema_migration_stats(
    path: Path,
) -> _PageSchemaMigrationStats:
    with closing(sqlite3.connect(path)) as connection:
        has_snapshots = _table_exists(connection, "page_snapshots")
        has_compact_pages = _table_exists(connection, "archive_pages")
        if has_snapshots and has_compact_pages:
            raise ValueError(
                f"archive同时包含page_snapshots与archive_pages：{path}"
            )

        page_snapshot_rows = 0
        page_snapshot_json_bytes = 0
        if has_snapshots:
            snapshot_stats = connection.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(LENGTH(page_json)), 0)
                FROM page_snapshots
                """
            ).fetchone()
            if (
                snapshot_stats is None
                or type(snapshot_stats[0]) is not int
                or type(snapshot_stats[1]) is not int
            ):
                raise ValueError(f"archive page_snapshots统计无效：{path}")
            page_snapshot_rows, page_snapshot_json_bytes = snapshot_stats
            expected_rows = tuple(
                connection.execute(
                    """
                    SELECT page_number, total_page, vrows, last_seen_at
                    FROM (
                        SELECT
                            page_number,
                            total_page,
                            vrows,
                            last_seen_at,
                            ROW_NUMBER() OVER (
                                PARTITION BY page_number
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM page_snapshots
                    )
                    WHERE row_number = 1
                    ORDER BY page_number
                    """
                ).fetchall()
            )
        elif has_compact_pages:
            expected_rows = tuple(
                connection.execute(
                    """
                    SELECT page_number, total_page, vrows, last_seen_at
                    FROM archive_pages
                    ORDER BY page_number
                    """
                ).fetchall()
            )
        else:
            expected_rows = ()

        post_observation_rows = 0
        if _table_exists(connection, "post_observations"):
            observation_row = connection.execute(
                "SELECT COUNT(*) FROM post_observations"
            ).fetchone()
            if observation_row is None or type(observation_row[0]) is not int:
                raise ValueError(f"archive post_observations统计无效：{path}")
            post_observation_rows = observation_row[0]

    return _PageSchemaMigrationStats(
        page_snapshot_rows_removed=page_snapshot_rows,
        page_snapshot_json_bytes_removed=page_snapshot_json_bytes,
        post_observation_rows_removed=post_observation_rows,
        archive_page_rows=len(expected_rows),
        expected_rows=expected_rows,
    )


def _validate_page_schema_migration(
    path: Path,
    stats: _PageSchemaMigrationStats,
) -> None:
    with closing(sqlite3.connect(path)) as connection:
        require_current_archive_schema(connection, path)
        actual_rows = tuple(
            connection.execute(
                """
                SELECT page_number, total_page, vrows, last_seen_at
                FROM archive_pages
                ORDER BY page_number
                """
            ).fetchall()
        )
    if actual_rows != stats.expected_rows:
        raise ValueError(f"archive分页状态迁移前后不一致：{path}")


def _drop_legacy_archive_tables(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        with connection:
            for table_name in _LEGACY_ARCHIVE_TABLES:
                connection.execute(f'DROP TABLE IF EXISTS "{table_name}"')
        connection.execute("VACUUM")


def _archive_is_clean_layout(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path)) as connection:
            metadata = read_storage_metadata(connection)
            if (
                metadata is None
                or metadata.role != "archive_data"
                or metadata.layout_version != STORAGE_LAYOUT_VERSION
            ):
                return False
            require_current_archive_schema(connection, path)
            return not any(
                _table_exists(connection, table_name)
                for table_name in _LEGACY_ARCHIVE_TABLES
            )
    except (sqlite3.Error, ValueError):
        return False


def _thread_layout_is_complete(thread_folder: Path) -> bool:
    data_path = thread_folder / ARCHIVE_DB_FILENAME
    if not _archive_is_clean_layout(data_path):
        return False
    try:
        store = ThreadArchiveStore(thread_folder)
        source_store_id = store.archive_store_id()
        with (
            closing(store.state_store.connect_read(source_store_id)),
            closing(store.cache_store.connect_read(source_store_id)),
        ):
            return True
    except (OSError, sqlite3.Error, ValueError):
        return False


def _database_has_role(
    path: Path,
    role: str,
    *,
    forbidden_tables: tuple[str, ...] = (),
) -> bool:
    if not path.is_file():
        return False
    try:
        with closing(sqlite3.connect(path)) as connection:
            metadata = read_storage_metadata(connection)
            return (
                metadata is not None
                and metadata.role == role
                and metadata.layout_version == STORAGE_LAYOUT_VERSION
                and not any(
                    _table_exists(connection, table_name)
                    for table_name in forbidden_tables
                )
            )
    except (sqlite3.Error, ValueError):
        return False


def _global_layout_is_complete(output_root: Path) -> bool:
    forum_path = output_root / FORUM_THREAD_DB_FILENAME
    backup_state_path = output_root / BACKUP_STATE_DB_FILENAME
    image_index_path = output_root / IMAGE_INDEX_FILENAME
    image_cache_path = output_root / IMAGE_CACHE_FILENAME
    audio_index_path = output_root / AUDIO_INDEX_FILENAME
    forum_complete = not forum_path.exists() or (
        _database_has_role(
            forum_path,
            "forum_data",
            forbidden_tables=("ankebak_thread_state",),
        )
        and _database_has_role(backup_state_path, "backup_state")
    )
    if backup_state_path.exists() and not _database_has_role(
        backup_state_path,
        "backup_state",
    ):
        forum_complete = False
    image_complete = not image_index_path.exists() or (
        _database_has_role(
            image_index_path,
            "image_index",
            forbidden_tables=("image_validation_cache",),
        )
        and _database_has_role(image_cache_path, "image_cache")
    )
    if image_cache_path.exists() and not _database_has_role(
        image_cache_path,
        "image_cache",
    ):
        image_complete = False
    audio_complete = not audio_index_path.exists() or _database_has_role(
        audio_index_path,
        "audio_index",
    )
    return forum_complete and image_complete and audio_complete


def _dynamic_table_fingerprints(
    path: Path,
    table_prefix: str,
) -> dict[str, str]:
    with closing(sqlite3.connect(path)) as connection:
        table_names = [
            row[0]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_schema
                WHERE type = 'table' AND name LIKE ?
                ORDER BY name
                """,
                (f"{table_prefix}%",),
            )
            if isinstance(row[0], str)
        ]
        return {
            table_name: _table_fingerprint(connection, table_name)
            for table_name in table_names
        }


def _ensure_backup_state_schema(connection: sqlite3.Connection) -> None:
    ensure_storage_metadata(connection, role="backup_state")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS ankebak_thread_state (
            target_key TEXT PRIMARY KEY,
            tid INTEGER NOT NULL,
            aid INTEGER,
            forum_replies INTEGER,
            forum_lastpost INTEGER,
            last_backup_success_at TEXT NOT NULL,
            last_full_backup_success_at TEXT
        )
        """
    )
    connection.commit()


def _valid_timestamp_text(value: object, *, optional: bool = False) -> bool:
    if value is None:
        return optional
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _copy_ankebak_state(
    source_path: Path,
    destination_path: Path,
) -> int:
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination_path)) as destination:
        _ensure_backup_state_schema(destination)
        if not source_path.is_file():
            return 0
        with closing(sqlite3.connect(source_path)) as source:
            if not _table_exists(source, "ankebak_thread_state"):
                return 0
            rows = source.execute(
                """
                SELECT target_key, tid, aid, forum_replies, forum_lastpost,
                       last_backup_success_at, last_full_backup_success_at
                FROM ankebak_thread_state ORDER BY target_key
                """
            ).fetchall()
        valid_rows: list[tuple[object, ...]] = []
        for row in rows:
            (
                target_key,
                tid,
                aid,
                forum_replies,
                forum_lastpost,
                last_backup_success_at,
                last_full_backup_success_at,
            ) = row
            if (
                not isinstance(target_key, str)
                or not target_key
                or type(tid) is not int
                or (aid is not None and type(aid) is not int)
                or (forum_replies is not None and type(forum_replies) is not int)
                or (forum_lastpost is not None and type(forum_lastpost) is not int)
                or not _valid_timestamp_text(last_backup_success_at)
                or not _valid_timestamp_text(
                    last_full_backup_success_at,
                    optional=True,
                )
            ):
                continue
            valid_rows.append(tuple(row))
        with destination:
            destination.executemany(
                """
                INSERT INTO ankebak_thread_state VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(target_key) DO UPDATE SET
                    tid = excluded.tid,
                    aid = excluded.aid,
                    forum_replies = excluded.forum_replies,
                    forum_lastpost = excluded.forum_lastpost,
                    last_backup_success_at = excluded.last_backup_success_at,
                    last_full_backup_success_at = excluded.last_full_backup_success_at
                """,
                valid_rows,
            )
        return len(valid_rows)


def _prepare_forum_data(source_path: Path, destination_path: Path) -> None:
    _snapshot_sqlite(source_path, destination_path)
    fingerprints_before = _dynamic_table_fingerprints(
        destination_path,
        "forum_threads_fid_",
    )
    with closing(sqlite3.connect(destination_path)) as connection:
        ensure_storage_metadata(connection, role="forum_data")
        with connection:
            connection.execute("DROP TABLE IF EXISTS ankebak_thread_state")
        connection.execute("VACUUM")
    fingerprints_after = _dynamic_table_fingerprints(
        destination_path,
        "forum_threads_fid_",
    )
    if fingerprints_after != fingerprints_before:
        raise ValueError("forum_threads持久数据指纹不一致。")


def _ensure_image_cache_schema(connection: sqlite3.Connection) -> None:
    ensure_storage_metadata(connection, role="image_cache")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS image_validation_cache (
            relative_path TEXT PRIMARY KEY,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            valid INTEGER NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.commit()


def _relative_image_cache_path(
    output_root: Path,
    raw_path: object,
) -> tuple[str, Path] | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = Path(raw_path)
    try:
        resolved_path = (
            raw.resolve(strict=False)
            if raw.is_absolute()
            else (output_root / raw).resolve(strict=False)
        )
        relative_path = resolved_path.relative_to(output_root.resolve())
    except (OSError, RuntimeError, ValueError):
        return None
    if not relative_path.parts or relative_path.parts[0] != "images_unique":
        return None
    return relative_path.as_posix(), resolved_path


def _copy_image_validation_cache(
    source_path: Path,
    destination_path: Path,
    output_root: Path,
) -> int:
    with closing(sqlite3.connect(destination_path)) as destination:
        _ensure_image_cache_schema(destination)
        if not source_path.is_file():
            return 0
        with closing(sqlite3.connect(source_path)) as source:
            if not _table_exists(source, "image_validation_cache"):
                return 0
            columns = {
                row[1]
                for row in source.execute(
                    "PRAGMA table_info(image_validation_cache)"
                )
                if isinstance(row[1], str)
            }
            path_column = (
                "canonical_path"
                if "canonical_path" in columns
                else "relative_path"
                if "relative_path" in columns
                else None
            )
            if path_column is None:
                return 0
            rows = source.execute(
                f"""
                SELECT {path_column}, size, mtime_ns, valid, updated_at
                FROM image_validation_cache ORDER BY {path_column}
                """
            )
            valid_rows: list[tuple[str, int, int, int, str]] = []
            for raw_path, size, mtime_ns, valid, updated_at in rows:
                normalized = _relative_image_cache_path(output_root, raw_path)
                if (
                    normalized is None
                    or type(size) is not int
                    or type(mtime_ns) is not int
                    or type(valid) is not int
                    or valid not in (0, 1)
                    or not isinstance(updated_at, str)
                ):
                    continue
                relative_path, file_path = normalized
                try:
                    file_stat = file_path.stat()
                except OSError:
                    continue
                if file_stat.st_size != size or file_stat.st_mtime_ns != mtime_ns:
                    continue
                valid_rows.append(
                    (relative_path, size, mtime_ns, valid, updated_at)
                )
        with destination:
            destination.executemany(
                """
                INSERT INTO image_validation_cache VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(relative_path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    valid = excluded.valid,
                    updated_at = excluded.updated_at
                """,
                valid_rows,
            )
        return len(valid_rows)


def _prepare_image_index(source_path: Path, destination_path: Path) -> None:
    _snapshot_sqlite(source_path, destination_path)
    with closing(sqlite3.connect(destination_path)) as connection:
        ensure_image_mappings_schema(connection)
        fingerprint_before = _table_fingerprint(connection, "image_mappings")
        ensure_storage_metadata(connection, role="image_index")
        with connection:
            connection.execute("DROP TABLE IF EXISTS image_validation_cache")
        connection.execute("VACUUM")
        fingerprint_after = _table_fingerprint(connection, "image_mappings")
    if fingerprint_after != fingerprint_before:
        raise ValueError("image_index持久映射指纹不一致。")


def _read_pending_images(
    connection: sqlite3.Connection,
) -> tuple[PendingImageRetry, ...]:
    if not _table_exists(connection, "backup_pending_images"):
        return ()
    columns = {
        row[1]
        for row in connection.execute("PRAGMA table_info(backup_pending_images)")
        if isinstance(row[1], str)
    }
    has_retry_metadata = {
        "last_attempt_at",
        "failure_kind",
        "http_status",
    } <= columns
    select_columns = (
        "url, last_attempt_at, failure_kind, http_status"
        if has_retry_metadata
        else "url, NULL, NULL, NULL"
    )
    rows = connection.execute(
        f"SELECT {select_columns} FROM backup_pending_images ORDER BY url"
    ).fetchall()
    retries: list[PendingImageRetry] = []
    seen_urls: set[str] = set()
    for url, last_attempt_at, failure_kind, http_status in rows:
        if not isinstance(url, str) or not url or url in seen_urls:
            continue
        parsed_last_attempt_at: datetime.datetime | None = None
        parsed_failure_kind: DownloadFailureKind | None = None
        if last_attempt_at is not None:
            if not isinstance(last_attempt_at, str):
                continue
            try:
                parsed_last_attempt_at = datetime.datetime.fromisoformat(
                    last_attempt_at
                )
            except ValueError:
                continue
            if (
                parsed_last_attempt_at.tzinfo is None
                or parsed_last_attempt_at.utcoffset() is None
                or not isinstance(failure_kind, str)
                or failure_kind not in DOWNLOAD_FAILURE_KINDS
            ):
                continue
            parsed_failure_kind = failure_kind
        elif failure_kind is not None or http_status is not None:
            continue
        if http_status is not None and (
            type(http_status) is not int
            or http_status < 100
            or http_status > 599
        ):
            continue
        retries.append(
            PendingImageRetry(
                url=url,
                last_attempt_at=parsed_last_attempt_at,
                failure_kind=parsed_failure_kind,
                http_status=http_status,
            )
        )
        seen_urls.add(url)
    return tuple(retries)


def _read_floor_state(
    connection: sqlite3.Connection,
    archive_revision: int,
    floor_map_revision: int,
) -> FloorProcessingState | None:
    if not _table_exists(connection, "backup_floor_processing_state"):
        return None
    row = connection.execute(
        """
        SELECT format_version, processed_archive_revision,
               processed_floor_map_revision, page_count,
               author_total_lou_count, floor_map_format_version,
               floor_map_generation_version, floor_map_hash_algorithm,
               completed_at
        FROM backup_floor_processing_state WHERE singleton = 1
        """
    ).fetchone()
    if row is None or len(row) != 9:
        return None
    if (
        any(type(value) is not int for value in row[:4] + row[5:7])
        or (row[4] is not None and type(row[4]) is not int)
        or not isinstance(row[7], str)
        or not row[7]
        or not isinstance(row[8], str)
        or not row[8]
    ):
        return None
    state = FloorProcessingState(*row)
    if (
        state.format_version != FLOOR_PROCESSING_STATE_VERSION
        or state.processed_archive_revision != archive_revision
        or state.processed_floor_map_revision != floor_map_revision
        or state.floor_map_format_version != FLOOR_MAP_VERSION
        or state.floor_map_generation_version != FLOOR_MAP_GENERATION_VERSION
        or state.floor_map_hash_algorithm != FLOOR_MAP_HASH_ALGORITHM
    ):
        return None
    return state


def _read_image_state(
    connection: sqlite3.Connection,
    *,
    archive_revision: int,
    overlays_fingerprint: str,
    selections_hash: str,
) -> ImageReferenceState | None:
    if not _table_exists(connection, "backup_image_reference_state"):
        return None
    row = connection.execute(
        """
        SELECT format_version, processed_archive_revision,
               post_overlays_fingerprint,
               post_version_selections_fingerprint,
               image_reference_extractor_version, completed_at
        FROM backup_image_reference_state WHERE singleton = 1
        """
    ).fetchone()
    if row is None or len(row) != 6:
        return None
    if (
        type(row[0]) is not int
        or type(row[1]) is not int
        or type(row[4]) is not int
        or any(not isinstance(value, str) or not value for value in row[2:4])
        or not isinstance(row[5], str)
        or not row[5]
    ):
        return None
    state = ImageReferenceState(*row)
    if (
        state.format_version != IMAGE_REFERENCE_STATE_VERSION
        or state.processed_archive_revision != archive_revision
        or state.post_overlays_fingerprint != overlays_fingerprint
        or state.post_version_selections_fingerprint != selections_hash
        or state.image_reference_extractor_version
        != IMAGE_REFERENCE_EXTRACTOR_VERSION
    ):
        return None
    return state


def _read_manifest_posts(
    connection: sqlite3.Connection,
    archive_revision: int,
) -> tuple[ImageReferenceManifestPost, ...] | None:
    required_tables = (
        "backup_image_reference_manifest_state",
        "backup_image_reference_manifest_posts",
        "backup_image_reference_manifest_entries",
        "backup_image_reference_manifest_urls",
    )
    if not all(_table_exists(connection, table) for table in required_tables):
        return None
    state_row = connection.execute(
        """
        SELECT format_version, processed_archive_revision
        FROM backup_image_reference_manifest_state WHERE singleton = 1
        """
    ).fetchone()
    if state_row != (IMAGE_REFERENCE_MANIFEST_VERSION, archive_revision):
        return None
    post_rows = connection.execute(
        """
        SELECT lou, cache_key
        FROM backup_image_reference_manifest_posts ORDER BY lou
        """
    ).fetchall()
    entry_rows = connection.execute(
        """
        SELECT lou, image_index, url, valid
        FROM backup_image_reference_manifest_entries
        ORDER BY lou, image_index
        """
    ).fetchall()
    references_by_lou: dict[int, list[ImageReferenceManifestEntry]] = {}
    counts: Counter[str] = Counter()
    validity_by_url: dict[str, bool] = {}
    for lou, image_index, url, valid in entry_rows:
        if (
            type(lou) is not int
            or lou < 0
            or type(image_index) is not int
            or image_index <= 0
            or not isinstance(url, str)
            or not url
            or type(valid) is not int
            or valid not in (0, 1)
        ):
            return None
        reference = ImageReferenceManifestEntry(
            image_index=image_index,
            url=url,
            valid=bool(valid),
        )
        references_by_lou.setdefault(lou, []).append(reference)
        previous_validity = validity_by_url.setdefault(url, bool(valid))
        if previous_validity != bool(valid):
            return None
        counts[url] += 1
    posts: list[ImageReferenceManifestPost] = []
    seen_lous: set[int] = set()
    for lou, cache_key in post_rows:
        if (
            type(lou) is not int
            or lou < 0
            or lou in seen_lous
            or not isinstance(cache_key, str)
            or not cache_key
        ):
            return None
        references = tuple(references_by_lou.pop(lou, []))
        previous_index = 0
        for reference in references:
            if reference.image_index <= previous_index:
                return None
            previous_index = reference.image_index
        posts.append(ImageReferenceManifestPost(lou, cache_key, references))
        seen_lous.add(lou)
    if references_by_lou:
        return None
    url_rows = connection.execute(
        """
        SELECT url, reference_count, valid
        FROM backup_image_reference_manifest_urls ORDER BY url
        """
    ).fetchall()
    expected_url_rows = [
        (url, counts[url], int(validity_by_url[url])) for url in sorted(counts)
    ]
    if url_rows != expected_url_rows:
        return None
    return tuple(posts)


def _read_post_reference_cache(
    connection: sqlite3.Connection,
) -> list[PostImageReferenceCacheEntry]:
    if not _table_exists(connection, "post_image_reference_cache"):
        return []
    rows = connection.execute(
        """
        SELECT cache_key, source_hash, extractor_version, references_json
        FROM post_image_reference_cache ORDER BY cache_key
        """
    )
    entries: list[PostImageReferenceCacheEntry] = []
    for cache_key, source_hash, extractor_version, references_json in rows:
        if (
            not isinstance(cache_key, str)
            or not cache_key
            or not isinstance(source_hash, str)
            or not source_hash
            or extractor_version != IMAGE_REFERENCE_EXTRACTOR_VERSION
            or not isinstance(references_json, str)
        ):
            continue
        try:
            deserialize_image_references(references_json)
        except ValueError:
            continue
        entries.append(
            PostImageReferenceCacheEntry(
                cache_key=cache_key,
                source_hash=source_hash,
                extractor_version=extractor_version,
                references_json=references_json,
            )
        )
    return entries


def _copy_legacy_auxiliary_data(
    legacy_archive_path: Path,
    staged_store: ThreadArchiveStore,
) -> ThreadLayoutMigrationStats:
    with closing(sqlite3.connect(legacy_archive_path)) as source:
        change_row = source.execute(
            """
            SELECT archive_revision, floor_map_revision
            FROM archive_change_state WHERE singleton = 1
            """
        ).fetchone()
        if (
            change_row is None
            or type(change_row[0]) is not int
            or type(change_row[1]) is not int
        ):
            raise ValueError(f"旧archive修订状态无效：{change_row!r}")
        archive_revision, floor_map_revision = change_row
        floor_state = _read_floor_state(
            source,
            archive_revision,
            floor_map_revision,
        )
        image_state = _read_image_state(
            source,
            archive_revision=archive_revision,
            overlays_fingerprint=staged_store.post_overlays_fingerprint(),
            selections_hash=(
                staged_store.post_version_selections_fingerprint()
            ),
        )
        manifest_posts = (
            None
            if image_state is None
            else _read_manifest_posts(source, archive_revision)
        )
        pending_images = _read_pending_images(source)
        cache_entries = _read_post_reference_cache(source)

    staged_store.ensure_backup_processing_schema()
    migrated_floor_state = (
        floor_state is not None
        and staged_store.commit_floor_processing_state(floor_state)
    )
    migrated_image_state = False
    if image_state is not None:
        migrated_image_state = staged_store.commit_image_reference_state(
            image_state,
            pending_images,
            manifest_posts=manifest_posts,
        )
    if not migrated_image_state and pending_images:
        staged_store.replace_pending_image_retries(pending_images)
    staged_store.upsert_post_image_reference_cache(cache_entries)
    return ThreadLayoutMigrationStats(
        migrated_floor_state=migrated_floor_state,
        migrated_image_state=migrated_image_state,
        migrated_manifest_posts=(
            len(manifest_posts)
            if migrated_image_state and manifest_posts is not None
            else 0
        ),
        migrated_pending_images=len(pending_images),
        migrated_cache_entries=len(cache_entries),
    )


def _manifest_path(run_root: Path) -> Path:
    return run_root / MIGRATION_MANIFEST_FILENAME


def _write_manifest(run_root: Path, manifest: dict[str, object]) -> None:
    write_json_atomically(
        _manifest_path(run_root),
        manifest,
        indent=2,
        trailing_newline=True,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"迁移清单不是有效JSON：{path}") from error
    if not isinstance(raw, dict):
        raise ValueError(f"迁移清单顶层必须是对象：{path}")
    manifest = cast(dict[str, object], raw)
    if manifest.get("version") != MIGRATION_MANIFEST_VERSION:
        raise ValueError(f"迁移清单版本不受支持：{path}")
    return manifest


def _target_names(manifest: dict[str, object]) -> tuple[str, ...]:
    targets = manifest.get("targets")
    if not isinstance(targets, list):
        raise ValueError("迁移清单targets无效。")
    raw_targets = cast(list[object], targets)
    if not all(isinstance(item, str) for item in raw_targets):
        raise ValueError("迁移清单targets无效。")
    target_names = tuple(
        item for item in raw_targets if isinstance(item, str)
    )
    if any(
        not name or Path(name).name != name or name in (".", "..")
        for name in target_names
    ):
        raise ValueError("迁移清单targets包含不安全路径。")
    return target_names


def _entry_for(
    manifest: dict[str, object],
    target_name: str,
) -> dict[str, object]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, dict):
        raise ValueError("迁移清单entries无效。")
    entries = cast(dict[str, object], raw_entries)
    raw_entry = entries.get(target_name)
    if not isinstance(raw_entry, dict):
        raise ValueError(f"迁移清单缺少目标：{target_name}")
    return cast(dict[str, object], raw_entry)


def _find_resumable_run(
    backup_root: Path,
    target_names: tuple[str, ...],
) -> tuple[Path, dict[str, object]] | None:
    if not backup_root.is_dir():
        return None
    for run_root in sorted(backup_root.iterdir(), reverse=True):
        manifest_path = _manifest_path(run_root)
        if not manifest_path.is_file():
            continue
        try:
            manifest = _load_manifest(manifest_path)
        except ValueError:
            continue
        if (
            manifest.get("status") == "incomplete"
            and manifest.get("archive_schema_version")
            == ARCHIVE_SCHEMA_VERSION
            and _target_names(manifest) == target_names
        ):
            return run_root, manifest
    return None


def _create_manifest(
    output_root: Path,
    target_names: tuple[str, ...],
) -> tuple[Path, dict[str, object]]:
    run_id = _new_run_id()
    run_root = output_root / MIGRATION_BACKUP_DIRNAME / run_id
    run_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "version": MIGRATION_MANIFEST_VERSION,
        "run_id": run_id,
        "output_root": str(output_root.resolve()),
        "created_at": _now_utc_iso(),
        "updated_at": _now_utc_iso(),
        "status": "incomplete",
        "archive_schema_version": ARCHIVE_SCHEMA_VERSION,
        "targets": list(target_names),
        "entries": {
            target_name: {"status": "pending"}
            for target_name in target_names
        },
    }
    _write_manifest(run_root, manifest)
    return run_root, manifest


def _ensure_original_backups(
    thread_folder: Path,
    run_root: Path,
    entry: dict[str, object],
) -> dict[str, bool]:
    raw_originals = entry.get("originals")
    if isinstance(raw_originals, dict):
        original_values = cast(dict[object, object], raw_originals)
        originals = {
            filename: original_values.get(filename) is True
            for filename in _THREAD_DATABASE_FILENAMES
        }
        for filename, existed in originals.items():
            backup_path = run_root / "files" / thread_folder.name / filename
            if existed and not backup_path.is_file():
                raise FileNotFoundError(f"迁移回滚副本缺失：{backup_path}")
        return originals

    originals: dict[str, bool] = {}
    for filename in _THREAD_DATABASE_FILENAMES:
        source = thread_folder / filename
        existed = source.is_file()
        originals[filename] = existed
        if existed:
            _snapshot_sqlite(
                source,
                run_root / "files" / thread_folder.name / filename,
            )
    entry["originals"] = originals
    entry["status"] = "backed_up"
    return originals


def _ensure_global_original_backups(
    output_root: Path,
    run_root: Path,
    entry: dict[str, object],
) -> dict[str, bool]:
    raw_originals = entry.get("originals")
    if isinstance(raw_originals, dict):
        original_values = cast(dict[object, object], raw_originals)
        originals = {
            filename: original_values.get(filename) is True
            for filename in _GLOBAL_DATABASE_FILENAMES
        }
        for filename, existed in originals.items():
            backup_path = run_root / "files" / _GLOBAL_TARGET_NAME / filename
            if existed and not backup_path.is_file():
                raise FileNotFoundError(f"迁移回滚副本缺失：{backup_path}")
        return originals

    originals: dict[str, bool] = {}
    for filename in _GLOBAL_DATABASE_FILENAMES:
        source = output_root / filename
        existed = source.is_file()
        originals[filename] = existed
        if existed:
            _snapshot_sqlite(
                source,
                run_root / "files" / _GLOBAL_TARGET_NAME / filename,
            )
    entry["originals"] = originals
    entry["status"] = "backed_up"
    return originals


def _migrate_global_databases(
    output_root: Path,
    run_root: Path,
    entry: dict[str, object],
) -> dict[str, int] | None:
    if _global_layout_is_complete(output_root):
        return None
    originals = _ensure_global_original_backups(output_root, run_root, entry)
    staging = output_root / f".layout-global-migration-{run_root.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    migrated_ankebak_rows = 0
    migrated_image_cache_rows = 0
    try:
        legacy_forum = (
            run_root
            / "files"
            / _GLOBAL_TARGET_NAME
            / FORUM_THREAD_DB_FILENAME
        )
        legacy_image_index = (
            run_root
            / "files"
            / _GLOBAL_TARGET_NAME
            / IMAGE_INDEX_FILENAME
        )
        if originals[FORUM_THREAD_DB_FILENAME]:
            _prepare_forum_data(
                legacy_forum,
                staging / FORUM_THREAD_DB_FILENAME,
            )
            migrated_ankebak_rows = _copy_ankebak_state(
                legacy_forum,
                staging / BACKUP_STATE_DB_FILENAME,
            )
        elif originals[BACKUP_STATE_DB_FILENAME]:
            _snapshot_sqlite(
                run_root
                / "files"
                / _GLOBAL_TARGET_NAME
                / BACKUP_STATE_DB_FILENAME,
                staging / BACKUP_STATE_DB_FILENAME,
            )

        if originals[IMAGE_INDEX_FILENAME]:
            _prepare_image_index(
                legacy_image_index,
                staging / IMAGE_INDEX_FILENAME,
            )
            migrated_image_cache_rows = _copy_image_validation_cache(
                legacy_image_index,
                staging / IMAGE_CACHE_FILENAME,
                output_root,
            )
        elif originals[IMAGE_CACHE_FILENAME]:
            _snapshot_sqlite(
                run_root
                / "files"
                / _GLOBAL_TARGET_NAME
                / IMAGE_CACHE_FILENAME,
                staging / IMAGE_CACHE_FILENAME,
            )

        if originals[AUDIO_INDEX_FILENAME]:
            staged_audio_index = staging / AUDIO_INDEX_FILENAME
            _snapshot_sqlite(
                run_root
                / "files"
                / _GLOBAL_TARGET_NAME
                / AUDIO_INDEX_FILENAME,
                staged_audio_index,
            )
            with closing(sqlite3.connect(staged_audio_index)) as connection:
                with connection:
                    ensure_storage_metadata(connection, role="audio_index")

        staged_filenames = [
            filename
            for filename in _GLOBAL_DATABASE_FILENAMES
            if (staging / filename).is_file()
        ]
        for filename in staged_filenames:
            _finalize_sqlite(staging / filename)
        entry["stats"] = {
            "migrated_ankebak_rows": migrated_ankebak_rows,
            "migrated_image_cache_rows": migrated_image_cache_rows,
        }
        for filename in (
            BACKUP_STATE_DB_FILENAME,
            IMAGE_CACHE_FILENAME,
            FORUM_THREAD_DB_FILENAME,
            IMAGE_INDEX_FILENAME,
            AUDIO_INDEX_FILENAME,
        ):
            staged_path = staging / filename
            if staged_path.is_file():
                os.replace(staged_path, output_root / filename)
        return {
            "migrated_ankebak_rows": migrated_ankebak_rows,
            "migrated_image_cache_rows": migrated_image_cache_rows,
        }
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _migrate_thread(
    thread_folder: Path,
    run_root: Path,
    entry: dict[str, object],
) -> ThreadLayoutMigrationStats | None:
    if _thread_layout_is_complete(thread_folder):
        return None
    originals = _ensure_original_backups(thread_folder, run_root, entry)
    if not originals[ARCHIVE_DB_FILENAME]:
        raise FileNotFoundError(
            f"帖子目录缺少{ARCHIVE_DB_FILENAME}：{thread_folder}"
        )
    legacy_archive = (
        run_root / "files" / thread_folder.name / ARCHIVE_DB_FILENAME
    )
    page_schema_stats = _read_page_schema_migration_stats(legacy_archive)
    staging = thread_folder / f".layout-migration-{run_root.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        staged_archive = staging / ARCHIVE_DB_FILENAME
        _snapshot_sqlite(legacy_archive, staged_archive)
        for filename in (ARCHIVE_STATE_DB_FILENAME, ARCHIVE_CACHE_DB_FILENAME):
            if originals[filename]:
                _snapshot_sqlite(
                    run_root / "files" / thread_folder.name / filename,
                    staging / filename,
                )
        staged_store = ThreadArchiveStore(staging, allow_layout_upgrade=True)
        staged_store.ensure_schema()
        _validate_page_schema_migration(staged_archive, page_schema_stats)
        fingerprints_before = _durable_fingerprints(staged_archive)
        auxiliary_stats = _copy_legacy_auxiliary_data(
            legacy_archive,
            staged_store,
        )
        _drop_legacy_archive_tables(staged_archive)
        fingerprints_after = _durable_fingerprints(staged_archive)
        if fingerprints_after != fingerprints_before:
            raise ValueError(
                f"持久数据指纹不一致：{thread_folder.name}"
            )
        stats = ThreadLayoutMigrationStats(
            migrated_floor_state=auxiliary_stats.migrated_floor_state,
            migrated_image_state=auxiliary_stats.migrated_image_state,
            migrated_manifest_posts=auxiliary_stats.migrated_manifest_posts,
            migrated_pending_images=auxiliary_stats.migrated_pending_images,
            migrated_cache_entries=auxiliary_stats.migrated_cache_entries,
            page_snapshot_rows_removed=(
                page_schema_stats.page_snapshot_rows_removed
            ),
            page_snapshot_json_bytes_removed=(
                page_schema_stats.page_snapshot_json_bytes_removed
            ),
            post_observation_rows_removed=(
                page_schema_stats.post_observation_rows_removed
            ),
            archive_page_rows=page_schema_stats.archive_page_rows,
        )
        for filename in _THREAD_DATABASE_FILENAMES:
            _finalize_sqlite(staging / filename)
        entry["durable_fingerprints"] = fingerprints_after
        entry["stats"] = {
            "migrated_floor_state": stats.migrated_floor_state,
            "migrated_image_state": stats.migrated_image_state,
            "migrated_manifest_posts": stats.migrated_manifest_posts,
            "migrated_pending_images": stats.migrated_pending_images,
            "migrated_cache_entries": stats.migrated_cache_entries,
            "page_snapshot_rows_removed": stats.page_snapshot_rows_removed,
            "page_snapshot_json_bytes_removed": (
                stats.page_snapshot_json_bytes_removed
            ),
            "post_observation_rows_removed": (
                stats.post_observation_rows_removed
            ),
            "archive_page_rows": stats.archive_page_rows,
        }
        for filename in (
            ARCHIVE_STATE_DB_FILENAME,
            ARCHIVE_CACHE_DB_FILENAME,
            ARCHIVE_DB_FILENAME,
        ):
            os.replace(staging / filename, thread_folder / filename)
        return stats
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def migrate_layout(
    output_root: Path,
    thread_folders: list[Path],
    *,
    include_global: bool = False,
) -> LayoutMigrationResult:
    resolved_output_root = output_root.resolve()
    ordered_folders = sorted(
        {folder.resolve() for folder in thread_folders},
        key=lambda folder: folder.name,
    )
    for folder in ordered_folders:
        if folder.parent != resolved_output_root:
            raise ValueError(f"迁移目标不在output根目录内：{folder}")
    has_global_databases = any(
        (resolved_output_root / filename).is_file()
        for filename in _GLOBAL_DATABASE_FILENAMES
    )
    global_target_names = (
        (_GLOBAL_TARGET_NAME,)
        if include_global and has_global_databases
        else ()
    )
    target_names = global_target_names + tuple(
        folder.name for folder in ordered_folders
    )
    backup_root = resolved_output_root / MIGRATION_BACKUP_DIRNAME
    resumable = _find_resumable_run(backup_root, target_names)
    if resumable is None:
        run_root, manifest = _create_manifest(resolved_output_root, target_names)
    else:
        run_root, manifest = resumable
    run_id = run_root.name

    migrated_count = 0
    skipped_count = 0
    failures: list[tuple[Path, str]] = []
    if global_target_names:
        entry = _entry_for(manifest, _GLOBAL_TARGET_NAME)
        if entry.get("status") in ("completed", "skipped"):
            skipped_count += 1
        else:
            try:
                with use_output_folder_lock(resolved_output_root):
                    if not _global_layout_is_complete(resolved_output_root):
                        _ensure_global_original_backups(
                            resolved_output_root,
                            run_root,
                            entry,
                        )
                        manifest["updated_at"] = _now_utc_iso()
                        _write_manifest(run_root, manifest)
                    global_stats = _migrate_global_databases(
                        resolved_output_root,
                        run_root,
                        entry,
                    )
            except Exception as error:
                error_text = f"{type(error).__name__}: {error}"
                entry["status"] = "failed"
                entry["error"] = error_text
                failures.append((resolved_output_root, error_text))
            else:
                entry.pop("error", None)
                if global_stats is None:
                    entry["status"] = "skipped"
                    skipped_count += 1
                else:
                    entry["status"] = "completed"
                    migrated_count += 1
            manifest["updated_at"] = _now_utc_iso()
            _write_manifest(run_root, manifest)

    for folder in ordered_folders:
        entry = _entry_for(manifest, folder.name)
        if entry.get("status") in ("completed", "skipped"):
            skipped_count += 1
            continue
        try:
            with use_output_folder_lock(folder):
                if not _thread_layout_is_complete(folder):
                    _ensure_original_backups(folder, run_root, entry)
                    manifest["updated_at"] = _now_utc_iso()
                    _write_manifest(run_root, manifest)
                stats = _migrate_thread(folder, run_root, entry)
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"
            entry["status"] = "failed"
            entry["error"] = error_text
            failures.append((folder, error_text))
        else:
            entry.pop("error", None)
            if stats is None:
                entry["status"] = "skipped"
                skipped_count += 1
            else:
                entry["status"] = "completed"
                migrated_count += 1
        manifest["updated_at"] = _now_utc_iso()
        _write_manifest(run_root, manifest)

    manifest["status"] = "completed" if not failures else "incomplete"
    manifest["updated_at"] = _now_utc_iso()
    _write_manifest(run_root, manifest)
    return LayoutMigrationResult(
        run_id=run_id,
        migrated_count=migrated_count,
        skipped_count=skipped_count,
        failures=tuple(failures),
    )


def rollback_layout(output_root: Path, run_id: str) -> LayoutRollbackResult:
    resolved_output_root = output_root.resolve()
    if not run_id or Path(run_id).name != run_id:
        raise ValueError(f"迁移运行ID无效：{run_id!r}")
    run_root = resolved_output_root / MIGRATION_BACKUP_DIRNAME / run_id
    manifest = _load_manifest(_manifest_path(run_root))
    if manifest.get("output_root") != str(resolved_output_root):
        raise ValueError("迁移清单不属于当前output目录。")
    restored_count = 0
    for target_name in _target_names(manifest):
        entry = _entry_for(manifest, target_name)
        raw_originals = entry.get("originals")
        if not isinstance(raw_originals, dict):
            continue
        original_values = cast(dict[object, object], raw_originals)
        if target_name == _GLOBAL_TARGET_NAME:
            for filename in (
                BACKUP_STATE_DB_FILENAME,
                IMAGE_CACHE_FILENAME,
                FORUM_THREAD_DB_FILENAME,
                IMAGE_INDEX_FILENAME,
                AUDIO_INDEX_FILENAME,
            ):
                target = resolved_output_root / filename
                existed = original_values.get(filename) is True
                if existed:
                    backup_path = (
                        run_root / "files" / _GLOBAL_TARGET_NAME / filename
                    )
                    _snapshot_sqlite(backup_path, target)
                else:
                    _remove_sqlite_file(target)
            entry["status"] = "rolled_back"
            restored_count += 1
            manifest["updated_at"] = _now_utc_iso()
            _write_manifest(run_root, manifest)
            continue
        thread_folder = resolved_output_root / target_name
        if (
            thread_folder.exists()
            and thread_folder.resolve().parent != resolved_output_root
        ):
            raise ValueError(f"回滚目标不在output根目录内：{thread_folder}")
        thread_folder.mkdir(parents=True, exist_ok=True)
        for filename in (
            ARCHIVE_STATE_DB_FILENAME,
            ARCHIVE_CACHE_DB_FILENAME,
            ARCHIVE_DB_FILENAME,
        ):
            target = thread_folder / filename
            existed = original_values.get(filename) is True
            if existed:
                backup_path = run_root / "files" / target_name / filename
                _snapshot_sqlite(backup_path, target)
            else:
                _remove_sqlite_file(target)
        entry["status"] = "rolled_back"
        restored_count += 1
        manifest["updated_at"] = _now_utc_iso()
        _write_manifest(run_root, manifest)
    manifest["status"] = "rolled_back"
    manifest["updated_at"] = _now_utc_iso()
    _write_manifest(run_root, manifest)
    return LayoutRollbackResult(run_id=run_id, restored_count=restored_count)

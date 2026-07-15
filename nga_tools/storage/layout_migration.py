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
from nga_tools.backup.post_version_selection import selections_fingerprint
from nga_tools.backup.thread_stores import (
    ARCHIVE_CACHE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
)
from nga_tools.core.atomic import replace_temp_file, write_json_atomically
from nga_tools.core.downloads import DOWNLOAD_FAILURE_KINDS, DownloadFailureKind
from nga_tools.core.output_lock import use_output_folder_lock
from nga_tools.storage import (
    STORAGE_LAYOUT_VERSION,
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
)
_DURABLE_ARCHIVE_TABLES = (
    "page_snapshots",
    "post_versions",
    "post_latest_metadata",
    "post_observations",
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
    original_thread_folder: Path,
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
            selections_hash=selections_fingerprint(original_thread_folder),
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
    staging = thread_folder / f".layout-migration-{run_root.name}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    try:
        staged_archive = staging / ARCHIVE_DB_FILENAME
        _snapshot_sqlite(legacy_archive, staged_archive)
        staged_store = ThreadArchiveStore(staging)
        staged_store.ensure_schema()
        fingerprints_before = _durable_fingerprints(staged_archive)
        stats = _copy_legacy_auxiliary_data(
            legacy_archive,
            staged_store,
            thread_folder,
        )
        _drop_legacy_archive_tables(staged_archive)
        fingerprints_after = _durable_fingerprints(staged_archive)
        if fingerprints_after != fingerprints_before:
            raise ValueError(
                f"持久数据指纹不一致：{thread_folder.name}"
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
) -> LayoutMigrationResult:
    resolved_output_root = output_root.resolve()
    ordered_folders = sorted(
        {folder.resolve() for folder in thread_folders},
        key=lambda folder: folder.name,
    )
    for folder in ordered_folders:
        if folder.parent != resolved_output_root:
            raise ValueError(f"迁移目标不在output根目录内：{folder}")
    target_names = tuple(folder.name for folder in ordered_folders)
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
    for folder in ordered_folders:
        entry = _entry_for(manifest, folder.name)
        if entry.get("status") in ("completed", "skipped"):
            skipped_count += 1
            continue
        try:
            with use_output_folder_lock(folder):
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

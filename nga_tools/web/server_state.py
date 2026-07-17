from __future__ import annotations

import threading
from _thread import LockType
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, cast

from nga_tools.backup import image_index
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.config import get_config
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.web.database import (
    DatabaseNotFoundError,
    DatabaseSchema,
    DatabaseSummary,
    list_database_refs,
    list_database_summaries,
    read_database_schema,
    resolve_database,
)
from nga_tools.web.image_usage import ImageUsageSnapshot, build_image_usage_snapshot
from nga_tools.web.thread_data import (
    PostVersionThreadSummary,
    ThreadSummary,
    ThreadSummaryDetail,
    parse_thread_dir_name,
    read_post_version_thread_summaries,
    scan_thread_summaries,
)

ThreadListFingerprint = tuple[str, ...]
DatabaseListFingerprint = tuple[str, ...]
DatabaseSchemaFingerprint = str
ImageUsageFingerprint = tuple[str, ...]


def _new_lock() -> LockType:
    return threading.Lock()



def _new_lock_map() -> dict[Path, LockType]:
    return {}



def _new_post_version_fingerprint_map() -> dict[
    ThreadSummaryDetail,
    ThreadListFingerprint,
]:
    return {}



def _new_post_version_thread_map() -> dict[
    ThreadSummaryDetail,
    list[PostVersionThreadSummary],
]:
    return {}



def _new_database_schema_fingerprint_map() -> dict[str, DatabaseSchemaFingerprint]:
    return {}



def _new_database_schema_map() -> dict[str, DatabaseSchema]:
    return {}



def _file_fingerprint(path: Path) -> str:
    try:
        stat_result = path.stat()
    except OSError:
        return "-"
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"



def _sqlite_fingerprint(path: Path) -> str:
    wal_path = Path(str(path) + "-wal")
    return f"{_file_fingerprint(path)}:{_file_fingerprint(wal_path)}"



def _thread_list_fingerprint(output_dir: Path) -> ThreadListFingerprint:
    entries = [
        f"config:{_file_fingerprint(Path(get_config().thread_config_file))}",
        f"output:{_file_fingerprint(output_dir)}",
    ]
    if not output_dir.is_dir():
        return tuple(entries)

    for thread_folder in sorted(output_dir.iterdir(), key=lambda path: path.name):
        if not thread_folder.is_dir():
            continue
        if parse_thread_dir_name(thread_folder.name) is None:
            continue
        archive_path = thread_folder / ARCHIVE_DB_FILENAME
        entries.append(
            "\0".join(
                [
                    thread_folder.name,
                    _file_fingerprint(thread_folder),
                    _sqlite_fingerprint(archive_path),
                    _file_fingerprint(thread_folder / "warnings.log"),
                    _file_fingerprint(thread_folder / "json"),
                ]
            )
        )
    return tuple(entries)



def _copy_thread_summaries(items: list[ThreadSummary]) -> list[ThreadSummary]:
    return [cast(ThreadSummary, dict(item)) for item in items]



def _copy_post_version_thread_summaries(
    items: list[PostVersionThreadSummary],
) -> list[PostVersionThreadSummary]:
    return [cast(PostVersionThreadSummary, dict(item)) for item in items]



def _copy_database_summaries(items: list[DatabaseSummary]) -> list[DatabaseSummary]:
    return deepcopy(items)



def _copy_database_schema(schema: DatabaseSchema) -> DatabaseSchema:
    return deepcopy(schema)



def _database_list_fingerprint(output_dir: Path) -> DatabaseListFingerprint:
    entries = [f"output:{_file_fingerprint(output_dir)}"]
    entries.extend(
        f"{ref.id}:{_sqlite_fingerprint(ref.path)}"
        for ref in list_database_refs(output_dir)
    )
    return tuple(entries)



def _database_file_for_id(output_dir: Path, database_id: str) -> Optional[Path]:
    try:
        return resolve_database(output_dir, database_id).path
    except DatabaseNotFoundError:
        return None



def _database_schema_fingerprint(
    output_dir: Path,
    database_id: str,
) -> DatabaseSchemaFingerprint:
    database_path = _database_file_for_id(output_dir, database_id)
    if database_path is None:
        return f"{database_id}\0-"
    return f"{database_id}\0{_sqlite_fingerprint(database_path)}"



def _image_usage_fingerprint(output_dir: Path) -> ImageUsageFingerprint:
    image_index_path = output_dir / image_index.IMAGE_INDEX_FILENAME
    entries = [
        f"config:{_file_fingerprint(Path(get_config().thread_config_file))}",
        f"output:{_file_fingerprint(output_dir)}",
        f"image:{_file_fingerprint(image_index_path)}",
        f"image-wal:{_file_fingerprint(Path(str(image_index_path) + '-wal'))}",
        f"images:{_file_fingerprint(output_dir / 'images_unique')}",
    ]
    if not output_dir.is_dir():
        return tuple(entries)

    for thread_folder in sorted(output_dir.iterdir(), key=lambda path: path.name):
        if not thread_folder.is_dir():
            continue
        if parse_thread_dir_name(thread_folder.name) is None:
            continue
        archive_path = thread_folder / ARCHIVE_DB_FILENAME
        if not archive_path.is_file():
            continue
        entries.append(
            "\0".join(
                [
                    thread_folder.name,
                    _file_fingerprint(archive_path),
                    _file_fingerprint(Path(str(archive_path) + "-wal")),
                ]
            )
        )
    return tuple(entries)



@dataclass


class ThreadSummaryCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprint: Optional[ThreadListFingerprint] = None
    _items: Optional[list[ThreadSummary]] = None

    def read(
        self,
        output_dir: Path,
        metadata_by_key: dict[tuple[int, str], ThreadConfig],
        *,
        refresh: bool,
    ) -> list[ThreadSummary]:
        fingerprint = _thread_list_fingerprint(output_dir)
        with self._guard:
            if (
                not refresh
                and self._fingerprint == fingerprint
                and self._items is not None
            ):
                return _copy_thread_summaries(self._items)

            items = scan_thread_summaries(
                output_dir,
                metadata_by_key,
                detail="full",
            )
            self._fingerprint = _thread_list_fingerprint(output_dir)
            self._items = _copy_thread_summaries(items)
            return _copy_thread_summaries(items)



@dataclass


class PostVersionThreadCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprints: dict[ThreadSummaryDetail, ThreadListFingerprint] = field(
        default_factory=_new_post_version_fingerprint_map
    )
    _items: dict[ThreadSummaryDetail, list[PostVersionThreadSummary]] = field(
        default_factory=_new_post_version_thread_map
    )

    def read(
        self,
        output_dir: Path,
        metadata_by_key: dict[tuple[int, str], ThreadConfig],
        *,
        detail: ThreadSummaryDetail,
        refresh: bool,
    ) -> list[PostVersionThreadSummary]:
        fingerprint = _thread_list_fingerprint(output_dir)
        with self._guard:
            if (
                not refresh
                and self._fingerprints.get(detail) == fingerprint
                and detail in self._items
            ):
                return _copy_post_version_thread_summaries(self._items[detail])

            items = read_post_version_thread_summaries(
                output_dir,
                metadata_by_key,
                detail=detail,
            )["items"]
            self._fingerprints[detail] = _thread_list_fingerprint(output_dir)
            self._items[detail] = _copy_post_version_thread_summaries(items)
            return _copy_post_version_thread_summaries(items)



@dataclass


class DatabaseSummaryCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprint: Optional[DatabaseListFingerprint] = None
    _items: Optional[list[DatabaseSummary]] = None

    def read(self, output_dir: Path, *, refresh: bool) -> list[DatabaseSummary]:
        fingerprint = _database_list_fingerprint(output_dir)
        with self._guard:
            if (
                not refresh
                and self._fingerprint == fingerprint
                and self._items is not None
            ):
                return _copy_database_summaries(self._items)

            items = list_database_summaries(output_dir)
            self._fingerprint = _database_list_fingerprint(output_dir)
            self._items = _copy_database_summaries(items)
            return _copy_database_summaries(items)



@dataclass


class DatabaseSchemaCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprints: dict[str, DatabaseSchemaFingerprint] = field(
        default_factory=_new_database_schema_fingerprint_map
    )
    _items: dict[str, DatabaseSchema] = field(
        default_factory=_new_database_schema_map
    )

    def read(
        self,
        output_dir: Path,
        database_id: str,
        *,
        refresh: bool,
    ) -> DatabaseSchema:
        fingerprint = _database_schema_fingerprint(output_dir, database_id)
        with self._guard:
            if (
                not refresh
                and self._fingerprints.get(database_id) == fingerprint
                and database_id in self._items
            ):
                return _copy_database_schema(self._items[database_id])

            schema = read_database_schema(output_dir, database_id)
            self._fingerprints[database_id] = _database_schema_fingerprint(
                output_dir,
                database_id,
            )
            self._items[database_id] = _copy_database_schema(schema)
            return _copy_database_schema(schema)



@dataclass


class ImageUsageCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprint: Optional[ImageUsageFingerprint] = None
    _snapshot: Optional[ImageUsageSnapshot] = None

    def read(
        self,
        output_dir: Path,
        *,
        refresh: bool,
    ) -> ImageUsageSnapshot:
        fingerprint = _image_usage_fingerprint(output_dir)
        with self._guard:
            if (
                not refresh
                and self._fingerprint == fingerprint
                and self._snapshot is not None
            ):
                return self._snapshot

            snapshot = build_image_usage_snapshot(output_dir)
            # Read-only SQLite access can create or settle WAL sidecar state.
            # Bind the snapshot to the stable post-build source fingerprint so
            # the next request does not immediately discard the memory cache.
            self._fingerprint = _image_usage_fingerprint(output_dir)
            self._snapshot = snapshot
            return snapshot



@dataclass(frozen=True)


class PostVersionSelectionLocks:
    _guard: LockType = field(default_factory=_new_lock)
    _locks: dict[Path, LockType] = field(default_factory=_new_lock_map)

    def for_thread(self, thread_folder: Path) -> LockType:
        lock_key = thread_folder.resolve()
        with self._guard:
            lock = self._locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[lock_key] = lock
            return lock



@dataclass(frozen=True)


class ViewerContext:
    output_dir: Path
    static_dir: Path
    post_version_selection_locks: PostVersionSelectionLocks
    thread_summary_cache: ThreadSummaryCache
    post_version_thread_cache: PostVersionThreadCache
    database_summary_cache: DatabaseSummaryCache
    database_schema_cache: DatabaseSchemaCache
    image_usage_cache: ImageUsageCache

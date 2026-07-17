from __future__ import annotations

import datetime
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from nga_tools.backup.thread_stores import ThreadArchiveCacheStore
from nga_tools.storage import UnsupportedStorageFormatError


@dataclass(frozen=True)
class PostImageReferenceCacheEntry:
    cache_key: str
    source_hash: str
    extractor_version: int
    references_json: str


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class ArchiveCacheSource(Protocol):
    def exists(self) -> bool: ...
    def require_exists(self) -> None: ...
    def ensure_schema(self) -> None: ...
    def archive_store_id(self) -> str: ...


class ArchiveCacheRepository:
    def __init__(
        self,
        source: ArchiveCacheSource,
        cache_store: ThreadArchiveCacheStore,
    ) -> None:
        self._source = source
        self.cache_store = cache_store

    @property
    def db_path(self) -> Path:
        return self.cache_store.db_path

    def require_exists(self) -> None:
        self._source.require_exists()

    def archive_store_id(self) -> str:
        return self._source.archive_store_id()

    def _connect_cache_write(self) -> sqlite3.Connection:
        return self.cache_store.connect_write(self.archive_store_id())

    def _connect_cache_read(self) -> sqlite3.Connection:
        return self.cache_store.connect_read(self.archive_store_id())

    def ensure_schema(self) -> None:
        if not self._source.exists():
            self._source.ensure_schema()
        self.cache_store.ensure_schema(self.archive_store_id())

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

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Literal

from nga_tools.storage.errors import UnsupportedStorageFormatError


STORAGE_LAYOUT_VERSION = 2
StorageRole = Literal[
    "archive_data",
    "archive_state",
    "archive_cache",
    "forum_data",
    "backup_state",
    "image_index",
    "image_cache",
    "audio_index",
    "image_cluster",
]


@dataclass(frozen=True)
class StorageMetadata:
    role: StorageRole
    layout_version: int
    store_id: str
    source_store_id: str | None


def _metadata_from_row(row: tuple[object, ...]) -> StorageMetadata:
    if len(row) != 4:
        raise ValueError(f"storage_metadata字段数量无效：{row!r}")
    role, layout_version, store_id, source_store_id = row
    valid_roles: tuple[StorageRole, ...] = (
        "archive_data",
        "archive_state",
        "archive_cache",
        "forum_data",
        "backup_state",
        "image_index",
        "image_cache",
        "audio_index",
        "image_cluster",
    )
    if role not in valid_roles:
        raise ValueError(f"storage_metadata角色无效：{role!r}")
    if type(layout_version) is not int or layout_version <= 0:
        raise ValueError(
            f"storage_metadata布局版本无效：{layout_version!r}"
        )
    if not isinstance(store_id, str) or not store_id:
        raise ValueError(f"storage_metadata store_id无效：{store_id!r}")
    if source_store_id is not None and (
        not isinstance(source_store_id, str) or not source_store_id
    ):
        raise ValueError(
            f"storage_metadata source_store_id无效：{source_store_id!r}"
        )
    return StorageMetadata(
        role=role,
        layout_version=layout_version,
        store_id=store_id,
        source_store_id=source_store_id,
    )


def read_storage_metadata(
    connection: sqlite3.Connection,
) -> StorageMetadata | None:
    table = connection.execute(
        """
        SELECT 1
        FROM sqlite_schema
        WHERE type = 'table' AND name = 'storage_metadata'
        """
    ).fetchone()
    if table is None:
        return None
    row = connection.execute(
        """
        SELECT role, layout_version, store_id, source_store_id
        FROM storage_metadata
        WHERE singleton = 1
        """
    ).fetchone()
    if row is None:
        return None
    return _metadata_from_row(tuple(row))


def ensure_storage_metadata(
    connection: sqlite3.Connection,
    *,
    role: StorageRole,
    source_store_id: str | None = None,
    store_id: str | None = None,
) -> StorageMetadata:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_metadata (
            singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
            role TEXT NOT NULL,
            layout_version INTEGER NOT NULL CHECK(layout_version > 0),
            store_id TEXT NOT NULL CHECK(store_id != ''),
            source_store_id TEXT
        )
        """
    )
    existing = read_storage_metadata(connection)
    if existing is not None:
        if existing.role != role:
            raise ValueError(
                f"SQLite存储角色不匹配：期望{role}，实际{existing.role}。"
            )
        if existing.layout_version != STORAGE_LAYOUT_VERSION:
            raise ValueError(
                "SQLite存储布局版本不受支持："
                f"{existing.layout_version}。"
            )
        if existing.source_store_id != source_store_id:
            raise ValueError(
                "SQLite存储来源不匹配："
                f"期望{source_store_id!r}，实际{existing.source_store_id!r}。"
            )
        if store_id is not None and existing.store_id != store_id:
            raise ValueError(
                f"SQLite store_id不匹配：期望{store_id}，"
                f"实际{existing.store_id}。"
            )
        return existing

    new_metadata = StorageMetadata(
        role=role,
        layout_version=STORAGE_LAYOUT_VERSION,
        store_id=store_id or str(uuid.uuid4()),
        source_store_id=source_store_id,
    )
    connection.execute(
        """
        INSERT INTO storage_metadata (
            singleton,
            role,
            layout_version,
            store_id,
            source_store_id
        ) VALUES (1, ?, ?, ?, ?)
        """,
        (
            new_metadata.role,
            new_metadata.layout_version,
            new_metadata.store_id,
            new_metadata.source_store_id,
        ),
    )
    return new_metadata


def require_storage_metadata(
    connection: sqlite3.Connection,
    *,
    role: StorageRole,
    source_store_id: str | None = None,
) -> StorageMetadata:
    columns = tuple(
        (row[1], str(row[2]).upper())
        for row in connection.execute("PRAGMA table_info(storage_metadata)")
        if len(row) > 2 and isinstance(row[1], str)
    )
    expected_columns = (
        ("singleton", "INTEGER"),
        ("role", "TEXT"),
        ("layout_version", "INTEGER"),
        ("store_id", "TEXT"),
        ("source_store_id", "TEXT"),
    )
    if columns != expected_columns:
        raise UnsupportedStorageFormatError(
            "SQLite storage_metadata不符合当前格式："
            f"expected={expected_columns!r}, actual={columns!r}"
        )
    try:
        metadata = read_storage_metadata(connection)
    except ValueError as error:
        raise UnsupportedStorageFormatError(
            f"SQLite storage_metadata记录无效：{error}"
        ) from error
    if metadata is None:
        raise UnsupportedStorageFormatError("SQLite缺少storage_metadata记录。")
    if metadata.role != role:
        raise UnsupportedStorageFormatError(
            f"SQLite存储角色不匹配：期望{role}，实际{metadata.role}。"
        )
    if metadata.layout_version != STORAGE_LAYOUT_VERSION:
        raise UnsupportedStorageFormatError(
            "SQLite存储布局版本不受支持："
            f"期望{STORAGE_LAYOUT_VERSION}，实际{metadata.layout_version}。"
        )
    if metadata.source_store_id != source_store_id:
        raise UnsupportedStorageFormatError(
            "SQLite存储来源不匹配："
            f"期望{source_store_id!r}，实际{metadata.source_store_id!r}。"
        )
    return metadata

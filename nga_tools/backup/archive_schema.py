from __future__ import annotations

import sqlite3
from pathlib import Path

from nga_tools.storage import StorageMetadata, require_storage_metadata
from nga_tools.storage.errors import UnsupportedStorageFormatError
from nga_tools.storage.schema import (
    require_exact_columns,
    require_index_names,
    require_table_names,
    require_table_sql,
)


ARCHIVE_SCHEMA_VERSION = 1
ARCHIVE_READ_INDEXES = {"idx_post_versions_latest_covering"}
ARCHIVE_WRITE_INDEXES = {
    *ARCHIVE_READ_INDEXES,
    "idx_floor_map_entries_unresolved",
}
ARCHIVE_FORBIDDEN_INDEXES = {"idx_post_versions_latest"}
ARCHIVE_TABLES = {
    "storage_metadata",
    "archive_pages",
    "post_versions",
    "post_latest_metadata",
    "post_version_selections",
    "floor_map_state",
    "floor_map_entries",
    "floor_map_candidates",
    "post_overlays",
    "archive_change_state",
}
ARCHIVE_TABLE_COLUMNS = {
    "archive_pages": (
        ("page_number", "INTEGER"),
        ("total_page", "INTEGER"),
        ("vrows", "INTEGER"),
        ("last_seen_at", "TEXT"),
    ),
    "post_versions": (
        ("id", "INTEGER"),
        ("pid", "INTEGER"),
        ("lou", "INTEGER"),
        ("source_hash", "TEXT"),
        ("content", "BLOB"),
        ("word_count_version", "INTEGER"),
        ("word_count_chinese_chars", "INTEGER"),
        ("word_count_chinese_with_punctuation", "INTEGER"),
        ("first_seen_at", "TEXT"),
        ("last_seen_at", "TEXT"),
        ("seen_count", "INTEGER"),
    ),
    "post_latest_metadata": (
        ("pid", "INTEGER"),
        ("lou", "INTEGER"),
        ("author_name", "TEXT"),
        ("author_uid", "INTEGER"),
        ("postdate_json", "TEXT"),
        ("image_attachments_json", "TEXT"),
        ("first_seen_at", "TEXT"),
        ("last_seen_at", "TEXT"),
        ("seen_count", "INTEGER"),
    ),
    "post_version_selections": (
        ("lou", "INTEGER"),
        ("version_id", "INTEGER"),
        ("selected_at", "TEXT"),
    ),
    "floor_map_state": (
        ("singleton", "INTEGER"),
        ("tid", "INTEGER"),
        ("aid", "INTEGER"),
        ("format_version", "INTEGER"),
        ("generation_version", "INTEGER"),
        ("hash_algorithm", "TEXT"),
        ("input_signature", "TEXT"),
    ),
    "floor_map_entries": (
        ("author_lou", "INTEGER"),
        ("pid", "INTEGER"),
        ("original_lou", "INTEGER"),
        ("original_pid", "INTEGER"),
    ),
    "floor_map_candidates": (
        ("author_lou", "INTEGER"),
        ("candidate_index", "INTEGER"),
        ("original_lou", "INTEGER"),
    ),
    "post_overlays": (
        ("lou", "INTEGER"),
        ("mode", "TEXT"),
        ("bbcode", "TEXT"),
        ("content_hash", "TEXT"),
        ("updated_at", "TEXT"),
    ),
    "archive_change_state": (
        ("singleton", "INTEGER"),
        ("archive_revision", "INTEGER"),
        ("floor_map_revision", "INTEGER"),
    ),
}


def read_archive_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise ValueError(f"archive user_version无效：{row!r}")
    return row[0]


def require_current_archive_schema(
    connection: sqlite3.Connection,
    db_path: Path,
) -> StorageMetadata:
    metadata = require_current_archive_identity(connection, db_path)
    source = f"archive {db_path}"
    require_table_names(connection, expected=ARCHIVE_TABLES, source=source)
    for table_name, columns in ARCHIVE_TABLE_COLUMNS.items():
        require_exact_columns(
            connection,
            table_name,
            columns,
            source=source,
        )
    require_table_sql(
        connection,
        "post_versions",
        source=source,
        required_fragments=("CHECK(typeof(content) = 'blob')",),
    )
    require_table_sql(
        connection,
        "post_overlays",
        source=source,
        forbidden_fragments=("CHECK(length(trim(bbcode)) > 0)",),
    )
    require_index_names(
        connection,
        required=ARCHIVE_READ_INDEXES,
        forbidden=ARCHIVE_FORBIDDEN_INDEXES,
        source=source,
    )
    return metadata


def require_archive_write_ready_schema(
    connection: sqlite3.Connection,
    db_path: Path,
) -> StorageMetadata:
    metadata = require_current_archive_schema(connection, db_path)
    require_index_names(
        connection,
        required=ARCHIVE_WRITE_INDEXES,
        forbidden=ARCHIVE_FORBIDDEN_INDEXES,
        source=f"archive {db_path}",
    )
    return metadata


def require_current_archive_identity(
    connection: sqlite3.Connection,
    db_path: Path,
) -> StorageMetadata:
    source = f"archive {db_path}"
    metadata = require_storage_metadata(connection, role="archive_data")
    version = read_archive_schema_version(connection)
    if version != ARCHIVE_SCHEMA_VERSION:
        raise UnsupportedStorageFormatError(
            f"{source} schema版本不受支持："
            f"期望{ARCHIVE_SCHEMA_VERSION}，实际{version}。"
        )
    return metadata

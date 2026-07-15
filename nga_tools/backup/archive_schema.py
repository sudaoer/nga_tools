from __future__ import annotations

import sqlite3
from pathlib import Path


ARCHIVE_SCHEMA_VERSION = 1
RETIRED_PAGE_TABLES = ("page_snapshots", "post_observations")


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


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows if isinstance(row[1], str)}


def read_archive_schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute("PRAGMA user_version").fetchone()
    if row is None or len(row) != 1 or type(row[0]) is not int:
        raise ValueError(f"archive user_version无效：{row!r}")
    return row[0]


def require_current_archive_schema(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    version = read_archive_schema_version(connection)
    if version != ARCHIVE_SCHEMA_VERSION:
        raise ValueError(
            f"archive仍是旧分页存储schema：{db_path}。"
            "请先运行 backup migrate-layout；运行时不会读取或升级旧表。"
        )
    if not _table_exists(connection, "archive_pages"):
        raise ValueError(f"archive缺少archive_pages：{db_path}")
    required_columns = {"page_number", "total_page", "vrows", "last_seen_at"}
    columns = _table_columns(connection, "archive_pages")
    if not required_columns <= columns:
        raise ValueError(
            f"archive archive_pages字段不完整：{db_path} columns={sorted(columns)}"
        )
    retired_tables = [
        table_name
        for table_name in RETIRED_PAGE_TABLES
        if _table_exists(connection, table_name)
    ]
    if retired_tables:
        raise ValueError(
            f"archive同时包含已停用分页表：{db_path} {retired_tables!r}。"
            "请运行 backup migrate-layout。"
        )

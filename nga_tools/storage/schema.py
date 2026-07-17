from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from nga_tools.storage.errors import UnsupportedStorageFormatError


ColumnContract = tuple[str, str]


def table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        row[0]
        for row in connection.execute(
            """
            SELECT name
            FROM sqlite_schema
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """
        )
        if isinstance(row[0], str)
    }


def require_table_names(
    connection: sqlite3.Connection,
    *,
    expected: set[str],
    source: str,
    allowed_prefixes: tuple[str, ...] = (),
) -> None:
    actual = table_names(connection)
    unexpected = {
        name
        for name in actual - expected
        if not name.startswith(allowed_prefixes)
    }
    missing = expected - actual
    if missing or unexpected:
        raise UnsupportedStorageFormatError(
            f"{source}数据表不符合当前格式："
            f"missing={sorted(missing)!r}, unexpected={sorted(unexpected)!r}"
        )


def require_exact_columns(
    connection: sqlite3.Connection,
    table_name: str,
    expected: Iterable[ColumnContract],
    *,
    source: str,
) -> None:
    quoted_table_name = table_name.replace('"', '""')
    actual = tuple(
        (row[1], str(row[2]).upper())
        for row in connection.execute(
            f'PRAGMA table_info("{quoted_table_name}")'
        )
        if len(row) > 2 and isinstance(row[1], str)
    )
    expected_tuple = tuple(
        (column_name, declared_type.upper())
        for column_name, declared_type in expected
    )
    if actual != expected_tuple:
        raise UnsupportedStorageFormatError(
            f"{source}数据表{table_name}字段不符合当前格式："
            f"expected={expected_tuple!r}, actual={actual!r}"
        )


def require_table_sql(
    connection: sqlite3.Connection,
    table_name: str,
    *,
    source: str,
    required_fragments: tuple[str, ...] = (),
    forbidden_fragments: tuple[str, ...] = (),
) -> None:
    row = connection.execute(
        """
        SELECT sql FROM sqlite_schema
        WHERE type = 'table' AND name = ?
        """,
        (table_name,),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        raise UnsupportedStorageFormatError(
            f"{source}缺少当前格式数据表：{table_name}"
        )
    normalized = "".join(row[0].lower().split())
    missing = [
        fragment
        for fragment in required_fragments
        if "".join(fragment.lower().split()) not in normalized
    ]
    forbidden = [
        fragment
        for fragment in forbidden_fragments
        if "".join(fragment.lower().split()) in normalized
    ]
    if missing or forbidden:
        raise UnsupportedStorageFormatError(
            f"{source}数据表{table_name}约束不符合当前格式："
            f"missing={missing!r}, forbidden={forbidden!r}"
        )


def require_index_names(
    connection: sqlite3.Connection,
    *,
    required: set[str],
    forbidden: set[str],
    source: str,
) -> None:
    actual = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_schema
            WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
            """
        )
        if isinstance(row[0], str)
    }
    missing = required - actual
    present_forbidden = forbidden & actual
    if missing or present_forbidden:
        raise UnsupportedStorageFormatError(
            f"{source}索引不符合当前格式："
            f"missing={sorted(missing)!r}, "
            f"forbidden={sorted(present_forbidden)!r}"
        )

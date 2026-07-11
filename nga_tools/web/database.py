from __future__ import annotations

import datetime
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypeAlias, TypedDict, cast
from urllib.parse import quote

from nga_tools.backup import image_store
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.core.sqlite import configure_readonly_connection
from nga_tools.forum.thread_store import FORUM_THREAD_DB_FILENAME
from nga_tools.web.data import parse_thread_dir_name

DatabaseKind: TypeAlias = Literal["forum_threads", "image_index", "archive"]
DatabaseStatus: TypeAlias = Literal["ready", "invalid"]
TableKind: TypeAlias = Literal["table", "view"]
SortDirection: TypeAlias = Literal["asc", "desc"]
DbCellKind: TypeAlias = Literal["null", "integer", "real", "text", "blob", "other"]
DbCellValue: TypeAlias = str | int | float | None

_DATABASE_ID_FORUM_THREADS = "forum_threads"
_DATABASE_ID_IMAGE_INDEX = "image_index"
_ARCHIVE_DATABASE_PREFIX = "archive:"
_ROW_PREVIEW_TEXT_LIMIT = 240
_ROW_PREVIEW_BLOB_BYTES = 64


class DatabaseSummary(TypedDict):
    id: str
    kind: DatabaseKind
    label: str
    relativePath: str
    status: DatabaseStatus
    message: Optional[str]
    sizeBytes: int
    updatedAt: str
    tableCount: int


class ColumnInfo(TypedDict):
    name: str
    type: str
    notNull: bool
    primaryKey: bool
    defaultValue: Optional[str]


class TableSummary(TypedDict):
    name: str
    type: TableKind
    rowCount: Optional[int]
    columns: list[ColumnInfo]


class DatabaseSchema(TypedDict):
    database: DatabaseSummary
    tables: list[TableSummary]


class DbCell(TypedDict):
    kind: DbCellKind
    value: DbCellValue
    truncated: bool


class TableRow(TypedDict):
    rowId: Optional[int]
    cells: dict[str, DbCell]


class TableRows(TypedDict):
    columns: list[ColumnInfo]
    rows: list[TableRow]
    total: int
    offset: int
    limit: int
    query: str
    sortBy: Optional[str]
    sortDirection: SortDirection


class TableRowDetail(TypedDict):
    row: TableRow


@dataclass(frozen=True)
class DatabaseRef:
    id: str
    kind: DatabaseKind
    label: str
    path: Path


class DatabaseNotFoundError(Exception):
    pass


class DatabaseUnavailableError(Exception):
    pass


class TableNotFoundError(Exception):
    pass


class RowNotFoundError(Exception):
    pass


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved_path = db_path.resolve()
    uri = f"file:{quote(str(resolved_path), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    configure_readonly_connection(connection)
    return connection


def _mtime_utc_iso(path: Path) -> str:
    return datetime.datetime.fromtimestamp(
        path.stat().st_mtime,
        datetime.timezone.utc,
    ).isoformat()


def _relative_path(output_dir: Path, path: Path) -> str:
    output_root = output_dir.resolve()
    resolved_path = path.resolve()
    if resolved_path.is_relative_to(output_root):
        return resolved_path.relative_to(output_root).as_posix()
    return str(resolved_path)


def _quote_identifier(identifier: str) -> str:
    if "\x00" in identifier:
        raise ValueError("SQLite标识符不能包含NUL字符。")
    return '"' + identifier.replace('"', '""') + '"'


def _table_kind(value: str) -> TableKind:
    if value == "view":
        return "view"
    return "table"


def _read_table_names(connection: sqlite3.Connection) -> list[tuple[str, TableKind]]:
    rows = cast(
        list[tuple[str, str]],
        connection.execute(
            """
            SELECT name, type
            FROM sqlite_schema
            WHERE type IN ('table', 'view')
                AND name NOT LIKE 'sqlite_%'
            ORDER BY name
            """
        ).fetchall(),
    )
    return [(name, _table_kind(table_type)) for name, table_type in rows]


def _read_table_count(db_path: Path) -> int:
    with closing(_connect_readonly(db_path)) as connection:
        return len(_read_table_names(connection))


def _database_summary_for_ref(output_dir: Path, ref: DatabaseRef) -> DatabaseSummary:
    table_count = 0
    status: DatabaseStatus = "ready"
    message: Optional[str] = None
    try:
        table_count = _read_table_count(ref.path)
    except sqlite3.Error as error:
        status = "invalid"
        message = f"SQLite数据库无法读取：{error}"

    return {
        "id": ref.id,
        "kind": ref.kind,
        "label": ref.label,
        "relativePath": _relative_path(output_dir, ref.path),
        "status": status,
        "message": message,
        "sizeBytes": ref.path.stat().st_size,
        "updatedAt": _mtime_utc_iso(ref.path),
        "tableCount": table_count,
    }


def _archive_ref(thread_dir: Path) -> DatabaseRef:
    return DatabaseRef(
        id=f"{_ARCHIVE_DATABASE_PREFIX}{thread_dir.name}",
        kind="archive",
        label=f"{thread_dir.name} / archive.sqlite3",
        path=thread_dir / ARCHIVE_DB_FILENAME,
    )


def list_database_summaries(output_dir: Path) -> list[DatabaseSummary]:
    refs: list[DatabaseRef] = []
    forum_db_path = output_dir / FORUM_THREAD_DB_FILENAME
    if forum_db_path.is_file():
        refs.append(
            DatabaseRef(
                id=_DATABASE_ID_FORUM_THREADS,
                kind="forum_threads",
                label=FORUM_THREAD_DB_FILENAME,
                path=forum_db_path,
            )
        )

    image_index_path = output_dir / image_store.IMAGE_INDEX_FILENAME
    if image_index_path.is_file():
        refs.append(
            DatabaseRef(
                id=_DATABASE_ID_IMAGE_INDEX,
                kind="image_index",
                label=image_store.IMAGE_INDEX_FILENAME,
                path=image_index_path,
            )
        )

    if output_dir.is_dir():
        for thread_dir in sorted(output_dir.iterdir(), key=lambda item: item.name):
            if (
                thread_dir.is_dir()
                and parse_thread_dir_name(thread_dir.name) is not None
                and (thread_dir / ARCHIVE_DB_FILENAME).is_file()
            ):
                refs.append(_archive_ref(thread_dir))

    return [_database_summary_for_ref(output_dir, ref) for ref in refs]


def resolve_database(output_dir: Path, database_id: str) -> DatabaseRef:
    if database_id == _DATABASE_ID_FORUM_THREADS:
        ref = DatabaseRef(
            id=database_id,
            kind="forum_threads",
            label=FORUM_THREAD_DB_FILENAME,
            path=output_dir / FORUM_THREAD_DB_FILENAME,
        )
    elif database_id == _DATABASE_ID_IMAGE_INDEX:
        ref = DatabaseRef(
            id=database_id,
            kind="image_index",
            label=image_store.IMAGE_INDEX_FILENAME,
            path=output_dir / image_store.IMAGE_INDEX_FILENAME,
        )
    elif database_id.startswith(_ARCHIVE_DATABASE_PREFIX):
        thread_dir_name = database_id.removeprefix(_ARCHIVE_DATABASE_PREFIX)
        if parse_thread_dir_name(thread_dir_name) is None:
            raise DatabaseNotFoundError("未知数据库。")
        ref = _archive_ref(output_dir / thread_dir_name)
    else:
        raise DatabaseNotFoundError("未知数据库。")

    output_root = output_dir.resolve()
    resolved_path = ref.path.resolve()
    if not resolved_path.is_relative_to(output_root):
        raise DatabaseNotFoundError("未知数据库。")
    if not resolved_path.is_file():
        raise DatabaseNotFoundError("数据库文件不存在。")
    if ref.kind == "archive":
        ThreadArchiveStore(ref.path.parent).ensure_schema()
    return ref


def _read_column_info(
    connection: sqlite3.Connection,
    table_name: str,
) -> list[ColumnInfo]:
    rows = connection.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    columns: list[ColumnInfo] = []
    for row in rows:
        name = row[1]
        if not isinstance(name, str):
            continue
        raw_type = row[2]
        raw_default = row[4]
        raw_not_null = row[3]
        raw_primary_key = row[5]
        columns.append(
            {
                "name": name,
                "type": raw_type if isinstance(raw_type, str) else "",
                "notNull": raw_not_null == 1,
                "primaryKey": raw_primary_key != 0,
                "defaultValue": None if raw_default is None else str(raw_default),
            }
        )
    return columns


def _table_row(
    connection: sqlite3.Connection,
    table_name: str,
) -> Optional[tuple[str, TableKind]]:
    row = cast(
        Optional[tuple[str, str]],
        connection.execute(
            """
            SELECT name, type
            FROM sqlite_schema
            WHERE type IN ('table', 'view')
                AND name NOT LIKE 'sqlite_%'
                AND name = ?
            """,
            (table_name,),
        ).fetchone(),
    )
    if row is None:
        return None
    return row[0], _table_kind(row[1])


def _require_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> tuple[TableKind, list[ColumnInfo]]:
    table = _table_row(connection, table_name)
    if table is None:
        raise TableNotFoundError("数据表不存在。")
    columns = _read_column_info(connection, table_name)
    return table[1], columns


def _read_row_count(
    connection: sqlite3.Connection,
    table_name: str,
) -> Optional[int]:
    try:
        row = connection.execute(
            f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}"
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None or type(row[0]) is not int:
        return None
    return row[0]


def _read_table_summary(
    connection: sqlite3.Connection,
    table_name: str,
    table_type: TableKind,
) -> TableSummary:
    return {
        "name": table_name,
        "type": table_type,
        "rowCount": _read_row_count(connection, table_name),
        "columns": _read_column_info(connection, table_name),
    }


def read_database_schema(output_dir: Path, database_id: str) -> DatabaseSchema:
    ref = resolve_database(output_dir, database_id)
    database = _database_summary_for_ref(output_dir, ref)
    if database["status"] != "ready":
        raise DatabaseUnavailableError(database["message"] or "数据库无法读取。")

    try:
        with closing(_connect_readonly(ref.path)) as connection:
            tables = [
                _read_table_summary(connection, table_name, table_type)
                for table_name, table_type in _read_table_names(connection)
            ]
    except sqlite3.Error as error:
        raise DatabaseUnavailableError(f"SQLite数据库无法读取：{error}") from error

    return {"database": database, "tables": tables}


def _escaped_like_pattern(query: str) -> str:
    escaped = (
        query.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _search_sql(
    columns: list[ColumnInfo],
    query: str,
) -> tuple[str, list[object]]:
    normalized_query = query.strip()
    if not normalized_query:
        return "", []
    if not columns:
        return " WHERE 0", []
    clauses = [
        f"CAST({_quote_identifier(column['name'])} AS TEXT) LIKE ? ESCAPE '\\'"
        for column in columns
    ]
    pattern = _escaped_like_pattern(normalized_query)
    return " WHERE (" + " OR ".join(clauses) + ")", [pattern for _ in columns]


def _table_has_rowid(connection: sqlite3.Connection, table_name: str) -> bool:
    try:
        connection.execute(
            f"SELECT rowid FROM {_quote_identifier(table_name)} LIMIT 1"
        ).fetchone()
    except sqlite3.Error:
        return False
    return True


def _cell_from_value(
    value: object,
    *,
    max_text_chars: Optional[int],
    max_blob_bytes: Optional[int],
) -> DbCell:
    if value is None:
        return {"kind": "null", "value": None, "truncated": False}
    if type(value) is int:
        return {"kind": "integer", "value": value, "truncated": False}
    if isinstance(value, float):
        return {"kind": "real", "value": value, "truncated": False}
    if isinstance(value, str):
        truncated = max_text_chars is not None and len(value) > max_text_chars
        return {
            "kind": "text",
            "value": value[:max_text_chars] if truncated and max_text_chars else value,
            "truncated": truncated,
        }
    if isinstance(value, bytes):
        truncated = max_blob_bytes is not None and len(value) > max_blob_bytes
        preview = value[:max_blob_bytes] if truncated and max_blob_bytes else value
        return {"kind": "blob", "value": preview.hex(), "truncated": truncated}

    text = str(value)
    truncated = max_text_chars is not None and len(text) > max_text_chars
    return {
        "kind": "other",
        "value": text[:max_text_chars] if truncated and max_text_chars else text,
        "truncated": truncated,
    }


def _row_from_values(
    column_names: list[str],
    values: tuple[object, ...],
    *,
    row_id: Optional[int],
    max_text_chars: Optional[int],
    max_blob_bytes: Optional[int],
) -> TableRow:
    return {
        "rowId": row_id,
        "cells": {
            column_name: _cell_from_value(
                value,
                max_text_chars=max_text_chars,
                max_blob_bytes=max_blob_bytes,
            )
            for column_name, value in zip(column_names, values, strict=True)
        },
    }


def _read_filtered_count(
    connection: sqlite3.Connection,
    table_name: str,
    where_sql: str,
    params: list[object],
) -> int:
    row = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_identifier(table_name)}{where_sql}",
        tuple(params),
    ).fetchone()
    if row is None or type(row[0]) is not int:
        return 0
    return row[0]


def read_table_rows(
    output_dir: Path,
    database_id: str,
    table_name: str,
    *,
    offset: int,
    limit: int,
    query: str = "",
    sort_by: Optional[str] = None,
    sort_direction: SortDirection = "asc",
) -> TableRows:
    if offset < 0:
        raise ValueError("offset必须大于等于0。")
    if limit <= 0:
        raise ValueError("limit必须大于0。")
    if sort_direction not in ("asc", "desc"):
        raise ValueError("sort_direction必须是asc或desc。")

    ref = resolve_database(output_dir, database_id)
    try:
        with closing(_connect_readonly(ref.path)) as connection:
            _table_type, columns = _require_columns(connection, table_name)
            column_names = [column["name"] for column in columns]
            if sort_by is not None and sort_by not in column_names:
                raise ValueError("sort_by必须是当前表字段。")

            where_sql, params = _search_sql(columns, query)
            total = _read_filtered_count(connection, table_name, where_sql, params)
            has_rowid = _table_has_rowid(connection, table_name)
            select_columns = ", ".join(
                _quote_identifier(column_name) for column_name in column_names
            )
            rowid_select = "rowid, " if has_rowid else ""
            order_sql = ""
            direction_sql = sort_direction.upper()
            if sort_by is not None:
                order_sql = f" ORDER BY {_quote_identifier(sort_by)} {direction_sql}"
                if has_rowid:
                    order_sql += ", rowid ASC"
            elif has_rowid:
                order_sql = " ORDER BY rowid ASC"

            rows = cast(
                list[tuple[object, ...]],
                connection.execute(
                    f"""
                    SELECT {rowid_select}{select_columns}
                    FROM {_quote_identifier(table_name)}
                    {where_sql}
                    {order_sql}
                    LIMIT ? OFFSET ?
                    """,
                    (*params, limit, offset),
                ).fetchall(),
            )
    except sqlite3.Error as error:
        raise DatabaseUnavailableError(f"SQLite数据库无法读取：{error}") from error

    table_rows: list[TableRow] = []
    for row in rows:
        row_id: Optional[int] = None
        values = row
        if has_rowid:
            raw_row_id = row[0]
            row_id = raw_row_id if type(raw_row_id) is int else None
            values = row[1:]
        table_rows.append(
            _row_from_values(
                column_names,
                values,
                row_id=row_id,
                max_text_chars=_ROW_PREVIEW_TEXT_LIMIT,
                max_blob_bytes=_ROW_PREVIEW_BLOB_BYTES,
            )
        )

    return {
        "columns": columns,
        "rows": table_rows,
        "total": total,
        "offset": offset,
        "limit": limit,
        "query": query.strip(),
        "sortBy": sort_by,
        "sortDirection": sort_direction,
    }


def read_table_row_detail(
    output_dir: Path,
    database_id: str,
    table_name: str,
    row_id: int,
) -> TableRowDetail:
    if row_id < 1:
        raise ValueError("rowid必须大于0。")

    ref = resolve_database(output_dir, database_id)
    try:
        with closing(_connect_readonly(ref.path)) as connection:
            _table_type, columns = _require_columns(connection, table_name)
            if not _table_has_rowid(connection, table_name):
                raise DatabaseUnavailableError("此表不支持rowid详情。")
            column_names = [column["name"] for column in columns]
            row = cast(
                Optional[tuple[object, ...]],
                connection.execute(
                    f"""
                    SELECT {", ".join(_quote_identifier(name) for name in column_names)}
                    FROM {_quote_identifier(table_name)}
                    WHERE rowid = ?
                    """,
                    (row_id,),
                ).fetchone(),
            )
    except sqlite3.Error as error:
        raise DatabaseUnavailableError(f"SQLite数据库无法读取：{error}") from error

    if row is None:
        raise RowNotFoundError("未找到对应行。")

    return {
        "row": _row_from_values(
            column_names,
            row,
            row_id=row_id,
            max_text_chars=None,
            max_blob_bytes=None,
        )
    }

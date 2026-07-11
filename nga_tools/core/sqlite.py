from __future__ import annotations

import sqlite3
from collections.abc import Sequence

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)

_WRITABLE_CACHE_SIZE_PAGES = -65536
_READONLY_CACHE_SIZE_PAGES = -32768
_WRITABLE_MMAP_SIZE_BYTES = 268435456
_READONLY_MMAP_SIZE_BYTES = 134217728

SQLITE_MAX_VARIABLES = 32766


def configure_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}"
    )
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    connection.execute(f"PRAGMA cache_size = {_WRITABLE_CACHE_SIZE_PAGES}")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute(f"PRAGMA mmap_size = {_WRITABLE_MMAP_SIZE_BYTES}")


def configure_readonly_connection(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MILLISECONDS}"
    )
    connection.execute(f"PRAGMA cache_size = {_READONLY_CACHE_SIZE_PAGES}")
    connection.execute("PRAGMA temp_store = MEMORY")
    connection.execute(f"PRAGMA mmap_size = {_READONLY_MMAP_SIZE_BYTES}")


def iter_in_clause_chunks(values: Sequence[object]) -> list[list[object]]:
    if len(values) <= SQLITE_MAX_VARIABLES:
        return [list(values)]
    return [
        list(values[start : start + SQLITE_MAX_VARIABLES])
        for start in range(0, len(values), SQLITE_MAX_VARIABLES)
    ]

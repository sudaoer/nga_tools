from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from time import perf_counter
from typing import Generator

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)

_WRITABLE_CACHE_SIZE_PAGES = -65536
_READONLY_CACHE_SIZE_PAGES = -32768
_WRITABLE_MMAP_SIZE_BYTES = 268435456
_READONLY_MMAP_SIZE_BYTES = 134217728

SQLITE_MAX_VARIABLES = 32766


class _SQLiteConcurrencyGate:
    """Process-wide, thread-reentrant limiter for backup SQLite work."""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.Lock())
        self._active = 0
        self._local = threading.local()

    @contextmanager
    def operation(self) -> Generator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        from nga_tools.config import DEFAULT_BACKUP_SQLITE_CONCURRENCY, get_config
        from nga_tools.timing import record_timing, record_timing_metric

        concurrency = getattr(
            get_config(),
            "backup_sqlite_concurrency",
            DEFAULT_BACKUP_SQLITE_CONCURRENCY,
        )
        wait_started = perf_counter()
        with self._condition:
            while self._active >= concurrency:
                self._condition.wait()
            self._active += 1
            active = self._active
        wait_seconds = perf_counter() - wait_started
        self._local.depth = 1
        execution_started = perf_counter()
        try:
            yield
        finally:
            execution_seconds = perf_counter() - execution_started
            self._local.depth = 0
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
            record_timing("SQLite排队", wait_seconds)
            record_timing("SQLite执行", execution_seconds)
            record_timing_metric(
                "SQLite最大排队微秒",
                round(wait_seconds * 1_000_000),
            )
            record_timing_metric("SQLite峰值并发", active)


_SQLITE_CONCURRENCY_GATE = _SQLiteConcurrencyGate()


@contextmanager
def sqlite_operation() -> Generator[None]:
    with _SQLITE_CONCURRENCY_GATE.operation():
        yield


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

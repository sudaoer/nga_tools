from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from contextlib import contextmanager
from time import perf_counter
from typing import Generator, Literal

SQLITE_BUSY_TIMEOUT_SECONDS = 30.0
SQLITE_BUSY_TIMEOUT_MILLISECONDS = int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)

_WRITABLE_CACHE_SIZE_PAGES = -65536
_READONLY_CACHE_SIZE_PAGES = -32768
_WRITABLE_MMAP_SIZE_BYTES = 268435456
_READONLY_MMAP_SIZE_BYTES = 134217728

SQLITE_MAX_VARIABLES = 32766


type SQLiteOperationKind = Literal["read", "write"]


class _SQLiteConcurrencyGate:
    """Process-wide, thread-reentrant limiter for one SQLite work class."""

    def __init__(self, kind: SQLiteOperationKind) -> None:
        self.kind = kind
        self._condition = threading.Condition(threading.Lock())
        self._active = 0
        self._local = threading.local()

    @contextmanager
    def operation(self, concurrency: int) -> Generator[None]:
        depth = getattr(self._local, "depth", 0)
        if depth:
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
            return

        from nga_tools.timing import record_timing, record_timing_metric
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
            kind_label = "读取" if self.kind == "read" else "写入"
            record_timing(f"SQLite{kind_label}排队", wait_seconds)
            record_timing(f"SQLite{kind_label}执行", execution_seconds)
            record_timing_metric(
                "SQLite最大排队微秒",
                round(wait_seconds * 1_000_000),
            )
            record_timing_metric("SQLite峰值并发", active)
            record_timing_metric(
                f"SQLite{kind_label}最大排队微秒",
                round(wait_seconds * 1_000_000),
            )
            record_timing_metric(f"SQLite{kind_label}峰值并发", active)


_SQLITE_CONCURRENCY_GATES = {
    "read": _SQLiteConcurrencyGate("read"),
    "write": _SQLiteConcurrencyGate("write"),
}
_READ_CONCURRENCY_LOCK = threading.RLock()
_active_read_concurrency: int | None = None
_read_concurrency_scope_depth = 0


def effective_backup_sqlite_read_concurrency(thread_workers: int) -> int:
    if thread_workers <= 0:
        raise ValueError("备份工作线程数必须大于0。")
    from nga_tools.config import DEFAULT_BACKUP_SQLITE_CONCURRENCY, get_config

    write_concurrency = getattr(
        get_config(),
        "backup_sqlite_concurrency",
        DEFAULT_BACKUP_SQLITE_CONCURRENCY,
    )
    return min(8, max(write_concurrency, thread_workers))


def _sqlite_concurrency(kind: SQLiteOperationKind) -> int:
    from nga_tools.config import (
        DEFAULT_BACKUP_CONFIGS_WORKERS,
        DEFAULT_BACKUP_SQLITE_CONCURRENCY,
        get_config,
    )

    app_config = get_config()
    write_concurrency = getattr(
        app_config,
        "backup_sqlite_concurrency",
        DEFAULT_BACKUP_SQLITE_CONCURRENCY,
    )
    if kind == "write":
        return write_concurrency
    with _READ_CONCURRENCY_LOCK:
        read_concurrency = _active_read_concurrency
    if read_concurrency is not None:
        return read_concurrency
    thread_workers = getattr(
        app_config,
        "backup_configs_workers",
        DEFAULT_BACKUP_CONFIGS_WORKERS,
    )
    return min(8, max(write_concurrency, thread_workers))


@contextmanager
def sqlite_operation(kind: SQLiteOperationKind = "write") -> Generator[None]:
    with _SQLITE_CONCURRENCY_GATES[kind].operation(_sqlite_concurrency(kind)):
        yield


@contextmanager
def use_backup_sqlite_concurrency(thread_workers: int) -> Generator[None]:
    global _active_read_concurrency, _read_concurrency_scope_depth
    read_concurrency = effective_backup_sqlite_read_concurrency(thread_workers)
    with _READ_CONCURRENCY_LOCK:
        if _active_read_concurrency is None:
            _active_read_concurrency = read_concurrency
        elif _active_read_concurrency != read_concurrency:
            raise RuntimeError(
                "备份SQLite并发作用域活动期间不能修改读取并发数："
                f"active={_active_read_concurrency}, requested={read_concurrency}"
            )
        _read_concurrency_scope_depth += 1
    try:
        yield
    finally:
        with _READ_CONCURRENCY_LOCK:
            _read_concurrency_scope_depth -= 1
            if _read_concurrency_scope_depth == 0:
                _active_read_concurrency = None


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

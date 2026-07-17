from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FileState:
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class DatabaseState:
    database: FileState
    wal: FileState


def file_state(path: Path) -> FileState:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return FileState(False, 0, 0)
    return FileState(True, stat_result.st_size, stat_result.st_mtime_ns)


def database_state(
    path: Path,
    *,
    ignore_empty_wal: bool = False,
) -> DatabaseState:
    wal_state = file_state(Path(f"{path}-wal"))
    if ignore_empty_wal and wal_state.size == 0:
        wal_state = FileState(False, 0, 0)
    return DatabaseState(database=file_state(path), wal=wal_state)

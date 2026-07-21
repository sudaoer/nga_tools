from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from typing import Protocol


class ArchiveRepositorySource(Protocol):
    def exists(self) -> bool: ...

    def require_exists(self) -> None: ...

    def connect_write(self) -> sqlite3.Connection: ...

    def connect_read(self) -> sqlite3.Connection: ...

    def write_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def read_connection(self) -> AbstractContextManager[sqlite3.Connection]: ...

    def increment_archive_revision(
        self,
        connection: sqlite3.Connection,
    ) -> None: ...

    def increment_floor_map_revision(
        self,
        connection: sqlite3.Connection,
    ) -> None: ...


class ArchiveRepository:
    def __init__(self, source: ArchiveRepositorySource) -> None:
        self._source = source

    def exists(self) -> bool:
        return self._source.exists()

    def require_exists(self) -> None:
        self._source.require_exists()

    def _write_connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._source.write_connection()

    def _read_connection(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._source.read_connection()

    def _increment_archive_revision(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        self._source.increment_archive_revision(connection)

    def _increment_floor_map_revision(
        self,
        connection: sqlite3.Connection,
    ) -> None:
        self._source.increment_floor_map_revision(connection)

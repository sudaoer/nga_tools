from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from typing import cast

from nga_tools.backup.archive_repository import ArchiveRepository
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    ExactMissingFloorLocatorRead,
    FloorMapEntry,
    PartialFloorMapUpdateResult,
    StoredFloorMap,
)
from nga_tools.core.sqlite import iter_in_clause_chunks


class ArchiveFloorMapRepository(ArchiveRepository):
    @staticmethod
    def _repairable_recovered_missing_floor_entries_from_connection(
        connection: sqlite3.Connection,
    ) -> dict[int, tuple[int, int]]:
        rows = cast(
            list[tuple[object, object, object, object, object]],
            connection.execute(
                """
                WITH latest AS (
                    SELECT
                        lou,
                        pid,
                        ROW_NUMBER() OVER (
                            PARTITION BY lou
                            ORDER BY last_seen_at DESC, id DESC
                        ) AS row_number
                    FROM post_versions
                )
                SELECT
                    floor_map.author_lou,
                    floor_map.original_lou,
                    floor_map.original_pid,
                    latest.pid,
                    metadata.author_uid
                FROM floor_map_entries AS floor_map
                LEFT JOIN latest
                    ON latest.lou = floor_map.author_lou
                    AND latest.row_number = 1
                LEFT JOIN post_latest_metadata AS metadata
                    ON metadata.lou = latest.lou
                    AND metadata.pid = latest.pid
                WHERE floor_map.pid IS NULL
                  AND floor_map.original_lou IS NOT NULL
                ORDER BY floor_map.author_lou
                """
            ).fetchall(),
        )

        repairable: dict[int, tuple[int, int]] = {}
        for (
            raw_author_lou,
            raw_original_lou,
            raw_original_pid,
            raw_latest_pid,
            raw_author_uid,
        ) in rows:
            if (
                type(raw_author_lou) is not int
                or raw_author_lou < 0
                or type(raw_original_lou) is not int
                or raw_original_lou < 0
            ):
                raise ValueError(
                    "archive缺失楼映射定位字段无效："
                    f"{(raw_author_lou, raw_original_lou)!r}"
                )
            if raw_original_pid is not None and (
                type(raw_original_pid) is not int or raw_original_pid <= 0
            ):
                raise ValueError(
                    "archive缺失楼映射original_pid无效："
                    f"author_lou={raw_author_lou}, original_pid={raw_original_pid!r}"
                )

            if raw_latest_pid is None:
                if raw_original_pid is not None:
                    raise ValueError(
                        "archive已恢复缺失楼缺少本地正文："
                        f"author_lou={raw_author_lou}, "
                        f"original_pid={raw_original_pid}"
                    )
                continue
            if type(raw_latest_pid) is not int or raw_latest_pid <= 0:
                raise ValueError(
                    "archive缺失楼本地正文PID无效："
                    f"author_lou={raw_author_lou}, pid={raw_latest_pid!r}"
                )
            if raw_author_uid != -1:
                raise ValueError(
                    "archive缺失楼存在非匿名或缺少元数据的本地正文："
                    f"author_lou={raw_author_lou}, author_uid={raw_author_uid!r}"
                )
            if raw_original_pid is None:
                repairable[raw_author_lou] = (
                    raw_original_lou,
                    raw_latest_pid,
                )
            elif raw_original_pid != raw_latest_pid:
                raise ValueError(
                    "archive已恢复缺失楼PID与本地正文不一致："
                    f"author_lou={raw_author_lou}, "
                    f"original_pid={raw_original_pid}, pid={raw_latest_pid}"
                )
        return repairable

    def read_repairable_recovered_missing_floor_entries(
        self,
    ) -> dict[int, tuple[int, int]]:
        self.require_exists()
        with self._read_connection() as connection:
            return self._repairable_recovered_missing_floor_entries_from_connection(
                connection
            )

    def repair_recovered_missing_floor_entries(self) -> int:
        self.require_exists()
        with self._write_connection() as connection:
            with connection:
                repairable = (
                    self._repairable_recovered_missing_floor_entries_from_connection(
                        connection
                    )
                )
                for author_lou, (original_lou, original_pid) in sorted(
                    repairable.items()
                ):
                    cursor = connection.execute(
                        """
                        UPDATE floor_map_entries
                        SET original_pid = ?
                        WHERE author_lou = ?
                          AND pid IS NULL
                          AND original_lou = ?
                          AND original_pid IS NULL
                        """,
                        (original_pid, author_lou, original_lou),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            "archive缺失楼映射本地修复行数异常："
                            f"author_lou={author_lou}"
                        )
                if repairable:
                    self._increment_floor_map_revision(connection)
        return len(repairable)

    @staticmethod
    def _validate_requested_author_lous(author_lous: Sequence[int]) -> list[int]:
        if any(
            type(author_lou) is not int or author_lou < 0
            for author_lou in author_lous
        ):
            raise ValueError("增量缺失楼author_lou必须是非负整数。")
        return sorted(set(author_lous))

    @staticmethod
    def _placeholders(values: Sequence[object]) -> str:
        return ", ".join("?" for _value in values)

    @staticmethod
    def _validate_floor_map(floor_map: StoredFloorMap) -> None:
        integer_fields = {
            "version": floor_map.version,
            "generation_version": floor_map.generation_version,
            "tid": floor_map.tid,
            "aid": floor_map.aid,
        }
        for field_name, value in integer_fields.items():
            if type(value) is not int:
                raise ValueError(f"楼层映射字段必须是整数：{field_name}")
        if not floor_map.algorithm:
            raise ValueError("楼层映射algorithm不能为空。")
        if not floor_map.input_signature:
            raise ValueError("楼层映射input_signature不能为空。")

        seen_author_lous: set[int] = set()
        for entry in floor_map.entries:
            author_lou = entry["author_lou"]
            pid = entry["pid"]
            original_lou = entry["original_lou"]
            original_pid = entry.get("original_pid")
            candidates = entry.get("candidate_original_lous", [])
            if type(author_lou) is not int:
                raise ValueError("楼层映射author_lou必须是整数。")
            if author_lou in seen_author_lous:
                raise ValueError(f"楼层映射author_lou重复：{author_lou}")
            seen_author_lous.add(author_lou)
            for field_name, value in (
                ("pid", pid),
                ("original_lou", original_lou),
                ("original_pid", original_pid),
            ):
                if value is not None and type(value) is not int:
                    raise ValueError(
                        f"楼层映射{field_name}必须是整数或null：author_lou={author_lou}"
                    )
            if original_pid is not None and (pid is not None or original_lou is None):
                raise ValueError(
                    "楼层映射original_pid仅允许用于已确定原楼层的缺失楼："
                    f"author_lou={author_lou}"
                )
            if original_lou is not None and candidates:
                raise ValueError(
                    f"楼层映射不能同时有确定楼层和候选楼层：author_lou={author_lou}"
                )
            if any(type(candidate) is not int for candidate in candidates):
                raise ValueError(
                    f"楼层映射候选楼层必须都是整数：author_lou={author_lou}"
                )

    @staticmethod
    def _normalized_floor_map(floor_map: StoredFloorMap) -> StoredFloorMap:
        return StoredFloorMap(
            version=floor_map.version,
            generation_version=floor_map.generation_version,
            algorithm=floor_map.algorithm,
            tid=floor_map.tid,
            aid=floor_map.aid,
            input_signature=floor_map.input_signature,
            entries=sorted(
                floor_map.entries,
                key=lambda entry: entry["author_lou"],
            ),
        )

    def replace_floor_map(self, floor_map: StoredFloorMap) -> bool:
        self._validate_floor_map(floor_map)
        normalized_floor_map = self._normalized_floor_map(floor_map)
        self.require_exists()
        with self._write_connection() as connection:
            with connection:
                try:
                    current_floor_map = self.read_floor_map_from_connection(connection)
                except ValueError:
                    current_floor_map = None
                if current_floor_map == normalized_floor_map:
                    return False
                connection.execute("DELETE FROM floor_map_candidates")
                connection.execute("DELETE FROM floor_map_entries")
                connection.execute("DELETE FROM floor_map_state")
                connection.execute(
                    """
                    INSERT INTO floor_map_state (
                        singleton,
                        tid,
                        aid,
                        format_version,
                        generation_version,
                        hash_algorithm,
                        input_signature
                    )
                    VALUES (1, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        normalized_floor_map.tid,
                        normalized_floor_map.aid,
                        normalized_floor_map.version,
                        normalized_floor_map.generation_version,
                        normalized_floor_map.algorithm,
                        normalized_floor_map.input_signature,
                    ),
                )
                for entry in normalized_floor_map.entries:
                    author_lou = entry["author_lou"]
                    connection.execute(
                        """
                        INSERT INTO floor_map_entries (
                            author_lou,
                            pid,
                            original_lou,
                            original_pid
                        )
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            author_lou,
                            entry["pid"],
                            entry["original_lou"],
                            entry.get("original_pid"),
                        ),
                    )
                    for candidate_index, original_lou in enumerate(
                        entry.get("candidate_original_lous", [])
                    ):
                        connection.execute(
                            """
                            INSERT INTO floor_map_candidates (
                                author_lou,
                                candidate_index,
                                original_lou
                            )
                            VALUES (?, ?, ?)
                            """,
                            (author_lou, candidate_index, original_lou),
                        )
                self._increment_floor_map_revision(connection)
        return True

    def read_exact_missing_floor_locators(
        self,
        author_lous: Sequence[int],
        *,
        tid: int,
        aid: int,
        expected_archive_revision: int,
        expected_floor_map_revision: int,
    ) -> ExactMissingFloorLocatorRead:
        requested_lous = self._validate_requested_author_lous(author_lous)
        if not requested_lous:
            return ExactMissingFloorLocatorRead({}, None)
        for label, value in (
            ("tid", tid),
            ("aid", aid),
            ("archive revision", expected_archive_revision),
            ("floor-map revision", expected_floor_map_revision),
        ):
            if type(value) is not int or value < 0:
                raise ValueError(f"增量缺失楼{label}必须是非负整数。")

        self.require_exists()
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            try:
                state_row = connection.execute(
                    """
                    SELECT
                        tid,
                        aid,
                        format_version,
                        generation_version,
                        hash_algorithm,
                        input_signature
                    FROM floor_map_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if (
                    state_row is None
                    or state_row[0] != tid
                    or state_row[1] != aid
                    or state_row[2] != FLOOR_MAP_VERSION
                    or state_row[3] != FLOOR_MAP_GENERATION_VERSION
                    or state_row[4] != FLOOR_MAP_HASH_ALGORITHM
                    or not isinstance(state_row[5], str)
                    or not state_row[5]
                ):
                    return ExactMissingFloorLocatorRead({}, "state_mismatch")

                revision_row = connection.execute(
                    """
                    SELECT archive_revision, floor_map_revision
                    FROM archive_change_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if revision_row != (
                    expected_archive_revision,
                    expected_floor_map_revision,
                ):
                    return ExactMissingFloorLocatorRead({}, "revision_mismatch")

                entry_rows: list[tuple[object, object, object, object]] = []
                candidate_author_lous: set[int] = set()
                for chunk in iter_in_clause_chunks(requested_lous):
                    placeholders = self._placeholders(chunk)
                    entry_rows.extend(
                        cast(
                            list[tuple[object, object, object, object]],
                            connection.execute(
                                f"""
                                SELECT author_lou, pid, original_lou, original_pid
                                FROM floor_map_entries
                                WHERE author_lou IN ({placeholders})
                                """,
                                chunk,
                            ).fetchall(),
                        )
                    )
                    candidate_rows = connection.execute(
                        f"""
                        SELECT DISTINCT author_lou
                        FROM floor_map_candidates
                        WHERE author_lou IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for row in candidate_rows:
                        if len(row) != 1 or type(row[0]) is not int:
                            raise ValueError(
                                f"archive楼层映射候选author_lou无效：{row!r}"
                            )
                        candidate_author_lous.add(row[0])
            finally:
                connection.rollback()

        entries_by_author_lou: dict[int, tuple[object, object, object]] = {}
        for author_lou, pid, original_lou, original_pid in entry_rows:
            if type(author_lou) is not int or author_lou in entries_by_author_lou:
                raise ValueError(f"archive增量楼层映射行无效：{entry_rows!r}")
            entries_by_author_lou[author_lou] = (pid, original_lou, original_pid)

        exact: dict[int, int] = {}
        for author_lou in requested_lous:
            row = entries_by_author_lou.get(author_lou)
            if author_lou in candidate_author_lous:
                return ExactMissingFloorLocatorRead({}, "candidate")
            if row is None or row[1] is None:
                return ExactMissingFloorLocatorRead({}, "no_locator")
            pid, original_lou, original_pid = row
            if (
                pid is not None
                or original_pid is not None
                or type(original_lou) is not int
                or original_lou < 0
            ):
                return ExactMissingFloorLocatorRead({}, "entry_state")
            exact[author_lou] = original_lou
        return ExactMissingFloorLocatorRead(exact, None)

    def mark_missing_floor_entries_recovered(
        self,
        recovered_by_author_lou: Mapping[int, tuple[int, int]],
        *,
        expected_floor_map_revision: int,
    ) -> PartialFloorMapUpdateResult:
        if (
            type(expected_floor_map_revision) is not int
            or expected_floor_map_revision < 0
        ):
            raise ValueError("增量楼层映射预期revision必须是非负整数。")
        rows = sorted(recovered_by_author_lou.items())
        for author_lou, (original_lou, original_pid) in rows:
            if (
                type(author_lou) is not int
                or author_lou < 0
                or type(original_lou) is not int
                or original_lou < 0
                or type(original_pid) is not int
                or original_pid < 0
            ):
                raise ValueError("增量楼层映射恢复字段必须是非负整数。")
        self.require_exists()
        with self._write_connection() as connection:
            with connection:
                revision_row = connection.execute(
                    """
                    SELECT floor_map_revision
                    FROM archive_change_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if revision_row != (expected_floor_map_revision,):
                    return PartialFloorMapUpdateResult(False, 0)
                if not rows:
                    return PartialFloorMapUpdateResult(True, 0)

                rows_to_update: list[tuple[int, int, int]] = []
                for author_lou, (original_lou, original_pid) in rows:
                    entry_row = connection.execute(
                        """
                        SELECT pid, original_lou, original_pid
                        FROM floor_map_entries
                        WHERE author_lou = ?
                        """,
                        (author_lou,),
                    ).fetchone()
                    candidate_row = connection.execute(
                        """
                        SELECT 1
                        FROM floor_map_candidates
                        WHERE author_lou = ?
                        LIMIT 1
                        """,
                        (author_lou,),
                    ).fetchone()
                    if candidate_row is not None:
                        return PartialFloorMapUpdateResult(False, 0)
                    if entry_row == (None, original_lou, original_pid):
                        continue
                    if entry_row != (None, original_lou, None):
                        return PartialFloorMapUpdateResult(False, 0)
                    rows_to_update.append(
                        (author_lou, original_lou, original_pid)
                    )

                for author_lou, original_lou, original_pid in rows_to_update:
                    cursor = connection.execute(
                        """
                        UPDATE floor_map_entries
                        SET original_pid = ?
                        WHERE author_lou = ?
                          AND pid IS NULL
                          AND original_lou = ?
                          AND original_pid IS NULL
                        """,
                        (original_pid, author_lou, original_lou),
                    )
                    if cursor.rowcount != 1:
                        raise RuntimeError(
                            f"增量楼层映射更新行数异常：author_lou={author_lou}"
                        )
                if rows_to_update:
                    self._increment_floor_map_revision(connection)
        return PartialFloorMapUpdateResult(True, len(rows_to_update))

    def read_floor_map_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> StoredFloorMap | None:
        state_row = connection.execute(
            """
            SELECT
                format_version,
                generation_version,
                hash_algorithm,
                tid,
                aid,
                input_signature
            FROM floor_map_state
            WHERE singleton = 1
            """
        ).fetchone()
        if state_row is None:
            return None
        entry_rows = connection.execute(
            """
            SELECT author_lou, pid, original_lou, original_pid
            FROM floor_map_entries
            ORDER BY author_lou
            """
        ).fetchall()
        candidate_rows = connection.execute(
            """
            SELECT author_lou, candidate_index, original_lou
            FROM floor_map_candidates
            ORDER BY author_lou, candidate_index
            """
        ).fetchall()

        candidates_by_author_lou: dict[int, list[int]] = {}
        for author_lou, candidate_index, original_lou in candidate_rows:
            if (
                type(author_lou) is not int
                or type(candidate_index) is not int
                or type(original_lou) is not int
            ):
                raise ValueError(f"archive楼层映射候选行无效：{candidate_rows!r}")
            candidates = candidates_by_author_lou.setdefault(author_lou, [])
            if candidate_index != len(candidates):
                raise ValueError(
                    "archive楼层映射候选序号不连续："
                    f"author_lou={author_lou}, candidate_index={candidate_index}"
                )
            candidates.append(original_lou)

        entries: list[FloorMapEntry] = []
        author_lous: set[int] = set()
        for author_lou, pid, original_lou, original_pid in entry_rows:
            if type(author_lou) is not int:
                raise ValueError(f"archive楼层映射author_lou无效：{author_lou!r}")
            entry: FloorMapEntry = {
                "pid": pid,
                "author_lou": author_lou,
                "original_lou": original_lou,
            }
            if original_pid is not None:
                entry["original_pid"] = original_pid
            candidates = candidates_by_author_lou.get(author_lou)
            if candidates:
                entry["candidate_original_lous"] = candidates
            entries.append(entry)
            author_lous.add(author_lou)
        orphan_candidates = set(candidates_by_author_lou) - author_lous
        if orphan_candidates:
            raise ValueError(
                "archive楼层映射候选缺少对应entry："
                f"{sorted(orphan_candidates)}"
            )

        version, generation_version, algorithm, tid, aid, input_signature = state_row
        if not isinstance(algorithm, str) or not algorithm:
            raise ValueError(f"archive楼层映射algorithm无效：{algorithm!r}")
        if not isinstance(input_signature, str) or not input_signature:
            raise ValueError(
                f"archive楼层映射input_signature无效：{input_signature!r}"
            )
        floor_map = StoredFloorMap(
            version=version,
            generation_version=generation_version,
            algorithm=algorithm,
            tid=tid,
            aid=aid,
            input_signature=input_signature,
            entries=entries,
        )
        self._validate_floor_map(floor_map)
        return floor_map

    def read_unresolved_author_lous_from_connection(
        self,
        connection: sqlite3.Connection,
    ) -> tuple[int, ...]:
        """Read only historical unresolved lous after validating map state."""

        state_row = connection.execute(
            """
            SELECT
                format_version,
                generation_version,
                hash_algorithm,
                tid,
                aid,
                input_signature
            FROM floor_map_state
            WHERE singleton = 1
            """
        ).fetchone()
        if state_row is None:
            return ()
        version, generation_version, algorithm, tid, aid, input_signature = state_row
        for field_name, value in (
            ("version", version),
            ("generation_version", generation_version),
            ("tid", tid),
            ("aid", aid),
        ):
            if type(value) is not int:
                raise ValueError(f"楼层映射字段必须是整数：{field_name}")
        if not isinstance(algorithm, str) or not algorithm:
            raise ValueError(f"archive楼层映射algorithm无效：{algorithm!r}")
        if not isinstance(input_signature, str) or not input_signature:
            raise ValueError(
                f"archive楼层映射input_signature无效：{input_signature!r}"
            )

        rows = connection.execute(
            """
            SELECT author_lou
            FROM floor_map_entries
            WHERE pid IS NULL AND original_pid IS NULL
            ORDER BY author_lou
            """
        ).fetchall()
        unresolved_lous: list[int] = []
        for row in rows:
            if len(row) != 1 or type(row[0]) is not int:
                raise ValueError(f"archive楼层映射author_lou无效：{row!r}")
            unresolved_lous.append(row[0])
        return tuple(unresolved_lous)

    def read_floor_map(self) -> StoredFloorMap | None:
        if not self.exists():
            return None
        with self._read_connection() as connection:
            return self.read_floor_map_from_connection(connection)

from __future__ import annotations

import sqlite3
from contextlib import closing

from nga_tools.backup.archive_repository import ArchiveRepository
from nga_tools.backup.floor_models import FloorMapEntry, StoredFloorMap


class ArchiveFloorMapRepository(ArchiveRepository):
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
        with closing(self._connect_write()) as connection:
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

    def read_floor_map(self) -> StoredFloorMap | None:
        if not self.exists():
            return None
        with closing(self._connect_read()) as connection:
            return self.read_floor_map_from_connection(connection)

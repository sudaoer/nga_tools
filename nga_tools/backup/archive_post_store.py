from __future__ import annotations

import datetime
import sqlite3
from collections.abc import Sequence
from typing import Optional, cast

from nga_tools.backup.archive_floor_store import ArchiveFloorMapRepository
from nga_tools.backup.archive_posts import PostDate, postdate_from_json
from nga_tools.backup.archive_repository import ArchiveRepository, ArchiveRepositorySource
from nga_tools.backup.archive_store_models import (
    ArchiveEffectivePostStats,
    ArchivePagePagination,
    ArchivePostVersionRow,
    AuthorFloorRefreshInputs,
)
from nga_tools.backup.content_codec import decode_content
from nga_tools.backup.floor_models import AuthorPostRef
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_version_selection import (
    PostVersionSelection,
    post_version_selections_fingerprint,
)
from nga_tools.backup.processing_state import CurrentPaginationState
from nga_tools.timing import time_section

_LATEST_POST_RECORDS_QUERY = """
    SELECT latest.id, latest.lou, latest.pid, latest.content, latest.source_hash
    FROM (
        SELECT id, lou, pid, content, source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
        {where_lous}
    ) AS latest
    WHERE latest.row_number = 1
    ORDER BY latest.lou
    """
_LATEST_POST_RECORD_SUMMARIES_QUERY = """
    SELECT id, lou, pid, source_hash
    FROM (
        SELECT id, lou, pid, source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
    )
    WHERE row_number = 1
    ORDER BY lou
    """
_LATEST_POST_ROWS_QUERY = """
    SELECT latest.id, latest.lou, latest.pid, latest.content, latest.source_hash,
        post_latest_metadata.author_name, post_latest_metadata.author_uid,
        post_latest_metadata.postdate_json
    FROM (
        SELECT id, lou, pid, content, source_hash,
            ROW_NUMBER() OVER (
                PARTITION BY lou ORDER BY last_seen_at DESC, id DESC
            ) AS row_number
        FROM post_versions
        {where_lous}
    ) AS latest
    LEFT JOIN post_latest_metadata
        ON post_latest_metadata.pid = latest.pid
        AND post_latest_metadata.lou = latest.lou
    WHERE latest.row_number = 1
    ORDER BY latest.lou
    """
_LATEST_AUTHOR_POST_REFS_QUERY = """
    SELECT latest.pid, latest.lou, metadata.author_uid
    FROM post_versions AS latest
    LEFT JOIN post_latest_metadata AS metadata
        ON metadata.pid = latest.pid AND metadata.lou = latest.lou
    WHERE latest.id = (
        SELECT candidate.id
        FROM post_versions AS candidate
        WHERE candidate.lou = latest.lou
        ORDER BY candidate.last_seen_at DESC, candidate.id DESC
        LIMIT 1
    )
    ORDER BY latest.lou
    """


class ArchivePostRepository(ArchiveRepository):
    def __init__(
        self,
        source: ArchiveRepositorySource,
        floor_maps: ArchiveFloorMapRepository,
    ) -> None:
        super().__init__(source)
        self._floor_maps = floor_maps
    def max_post_version_id(self) -> int:
        self.require_exists()
        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(id), 0) FROM post_versions"
            ).fetchone()
        if row is None or len(row) != 1 or type(row[0]) is not int:
            raise ValueError(f"archive帖子版本最大ID无效：{row!r}")
        if row[0] < 0:
            raise ValueError(f"archive帖子版本最大ID为负数：{row[0]}")
        return row[0]

    def read_post_version_contents(
        self,
        *,
        after_id: int,
        through_id: int,
    ) -> list[tuple[int, str]]:
        if after_id < 0 or through_id < after_id:
            raise ValueError(
                "archive帖子版本扫描范围无效："
                f"after={after_id}, through={through_id}"
            )
        if after_id == through_id:
            return []
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, content
                FROM post_versions
                WHERE id > ? AND id <= ?
                ORDER BY id
                """,
                (after_id, through_id),
            ).fetchall()
        result: list[tuple[int, str]] = []
        for row in rows:
            if (
                len(row) != 2
                or type(row[0]) is not int
                or not isinstance(row[1], bytes)
            ):
                raise ValueError(f"archive帖子版本正文行无效：{row!r}")
            result.append(
                (
                    row[0],
                    decode_content(row[1], source=f"archive帖子版本{row[0]}正文"),
                )
            )
        return result

    def read_latest_post_record_summaries(self) -> list[PostRecord]:
        self.require_exists()

        with self._read_connection() as connection:
            rows = cast(
                list[tuple[int, int, int, str]],
                connection.execute(_LATEST_POST_RECORD_SUMMARIES_QUERY).fetchall(),
            )

        records: list[PostRecord] = []
        for _version_id, lou, pid, source_hash in rows:
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }
            )
        return records

    def _validated_post_version_selections(
        self,
        connection: sqlite3.Connection,
        lous: set[int] | None = None,
    ) -> dict[int, PostVersionSelection]:
        rows = cast(
            list[tuple[object, object, object]],
            connection.execute(
                """
                SELECT lou, version_id, selected_at
                FROM post_version_selections
                ORDER BY lou
                """
            ).fetchall(),
        )

        valid_selections: dict[int, PostVersionSelection] = {}
        for raw_lou, raw_version_id, raw_selected_at in rows:
            if (
                type(raw_lou) is not int
                or raw_lou < 0
                or type(raw_version_id) is not int
                or not isinstance(raw_selected_at, str)
                or not raw_selected_at
            ):
                continue
            lou = raw_lou
            version_id = raw_version_id
            if lous is not None and lou not in lous:
                continue
            version_row = cast(
                Optional[tuple[object, object]],
                connection.execute(
                    """
                    SELECT lou, source_hash
                    FROM post_versions
                    WHERE id = ?
                    """,
                    (version_id,),
                ).fetchone(),
            )
            if version_row is None:
                continue
            version_lou, source_hash = version_row
            if version_lou != lou or not isinstance(source_hash, str):
                continue

            latest_row = cast(
                Optional[tuple[int]],
                connection.execute(
                    """
                    SELECT id
                    FROM post_versions
                    WHERE lou = ?
                    ORDER BY last_seen_at DESC, id DESC
                    LIMIT 1
                    """,
                    (lou,),
                ).fetchone(),
            )
            if latest_row is None or latest_row[0] == version_id:
                continue
            valid_selections[lou] = {
                "version_id": version_id,
                "source_hash": source_hash,
                "selected_at": raw_selected_at,
            }
        return valid_selections

    def read_valid_post_version_selections(self) -> dict[int, PostVersionSelection]:
        self.require_exists()
        with self._read_connection() as connection:
            return self._validated_post_version_selections(connection)

    def post_version_selections_fingerprint(self) -> str:
        return post_version_selections_fingerprint(
            self.read_valid_post_version_selections()
        )

    def upsert_post_version_selection(
        self,
        lou: int,
        version_id: int,
    ) -> PostVersionSelection:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"正文版本选择楼层必须是非负整数：{lou!r}")
        if type(version_id) is not int or version_id < 1:
            raise ValueError(f"正文版本ID必须是正整数：{version_id!r}")

        selected_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._write_connection() as connection:
            with connection:
                version_row = cast(
                    Optional[tuple[object, object]],
                    connection.execute(
                        """
                        SELECT lou, source_hash
                        FROM post_versions
                        WHERE id = ?
                        """,
                        (version_id,),
                    ).fetchone(),
                )
                if version_row is None:
                    raise ValueError("未知帖子正文版本。")
                version_lou, source_hash = version_row
                if version_lou != lou:
                    raise ValueError("帖子正文版本不属于指定楼层。")
                if not isinstance(source_hash, str):
                    raise ValueError("帖子正文版本哈希无效。")

                latest_row = cast(
                    Optional[tuple[object]],
                    connection.execute(
                        """
                        SELECT id
                        FROM post_versions
                        WHERE lou = ?
                        ORDER BY last_seen_at DESC, id DESC
                        LIMIT 1
                        """,
                        (lou,),
                    ).fetchone(),
                )
                if latest_row is None or type(latest_row[0]) is not int:
                    raise ValueError("未知楼层。")
                if latest_row[0] == version_id:
                    raise ValueError("不能手动选择当前最新版。")

                connection.execute(
                    """
                    INSERT INTO post_version_selections (
                        lou,
                        version_id,
                        selected_at
                    )
                    VALUES (?, ?, ?)
                    ON CONFLICT(lou) DO UPDATE SET
                        version_id = excluded.version_id,
                        selected_at = excluded.selected_at
                    """,
                    (lou, version_id, selected_at),
                )

        return {
            "version_id": version_id,
            "source_hash": source_hash,
            "selected_at": selected_at,
        }

    def delete_post_version_selection(self, lou: int) -> int:
        if type(lou) is not int or lou < 0:
            raise ValueError(f"正文版本选择楼层必须是非负整数：{lou!r}")
        with self._write_connection() as connection:
            with connection:
                latest_row = cast(
                    Optional[tuple[object]],
                    connection.execute(
                        """
                        SELECT id
                        FROM post_versions
                        WHERE lou = ?
                        ORDER BY last_seen_at DESC, id DESC
                        LIMIT 1
                        """,
                        (lou,),
                    ).fetchone(),
                )
                if latest_row is None or type(latest_row[0]) is not int:
                    raise ValueError("未知楼层。")
                connection.execute(
                    "DELETE FROM post_version_selections WHERE lou = ?",
                    (lou,),
                )
        return latest_row[0]

    def read_effective_post_stats(self) -> ArchiveEffectivePostStats:
        self.require_exists()
        with self._read_connection() as connection:
            row = cast(
                tuple[int, Optional[int]],
                connection.execute(
                    """
                    WITH latest AS (
                        SELECT
                            lou,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    )
                    SELECT COUNT(*), MAX(lou)
                    FROM latest
                    WHERE row_number = 1
                    """
                ).fetchone(),
            )
        return ArchiveEffectivePostStats(post_count=row[0], max_lou=row[1])

    def read_effective_post_record_summaries(self) -> list[PostRecord]:
        self.require_exists()

        with self._read_connection() as connection:
            rows = cast(
                list[tuple[int, int, int, str]],
                connection.execute(_LATEST_POST_RECORD_SUMMARIES_QUERY).fetchall(),
            )
            records_by_lou: dict[int, PostRecord] = {
                lou: {
                    "lou": lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }
                for _version_id, lou, pid, source_hash in rows
            }

            valid_selections = self._validated_post_version_selections(connection)
            for _lou, selection in valid_selections.items():
                selected_row = cast(
                    Optional[tuple[int, int, str]],
                    connection.execute(
                        """
                        SELECT lou, pid, source_hash
                        FROM post_versions
                        WHERE id = ?
                        """,
                        (selection["version_id"],),
                    ).fetchone(),
                )
                if selected_row is None:
                    continue
                selected_lou, pid, source_hash = selected_row
                records_by_lou[selected_lou] = {
                    "lou": selected_lou,
                    "pid": pid,
                    "post": None,
                    "html": None,
                    "source_hash": source_hash,
                }

        return [records_by_lou[lou] for lou in sorted(records_by_lou)]

    @staticmethod
    def _effective_post_row_from_sql_row(
        row: tuple[
            int,
            int,
            int,
            object,
            str,
            Optional[str],
            Optional[int],
            Optional[str],
        ],
        *,
        manual_selection: bool,
    ) -> ArchivePostVersionRow:
        (
            version_id,
            lou,
            pid,
            content,
            source_hash,
            author_name,
            author_uid,
            postdate_json,
        ) = row
        return ArchivePostVersionRow(
            version_id=version_id,
            lou=lou,
            pid=pid,
            content=decode_content(
                content,
                source=f"archive帖子版本{version_id}正文",
            ),
            source_hash=source_hash,
            author_name=author_name,
            author_uid=author_uid,
            postdate_json=postdate_json,
            manual_selection=manual_selection,
        )

    def read_effective_post_rows(
        self,
        lous: set[int] | None = None,
    ) -> list[ArchivePostVersionRow]:
        self.require_exists()
        if lous is not None and not lous:
            return []

        params: tuple[int, ...] = ()
        where_lous = ""
        if lous is not None:
            sorted_lous = tuple(sorted(lous))
            placeholders = ",".join("?" for _ in sorted_lous)
            where_lous = f"WHERE lou IN ({placeholders})"
            params = sorted_lous

        with self._read_connection() as connection:
            latest_rows = cast(
                list[
                    tuple[
                        int,
                        int,
                        int,
                        object,
                        str,
                        Optional[str],
                        Optional[int],
                        Optional[str],
                    ]
                ],
                connection.execute(
                    _LATEST_POST_ROWS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )
            rows_by_lou = {
                row[1]: self._effective_post_row_from_sql_row(
                    row,
                    manual_selection=False,
                )
                for row in latest_rows
            }

            valid_selections = self._validated_post_version_selections(
                connection,
                lous,
            )
            for lou, selection in valid_selections.items():
                selected_row = cast(
                    Optional[
                        tuple[
                            int,
                            int,
                            int,
                            object,
                            str,
                            Optional[str],
                            Optional[int],
                            Optional[str],
                        ]
                    ],
                    connection.execute(
                        """
                        SELECT
                            post_versions.id,
                            post_versions.lou,
                            post_versions.pid,
                            post_versions.content,
                            post_versions.source_hash,
                            post_latest_metadata.author_name,
                            post_latest_metadata.author_uid,
                            post_latest_metadata.postdate_json
                        FROM post_versions
                        LEFT JOIN post_latest_metadata
                            ON post_latest_metadata.pid = post_versions.pid
                            AND post_latest_metadata.lou = post_versions.lou
                        WHERE post_versions.id = ?
                        """,
                        (selection["version_id"],),
                    ).fetchone(),
                )
                if selected_row is None:
                    continue
                rows_by_lou[lou] = self._effective_post_row_from_sql_row(
                    selected_row,
                    manual_selection=True,
                )

        return [rows_by_lou[lou] for lou in sorted(rows_by_lou)]

    def read_post_row_for_version(
        self,
        version_id: int,
    ) -> ArchivePostVersionRow | None:
        self.require_exists()
        with self._read_connection() as connection:
            row = cast(
                Optional[
                    tuple[
                        int,
                        int,
                        int,
                        object,
                        str,
                        Optional[str],
                        Optional[int],
                        Optional[str],
                    ]
                ],
                connection.execute(
                    """
                    SELECT
                        post_versions.id,
                        post_versions.lou,
                        post_versions.pid,
                        post_versions.content,
                        post_versions.source_hash,
                        post_latest_metadata.author_name,
                        post_latest_metadata.author_uid,
                        post_latest_metadata.postdate_json
                    FROM post_versions
                    LEFT JOIN post_latest_metadata
                        ON post_latest_metadata.pid = post_versions.pid
                        AND post_latest_metadata.lou = post_versions.lou
                    WHERE post_versions.id = ?
                    """,
                    (version_id,),
                ).fetchone(),
            )
            if row is None:
                return None
            return self._effective_post_row_from_sql_row(
                row,
                manual_selection=False,
            )

    def read_latest_post_records(self, lous: set[int] | None = None) -> list[PostRecord]:
        self.require_exists()
        if lous is not None and not lous:
            return []

        params: tuple[int, ...] = ()
        where_lous = ""
        if lous is not None:
            sorted_lous = tuple(sorted(lous))
            placeholders = ",".join("?" for _ in sorted_lous)
            where_lous = f"WHERE lou IN ({placeholders})"
            params = sorted_lous

        with self._read_connection() as connection:
            rows = cast(
                list[tuple[int, int, int, object, str]],
                connection.execute(
                    _LATEST_POST_RECORDS_QUERY.format(where_lous=where_lous),
                    params,
                ).fetchall(),
            )

        records: list[PostRecord] = []
        for _version_id, lou, pid, raw_content, source_hash in rows:
            content = decode_content(
                raw_content,
                source=f"archive帖子版本{_version_id}正文",
            )
            records.append(
                {
                    "lou": lou,
                    "pid": pid,
                    "post": {
                        "lou": lou,
                        "pid": pid,
                        "content": content,
                    },
                    "html": None,
                    "source_hash": source_hash,
                }
            )
        return records

    def read_effective_post_records(
        self,
        lous: set[int] | None = None,
    ) -> list[PostRecord]:
        records: list[PostRecord] = []
        for row in self.read_effective_post_rows(lous):
            records.append(
                {
                    "lou": row.lou,
                    "pid": row.pid,
                    "post": {
                        "lou": row.lou,
                        "pid": row.pid,
                        "content": row.content,
                    },
                    "html": None,
                    "source_hash": row.source_hash,
                }
            )
        return records

    @staticmethod
    def _read_latest_author_post_refs(
        connection: sqlite3.Connection,
    ) -> list[AuthorPostRef]:
        rows = cast(
            list[tuple[int, int, Optional[int]]],
            connection.execute(_LATEST_AUTHOR_POST_REFS_QUERY).fetchall(),
        )
        return [
            {"pid": pid, "author_lou": lou}
            for pid, lou, author_uid in rows
            if author_uid != -1
        ]

    def read_latest_author_post_refs(self) -> list[AuthorPostRef]:
        self.require_exists()
        with self._read_connection() as connection:
            return self._read_latest_author_post_refs(connection)

    def read_next_postdates_after_lous(
        self,
        after_lous: Sequence[int],
    ) -> dict[int, Optional[PostDate]]:
        """Read the first archived post date after each requested author lou."""
        ordered_lous = sorted(set(after_lous))
        if not ordered_lous:
            return {}
        self.require_exists()
        result: dict[int, Optional[PostDate]] = {}
        with self._read_connection() as connection:
            for after_lou in ordered_lous:
                row = cast(
                    Optional[tuple[object, object]],
                    connection.execute(
                        """
                        SELECT latest.lou, metadata.postdate_json
                        FROM (
                            SELECT id, lou, pid
                            FROM (
                                SELECT id, lou, pid,
                                    ROW_NUMBER() OVER (
                                        PARTITION BY lou
                                        ORDER BY last_seen_at DESC, id DESC
                                    ) AS row_number
                                FROM post_versions
                                WHERE lou > ?
                            )
                            WHERE row_number = 1
                            ORDER BY lou
                            LIMIT 1
                        ) AS latest
                        LEFT JOIN post_latest_metadata AS metadata
                            ON metadata.pid = latest.pid
                            AND metadata.lou = latest.lou
                        """,
                        (after_lou,),
                    ).fetchone(),
                )
                if row is None:
                    result[after_lou] = None
                    continue
                next_lou, postdate_json = row
                if type(next_lou) is not int or next_lou <= after_lou:
                    raise ValueError(
                        f"archive下一有效楼无效：{(after_lou, row)!r}"
                    )
                if postdate_json is not None and not isinstance(
                    postdate_json,
                    str,
                ):
                    raise ValueError(
                        f"archive下一有效楼发帖时间无效：{row!r}"
                    )
                result[after_lou] = postdate_from_json(postdate_json)
        return result

    def read_author_floor_refresh_inputs(self) -> AuthorFloorRefreshInputs:
        """Read author refs and historical unresolved lous in one snapshot."""
        self.require_exists()
        with self._read_connection() as connection:
            connection.execute("BEGIN")
            try:
                with time_section("楼主最新回复索引读取"):
                    post_refs = self._read_latest_author_post_refs(connection)
                try:
                    with time_section("历史未恢复缺失楼读取"):
                        historical_unresolved_lous = (
                            self._floor_maps.read_unresolved_author_lous_from_connection(
                                connection,
                            )
                        )
                except ValueError as error:
                    return AuthorFloorRefreshInputs(
                        tuple(post_refs),
                        (),
                        str(error),
                    )
            finally:
                connection.rollback()
        return AuthorFloorRefreshInputs(
            tuple(post_refs),
            historical_unresolved_lous,
            None,
        )

    def read_latest_author_total_lou_count(self) -> Optional[int]:
        self.require_exists()
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT vrows
                FROM archive_pages
                WHERE page_number = 1
                """
            ).fetchone()

        if row is None:
            return None
        value = row[0]
        if value is None:
            return None
        if type(value) is int:
            return value
        raise ValueError(f"archive vrows字段无效：{value!r}")

    def read_latest_page_one_pagination(
        self,
    ) -> ArchivePagePagination | None:
        self.require_exists()
        with self._read_connection() as connection:
            row = cast(
                Optional[tuple[object, object]],
                connection.execute(
                    """
                    SELECT total_page, vrows
                    FROM archive_pages
                    WHERE page_number = 1
                    """
                ).fetchone(),
            )

        if row is None:
            return None
        total_page, vrows = row
        if total_page is None:
            page_count = 1
        elif type(total_page) is int:
            page_count = total_page
        else:
            raise ValueError(f"archive totalPage字段无效：{total_page!r}")
        if vrows is not None and type(vrows) is not int:
            raise ValueError(f"archive vrows字段无效：{vrows!r}")
        return ArchivePagePagination(page_count, vrows)

    def read_latest_pagination_observation(
        self,
    ) -> CurrentPaginationState | None:
        self.require_exists()
        with self._read_connection() as connection:
            row = cast(
                Optional[tuple[object, object, object, object]],
                connection.execute(
                    """
                    SELECT total_page, vrows, page_number, last_seen_at
                    FROM archive_pages
                    ORDER BY last_seen_at DESC, page_number DESC
                    LIMIT 1
                    """
                ).fetchone(),
            )
        if row is None:
            return None
        total_page, vrows, source_page_number, observed_at = row
        if total_page is None:
            page_count = 1
        elif type(total_page) is int and total_page >= 1:
            page_count = total_page
        else:
            raise ValueError(f"archive totalPage字段无效：{total_page!r}")
        if vrows is not None and (type(vrows) is not int or vrows < 0):
            raise ValueError(f"archive vrows字段无效：{vrows!r}")
        if type(source_page_number) is not int or source_page_number < 1:
            raise ValueError(
                f"archive page_number字段无效：{source_page_number!r}"
            )
        if not isinstance(observed_at, str) or not observed_at:
            raise ValueError(f"archive last_seen_at字段无效：{observed_at!r}")
        try:
            parsed_observed_at = datetime.datetime.fromisoformat(observed_at)
        except ValueError as error:
            raise ValueError(
                f"archive last_seen_at字段无效：{observed_at!r}"
            ) from error
        if (
            parsed_observed_at.tzinfo is None
            or parsed_observed_at.utcoffset() is None
        ):
            raise ValueError(
                f"archive last_seen_at字段缺少时区：{observed_at!r}"
            )
        return CurrentPaginationState(
            page_count=page_count,
            author_total_lou_count=vrows,
            source_page_number=source_page_number,
            observed_at=parsed_observed_at,
        )

    def read_page_numbers(self) -> set[int]:
        if not self.exists():
            return set()
        with self._read_connection() as connection:
            rows = cast(
                list[tuple[int]],
                connection.execute(
                    "SELECT page_number FROM archive_pages"
                ).fetchall(),
            )

        page_numbers: set[int] = set()
        for (page_number,) in rows:
            if type(page_number) is not int:
                raise ValueError(f"archive page_number字段无效：{page_number!r}")
            page_numbers.add(page_number)
        return page_numbers

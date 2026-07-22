from __future__ import annotations

import datetime
import sqlite3
from typing import Optional, cast

from nga_tools.backup.archive_posts import (
    ArchivePostMetadata,
    metadata_from_raw_post,
)
from nga_tools.backup.archive_repository import ArchiveRepository
from nga_tools.backup.archive_store_models import (
    ArchivePagesUpsertResult,
    ArchivePageUpsertResult,
    PreparedArchivePage,
    PreparedArchivePost,
    RecoveredPostsUpsertResult,
)
from nga_tools.backup.content_codec import decode_content, encode_content
from nga_tools.backup.floor_models import RecoveredMissingPost
from nga_tools.backup.models import PostData
from nga_tools.backup.post_data import post_data_from_raw, post_source_hash
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import time_section
from nga_tools.word_count import WORD_COUNT_VERSION, TextWordCount, count_post_content

_EMPTY_IMAGE_ATTACHMENTS_JSON = "[]"


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _optional_int(data: dict[str, object], key: str) -> Optional[int]:
    value = data.get(key)
    if type(value) is int:
        return value
    return None


class ArchiveIngestRepository(ArchiveRepository):
    def _upsert_archive_page(
        self,
        connection: sqlite3.Connection,
        page: PreparedArchivePage,
    ) -> None:
        connection.execute(
            """
            INSERT INTO archive_pages (
                page_number,
                total_page,
                vrows,
                last_seen_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(page_number) DO UPDATE SET
                total_page = excluded.total_page,
                vrows = excluded.vrows,
                last_seen_at = excluded.last_seen_at
            WHERE excluded.last_seen_at >= archive_pages.last_seen_at
            """,
            (
                page.page_number,
                page.total_page,
                page.vrows,
                page.observed_at,
            ),
        )

    def _read_existing_post_version_keys(
        self,
        connection: sqlite3.Connection,
        keys: set[tuple[int, int, str]],
    ) -> set[tuple[int, int, str]]:
        existing: set[tuple[int, int, str]] = set()
        ordered_keys = sorted(keys)
        for start in range(0, len(ordered_keys), 800):
            chunk = ordered_keys[start : start + 800]
            placeholders = ", ".join("(?, ?, ?)" for _key in chunk)
            parameters: list[object] = []
            for key in chunk:
                parameters.extend(key)
            rows = connection.execute(
                f"""
                SELECT pid, lou, source_hash
                FROM post_versions
                WHERE (pid, lou, source_hash) IN ({placeholders})
                """,
                parameters,
            ).fetchall()
            for row in rows:
                if (
                    len(row) != 3
                    or type(row[0]) is not int
                    or type(row[1]) is not int
                    or not isinstance(row[2], str)
                ):
                    raise ValueError(f"archive帖子版本键无效：{row!r}")
                existing.add((row[0], row[1], row[2]))
        return existing

    def _upsert_post_version(
        self,
        connection: sqlite3.Connection,
        post: PostData,
        observed_at: str,
        *,
        count_observation: bool,
        source_hash: str | None = None,
        word_count: TextWordCount | None = None,
        existing_keys: set[tuple[int, int, str]] | None = None,
    ) -> bool:
        if source_hash is None:
            source_hash = post_source_hash(post)
        if word_count is None:
            word_count = count_post_content(post["content"])
        key = (post["pid"], post["lou"], source_hash)
        if existing_keys is None:
            existing_keys = self._read_existing_post_version_keys(
                connection,
                {key},
            )
        seen_increment = 1 if count_observation else 0
        if key in existing_keys:
            cursor = connection.execute(
                """
                UPDATE post_versions SET
                    word_count_version = ?,
                    word_count_chinese_chars = ?,
                    word_count_chinese_with_punctuation = ?,
                    first_seen_at = CASE
                        WHEN first_seen_at > ? THEN ? ELSE first_seen_at
                    END,
                    last_seen_at = CASE
                        WHEN last_seen_at < ? THEN ? ELSE last_seen_at
                    END,
                    seen_count = seen_count + ?
                WHERE pid = ? AND lou = ? AND source_hash = ?
                """,
                (
                    WORD_COUNT_VERSION,
                    word_count.chinese_chars,
                    word_count.chinese_with_punctuation,
                    observed_at,
                    observed_at,
                    observed_at,
                    observed_at,
                    seen_increment,
                    *key,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"更新archive帖子版本失败：{key!r}")
            return False

        cursor = connection.execute(
            """
            INSERT INTO post_versions (
                pid, lou, source_hash, content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at, last_seen_at, seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pid, lou, source_hash) DO NOTHING
            """,
            (
                *key,
                encode_content(post["content"]),
                WORD_COUNT_VERSION,
                word_count.chinese_chars,
                word_count.chinese_with_punctuation,
                observed_at,
                observed_at,
            ),
        )
        if cursor.rowcount == 1:
            existing_keys.add(key)
            return True

        existing_keys.add(key)
        cursor = connection.execute(
            """
            UPDATE post_versions SET
                word_count_version = ?,
                word_count_chinese_chars = ?,
                word_count_chinese_with_punctuation = ?,
                first_seen_at = CASE
                    WHEN first_seen_at > ? THEN ? ELSE first_seen_at
                END,
                last_seen_at = CASE
                    WHEN last_seen_at < ? THEN ? ELSE last_seen_at
                END,
                seen_count = seen_count + ?
            WHERE pid = ? AND lou = ? AND source_hash = ?
            """,
            (
                WORD_COUNT_VERSION,
                word_count.chinese_chars,
                word_count.chinese_with_punctuation,
                observed_at,
                observed_at,
                observed_at,
                observed_at,
                seen_increment,
                *key,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError(f"写入archive帖子版本失败：{key!r}")
        return False

    def _upsert_post_latest_metadata(
        self,
        connection: sqlite3.Connection,
        raw_post: object,
        post: PostData,
        observed_at: str,
        *,
        count_observation: bool,
        metadata: ArchivePostMetadata | None = None,
    ) -> None:
        if metadata is None:
            metadata = metadata_from_raw_post(raw_post)
        self._upsert_post_latest_metadata_batch(
            connection,
            [(post, observed_at, count_observation, metadata)],
        )

    @staticmethod
    def _upsert_post_latest_metadata_batch(
        connection: sqlite3.Connection,
        items: list[tuple[PostData, str, bool, ArchivePostMetadata]],
    ) -> None:
        rows = [
            (
                post["pid"],
                post["lou"],
                metadata["author_name"],
                metadata["author_uid"],
                metadata["postdate_json"],
                _EMPTY_IMAGE_ATTACHMENTS_JSON,
                observed_at,
                observed_at,
                1 if count_observation else 0,
            )
            for post, observed_at, count_observation, metadata in items
        ]
        connection.executemany(
            """
            INSERT INTO post_latest_metadata (
                pid,
                lou,
                author_name,
                author_uid,
                postdate_json,
                image_attachments_json,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(pid, lou) DO UPDATE SET
                author_name = excluded.author_name,
                author_uid = excluded.author_uid,
                postdate_json = excluded.postdate_json,
                image_attachments_json = excluded.image_attachments_json,
                first_seen_at = CASE
                    WHEN post_latest_metadata.first_seen_at > excluded.first_seen_at
                    THEN excluded.first_seen_at
                    ELSE post_latest_metadata.first_seen_at
                END,
                last_seen_at = CASE
                    WHEN post_latest_metadata.last_seen_at < excluded.last_seen_at
                    THEN excluded.last_seen_at
                    ELSE post_latest_metadata.last_seen_at
                END,
                seen_count = post_latest_metadata.seen_count + ?
            """,
            rows,
        )

    def _read_effective_processing_inputs(
        self,
        connection: sqlite3.Connection,
        lous: set[int],
    ) -> dict[int, tuple[int, int, str, Optional[int]]]:
        inputs_by_lou: dict[
            int,
            tuple[int, int, str, Optional[int]],
        ] = {}
        sorted_lous = sorted(lous)
        for start in range(0, len(sorted_lous), 900):
            chunk = sorted_lous[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    f"""
                    SELECT
                        latest.lou,
                        latest.pid,
                        latest.source_hash,
                        metadata.author_uid
                    FROM (
                        SELECT
                            lou,
                            pid,
                            source_hash,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                        WHERE lou IN ({placeholders})
                    ) AS latest
                    LEFT JOIN post_latest_metadata AS metadata
                        ON metadata.pid = latest.pid
                        AND metadata.lou = latest.lou
                    WHERE latest.row_number = 1
                    """,
                    chunk,
                ).fetchall(),
            )
            for lou, pid, source_hash, author_uid in rows:
                if (
                    type(lou) is not int
                    or type(pid) is not int
                    or not isinstance(source_hash, str)
                    or (author_uid is not None and type(author_uid) is not int)
                ):
                    raise ValueError(f"archive有效处理输入无效：{rows!r}")
                inputs_by_lou[lou] = (
                    lou,
                    pid,
                    source_hash,
                    author_uid,
                )
        return inputs_by_lou

    @staticmethod
    def _prepare_archive_page(
        page_number: int,
        page_data: PageData,
        *,
        observed_at: str,
        count_observation: bool,
    ) -> PreparedArchivePage:
        raw_posts = page_data.get("result")
        if not isinstance(raw_posts, list):
            raise ValueError("NGA响应中缺少帖子列表。")
        raw_post_items = cast(list[object], raw_posts)
        prepared_posts: list[PreparedArchivePost] = []
        for raw_post in raw_post_items:
            post = post_data_from_raw(raw_post)
            prepared_posts.append(
                PreparedArchivePost(
                    raw_post=raw_post,
                    post=post,
                    source_hash=post_source_hash(post),
                    word_count=count_post_content(post["content"]),
                    metadata=metadata_from_raw_post(raw_post),
                )
            )
        return PreparedArchivePage(
            page_number=page_number,
            total_page=_optional_int(page_data, "totalPage"),
            vrows=_optional_int(page_data, "vrows"),
            observed_at=observed_at,
            count_observation=count_observation,
            posts=tuple(prepared_posts),
        )

    def page_effective_processing_inputs_changed(
        self,
        page_number: int,
        page_data: PageData,
    ) -> bool:
        prepared_page = self._prepare_archive_page(
            page_number,
            page_data,
            observed_at=_now_utc_iso(),
            count_observation=False,
        )
        affected_lous = {item.post["lou"] for item in prepared_page.posts}
        with self._read_connection() as connection:
            inputs_before = self._read_effective_processing_inputs(
                connection,
                affected_lous,
            )
        inputs_after = {
            item.post["lou"]: (
                item.post["lou"],
                item.post["pid"],
                item.source_hash,
                item.metadata["author_uid"],
            )
            for item in prepared_page.posts
        }
        return any(
            inputs_before.get(lou) != inputs_after.get(lou)
            for lou in affected_lous
        )

    def upsert_pages(
        self,
        page_data_by_page: dict[int, PageData],
        *,
        observed_at: str | None = None,
        count_observation: bool = True,
    ) -> ArchivePagesUpsertResult:
        with time_section("归档页面准备"):
            prepared_pages = [
                self._prepare_archive_page(
                    page_number,
                    page_data_by_page[page_number],
                    observed_at=(
                        _now_utc_iso() if observed_at is None else observed_at
                    ),
                    count_observation=count_observation,
                )
                for page_number in sorted(page_data_by_page)
            ]
        if not prepared_pages:
            return ArchivePagesUpsertResult(
                pages_processed=0,
                post_versions_inserted=0,
                effective_processing_inputs_changed=False,
                effective_changed_pages=0,
                effective_changed_lous=frozenset(),
                effective_added_lous=frozenset(),
            )

        affected_lous_by_page = {
            page.page_number: {item.post["lou"] for item in page.posts}
            for page in prepared_pages
        }
        affected_lous: set[int] = {
            lou
            for page_lous in affected_lous_by_page.values()
            for lou in page_lous
        }
        post_versions_inserted = 0
        changed_lous: set[int] = set()
        prepared_posts = [
            (page, prepared_post)
            for page in prepared_pages
            for prepared_post in page.posts
        ]
        version_keys = {
            (
                prepared_post.post["pid"],
                prepared_post.post["lou"],
                prepared_post.source_hash,
            )
            for _page, prepared_post in prepared_posts
        }

        with self._write_connection() as connection:
            connection.execute("BEGIN")
            try:
                with time_section("归档版本预读"):
                    inputs_before = self._read_effective_processing_inputs(
                        connection,
                        affected_lous,
                    )
                    existing_version_keys = (
                        self._read_existing_post_version_keys(
                            connection,
                            version_keys,
                        )
                    )
                with time_section("归档版本写入"):
                    for page in prepared_pages:
                        self._upsert_archive_page(connection, page)
                    for page, prepared_post in prepared_posts:
                        version_inserted = self._upsert_post_version(
                            connection,
                            prepared_post.post,
                            page.observed_at,
                            count_observation=page.count_observation,
                            source_hash=prepared_post.source_hash,
                            word_count=prepared_post.word_count,
                            existing_keys=existing_version_keys,
                        )
                        if version_inserted:
                            post_versions_inserted += 1
                with time_section("归档元数据写入"):
                    self._upsert_post_latest_metadata_batch(
                        connection,
                        [
                            (
                                prepared_post.post,
                                page.observed_at,
                                page.count_observation,
                                prepared_post.metadata,
                            )
                            for page, prepared_post in prepared_posts
                        ],
                    )
                with time_section("归档有效输入比较"):
                    inputs_after = self._read_effective_processing_inputs(
                        connection,
                        affected_lous,
                    )
                    changed_lous = {
                        lou
                        for lou in affected_lous
                        if inputs_before.get(lou) != inputs_after.get(lou)
                    }
                    if changed_lous:
                        self._increment_archive_revision(connection)
                with time_section("归档事务提交"):
                    connection.commit()
            except BaseException:
                connection.rollback()
                raise

        return ArchivePagesUpsertResult(
            pages_processed=len(prepared_pages),
            post_versions_inserted=post_versions_inserted,
            effective_processing_inputs_changed=bool(changed_lous),
            effective_changed_pages=sum(
                bool(page_lous & changed_lous)
                for page_lous in affected_lous_by_page.values()
            ),
            effective_changed_lous=frozenset(changed_lous),
            effective_added_lous=frozenset(
                lou for lou in changed_lous if lou not in inputs_before
            ),
        )

    def upsert_page(
        self,
        page_number: int,
        page_data: PageData,
        *,
        observed_at: str | None = None,
        count_observation: bool = True,
    ) -> ArchivePageUpsertResult:
        result = self.upsert_pages(
            {page_number: page_data},
            observed_at=observed_at,
            count_observation=count_observation,
        )

        return ArchivePageUpsertResult(
            post_versions_inserted=result.post_versions_inserted,
            effective_processing_inputs_changed=(
                result.effective_processing_inputs_changed
            ),
            effective_changed_lous=result.effective_changed_lous,
            effective_added_lous=result.effective_added_lous,
        )

    def upsert_recovered_posts(
        self,
        recovered_posts_by_author_lou: dict[int, RecoveredMissingPost],
        *,
        observed_at: str | None = None,
    ) -> RecoveredPostsUpsertResult:
        if not recovered_posts_by_author_lou:
            return RecoveredPostsUpsertResult(
                0,
                frozenset(),
                frozenset(),
            )

        observed_at = _now_utc_iso() if observed_at is None else observed_at
        inserted_count = 0
        affected_lous = set(recovered_posts_by_author_lou)
        changed_lous: set[int] = set()
        with self._write_connection() as connection:
            with connection:
                inputs_before = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                for author_lou, recovered in sorted(
                    recovered_posts_by_author_lou.items()
                ):
                    raw_post = dict(recovered["raw_post"])
                    raw_post["lou"] = author_lou
                    raw_post["pid"] = recovered["original_pid"]
                    raw_post["content"] = recovered["content"]
                    metadata = metadata_from_raw_post(raw_post)
                    if metadata["author_uid"] != -1:
                        raise ValueError(
                            f"恢复第{author_lou}楼时原帖不是匿名帖子。"
                        )

                    post = post_data_from_raw(
                        raw_post,
                        source=f"恢复的匿名原帖第{recovered['original_lou']}楼",
                    )
                    inserted = self._upsert_post_version(
                        connection,
                        post,
                        observed_at,
                        count_observation=False,
                    )
                    self._upsert_post_latest_metadata(
                        connection,
                        raw_post,
                        post,
                        observed_at,
                        count_observation=False,
                    )
                    if inserted:
                        inserted_count += 1
                inputs_after = self._read_effective_processing_inputs(
                    connection,
                    affected_lous,
                )
                changed_lous = {
                    lou
                    for lou in affected_lous
                    if inputs_before.get(lou) != inputs_after.get(lou)
                }
                if changed_lous:
                    self._increment_archive_revision(connection)
        return RecoveredPostsUpsertResult(
            inserted_count,
            frozenset(changed_lous),
            frozenset(
                lou for lou in changed_lous if lou not in inputs_before
            ),
        )

    def refresh_stored_word_counts(self) -> int:
        self.require_exists()
        with self._write_connection() as connection:
            with connection:
                rows = cast(
                    list[tuple[int, object]],
                    connection.execute(
                        """
                        SELECT id, content
                        FROM post_versions
                        WHERE word_count_version != ?
                        """,
                        (WORD_COUNT_VERSION,),
                    ).fetchall(),
                )
                for row_id, raw_content in rows:
                    content = decode_content(
                        raw_content,
                        source=f"archive帖子版本{row_id}正文",
                    )
                    word_count = count_post_content(content)
                    connection.execute(
                        """
                        UPDATE post_versions
                        SET
                            word_count_version = ?,
                            word_count_chinese_chars = ?,
                            word_count_chinese_with_punctuation = ?
                        WHERE id = ?
                        """,
                        (
                            WORD_COUNT_VERSION,
                            word_count.chinese_chars,
                            word_count.chinese_with_punctuation,
                            row_id,
                        ),
                    )
        return len(rows)

from __future__ import annotations

import datetime
import sqlite3
from collections import Counter
from collections.abc import Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from nga_tools.backup.processing_state import (
    IMAGE_REFERENCE_MANIFEST_VERSION,
    ArchiveChangeState,
    AudioProcessingState,
    BackupProcessingSnapshot,
    CurrentPaginationState,
    FloorProcessingState,
    ImageReferenceManifestEntry,
    ImageReferenceManifestPost,
    ImageReferenceManifestSnapshot,
    ImageReferenceManifestState,
    ImageReferenceState,
    PendingMediaRetry,
    PendingMissingFloorRetry,
)
from nga_tools.backup.archive_post_store import ArchivePostRepository
from nga_tools.backup.thread_stores import ThreadArchiveStateStore
from nga_tools.core.download_types import DOWNLOAD_FAILURE_KINDS
from nga_tools.core.sqlite import iter_in_clause_chunks


@dataclass(frozen=True)
class _PendingMediaTableSpec:
    table_name: str
    label: str


_PENDING_IMAGE_RETRIES = _PendingMediaTableSpec(
    table_name="backup_pending_images",
    label="图片",
)
_PENDING_AUDIO_RETRIES = _PendingMediaTableSpec(
    table_name="backup_pending_audio",
    label="音频",
)


class ArchiveStateSource(Protocol):
    def exists(self) -> bool: ...
    def require_exists(self) -> None: ...
    def ensure_schema(self) -> None: ...
    def archive_store_id(self) -> str: ...
    def read_current_archive_change_state(self) -> ArchiveChangeState: ...
    def state_write_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]: ...
    def state_read_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]: ...


class ArchiveStateRepository:
    def __init__(
        self,
        source: ArchiveStateSource,
        state_store: ThreadArchiveStateStore,
        posts: ArchivePostRepository,
    ) -> None:
        self._source = source
        self.state_store = state_store
        self._posts = posts

    @property
    def db_path(self) -> Path:
        return self.state_store.db_path

    def exists(self) -> bool:
        return self._source.exists()

    def require_exists(self) -> None:
        self._source.require_exists()

    def archive_store_id(self) -> str:
        return self._source.archive_store_id()

    def _read_current_archive_change_state(self) -> ArchiveChangeState:
        return self._source.read_current_archive_change_state()

    def max_post_version_id(self) -> int:
        return self._posts.max_post_version_id()

    def _state_write_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        return self._source.state_write_connection()

    def _state_read_connection(
        self,
    ) -> AbstractContextManager[sqlite3.Connection]:
        return self._source.state_read_connection()

    def ensure_schema(self) -> None:
        if not self.exists():
            self._source.ensure_schema()
        with self._state_write_connection():
            pass
        if self.read_current_pagination_state() is not None:
            return
        fallback = self._posts.read_latest_pagination_observation()
        if fallback is not None:
            self.initialize_current_pagination_state(fallback)

    @staticmethod
    def _validate_current_pagination_state(
        state: CurrentPaginationState,
    ) -> str:
        if (
            type(state.page_count) is not int
            or state.page_count < 1
            or (
                state.author_total_lou_count is not None
                and (
                    type(state.author_total_lou_count) is not int
                    or state.author_total_lou_count < 0
                )
            )
            or type(state.source_page_number) is not int
            or state.source_page_number < 1
            or state.observed_at.tzinfo is None
            or state.observed_at.utcoffset() is None
        ):
            raise ValueError(f"backup当前分页水位无效：{state!r}")
        return state.observed_at.astimezone(datetime.timezone.utc).isoformat(
            timespec="microseconds"
        )

    @staticmethod
    def _current_pagination_state_from_row(
        row: tuple[object, object, object, object] | None,
    ) -> CurrentPaginationState | None:
        if row is None:
            return None
        page_count, author_total_lou_count, source_page_number, observed_at = row
        if type(page_count) is not int or page_count < 1:
            raise ValueError(f"backup当前分页数无效：{row!r}")
        if author_total_lou_count is not None and (
            type(author_total_lou_count) is not int
            or author_total_lou_count < 0
        ):
            raise ValueError(f"backup当前分页vrows无效：{row!r}")
        if type(source_page_number) is not int or source_page_number < 1:
            raise ValueError(f"backup当前分页来源页无效：{row!r}")
        if not isinstance(observed_at, str) or not observed_at:
            raise ValueError(f"backup当前分页时间无效：{row!r}")
        try:
            parsed_observed_at = datetime.datetime.fromisoformat(observed_at)
        except ValueError as error:
            raise ValueError(f"backup当前分页时间无效：{row!r}") from error
        if (
            parsed_observed_at.tzinfo is None
            or parsed_observed_at.utcoffset() is None
        ):
            raise ValueError(f"backup当前分页时间缺少时区：{row!r}")
        return CurrentPaginationState(
            page_count=page_count,
            author_total_lou_count=author_total_lou_count,
            source_page_number=source_page_number,
            observed_at=parsed_observed_at,
        )

    def read_current_pagination_state(self) -> CurrentPaginationState | None:
        if not self.state_store.exists():
            return None
        with self._state_read_connection() as connection:
            row = cast(
                tuple[object, object, object, object] | None,
                connection.execute(
                    """
                    SELECT page_count, author_total_lou_count,
                           source_page_number, observed_at
                    FROM backup_current_pagination_state
                    WHERE singleton = 1
                    """
                ).fetchone(),
            )
        return self._current_pagination_state_from_row(row)

    def initialize_current_pagination_state(
        self,
        state: CurrentPaginationState,
    ) -> bool:
        observed_at = self._validate_current_pagination_state(state)
        self.require_exists()
        with self._state_write_connection() as connection:
            with connection:
                row = cast(
                    tuple[object, object, object, object] | None,
                    connection.execute(
                    """
                    INSERT INTO backup_current_pagination_state VALUES
                    (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO NOTHING
                    RETURNING page_count, author_total_lou_count,
                              source_page_number, observed_at
                    """,
                    (
                        state.page_count,
                        state.author_total_lou_count,
                        state.source_page_number,
                        observed_at,
                    ),
                    ).fetchone(),
                )
                if row is None:
                    row = cast(
                        tuple[object, object, object, object] | None,
                        connection.execute(
                            """
                            SELECT page_count, author_total_lou_count,
                                   source_page_number, observed_at
                            FROM backup_current_pagination_state
                            WHERE singleton = 1
                            """
                        ).fetchone(),
                    )
                current = self._current_pagination_state_from_row(row)
        return current is not None

    def commit_current_pagination_state(
        self,
        state: CurrentPaginationState,
    ) -> bool:
        observed_at = self._validate_current_pagination_state(state)
        self.require_exists()
        with self._state_write_connection() as connection:
            with connection:
                row = cast(
                    tuple[object, object, object, object] | None,
                    connection.execute(
                    """
                    INSERT INTO backup_current_pagination_state VALUES
                    (1, ?, ?, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        page_count = excluded.page_count,
                        author_total_lou_count = excluded.author_total_lou_count,
                        source_page_number = excluded.source_page_number,
                        observed_at = excluded.observed_at
                    WHERE excluded.observed_at >=
                          backup_current_pagination_state.observed_at
                    RETURNING page_count, author_total_lou_count,
                              source_page_number, observed_at
                    """,
                    (
                        state.page_count,
                        state.author_total_lou_count,
                        state.source_page_number,
                        observed_at,
                    ),
                    ).fetchone(),
                )
                if row is None:
                    row = cast(
                        tuple[object, object, object, object] | None,
                        connection.execute(
                            """
                            SELECT page_count, author_total_lou_count,
                                   source_page_number, observed_at
                            FROM backup_current_pagination_state
                            WHERE singleton = 1
                            """
                        ).fetchone(),
                    )
                current = self._current_pagination_state_from_row(row)
        return current is not None and (
            current.page_count == state.page_count
            and current.author_total_lou_count
            == state.author_total_lou_count
        )

    def read_backup_processing_snapshot(self) -> BackupProcessingSnapshot:
        self.require_exists()
        change_state = self._read_current_archive_change_state()
        if not self.state_store.exists():
            return BackupProcessingSnapshot(
                change_state=change_state,
                pending_image_retries=(),
            )
        return self._read_backup_processing_snapshot_from_state(change_state)

    @staticmethod
    def _read_pending_media_retry_rows(
        connection: sqlite3.Connection,
        spec: _PendingMediaTableSpec,
    ) -> list[tuple[object, object, object, object]]:
        return cast(
            list[tuple[object, object, object, object]],
            connection.execute(
                f"""
                SELECT url, last_attempt_at, failure_kind, http_status
                FROM {spec.table_name}
                ORDER BY url
                """
            ).fetchall(),
        )

    @staticmethod
    def _parse_pending_media_retry_rows(
        rows: Sequence[tuple[object, object, object, object]],
        spec: _PendingMediaTableSpec,
    ) -> tuple[PendingMediaRetry, ...]:
        retries: list[PendingMediaRetry] = []
        for url, last_attempt_at, failure_kind, http_status in rows:
            if not isinstance(url, str) or not url:
                raise ValueError(
                    f"backup待重试{spec.label}URL无效：{url!r}"
                )
            if last_attempt_at is None:
                if failure_kind is not None or http_status is not None:
                    raise ValueError(
                        f"backup待重试{spec.label}旧状态无效："
                        f"{(url, failure_kind, http_status)!r}"
                    )
                parsed_last_attempt_at = None
                parsed_failure_kind = None
            else:
                if not isinstance(last_attempt_at, str):
                    raise ValueError(
                        f"backup待重试{spec.label}时间无效："
                        f"{last_attempt_at!r}"
                    )
                try:
                    parsed_last_attempt_at = datetime.datetime.fromisoformat(
                        last_attempt_at
                    )
                except ValueError as error:
                    raise ValueError(
                        f"backup待重试{spec.label}时间无效："
                        f"{last_attempt_at!r}"
                    ) from error
                if (
                    parsed_last_attempt_at.tzinfo is None
                    or parsed_last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        f"backup待重试{spec.label}时间缺少时区："
                        f"{last_attempt_at!r}"
                    )
                if (
                    not isinstance(failure_kind, str)
                    or failure_kind not in DOWNLOAD_FAILURE_KINDS
                ):
                    raise ValueError(
                        f"backup待重试{spec.label}失败类别无效："
                        f"{failure_kind!r}"
                    )
                parsed_failure_kind = failure_kind
            if http_status is not None and (
                type(http_status) is not int
                or http_status < 100
                or http_status > 599
            ):
                raise ValueError(
                    f"backup待重试{spec.label}HTTP状态无效："
                    f"{http_status!r}"
                )
            retries.append(
                PendingMediaRetry(
                    url=url,
                    last_attempt_at=parsed_last_attempt_at,
                    failure_kind=parsed_failure_kind,
                    http_status=http_status,
                )
            )
        return tuple(retries)

    @staticmethod
    def _parse_floor_processing_state_row(
        row: Sequence[object] | None,
    ) -> FloorProcessingState | None:
        if row is None:
            return None
        if any(
            type(value) is not int
            for value in (*row[:4], *row[5:7])
        ):
            raise ValueError(f"backup楼层处理状态整数列无效：{row!r}")
        if row[4] is not None and type(row[4]) is not int:
            raise ValueError(f"backup楼层处理状态vrows无效：{row!r}")
        if (
            not isinstance(row[7], str)
            or not row[7]
            or not isinstance(row[8], str)
            or not row[8]
        ):
            raise ValueError(f"backup楼层处理状态文本列无效：{row!r}")
        return FloorProcessingState(
            format_version=cast(int, row[0]),
            processed_archive_revision=cast(int, row[1]),
            processed_floor_map_revision=cast(int, row[2]),
            page_count=cast(int, row[3]),
            author_total_lou_count=row[4],
            floor_map_format_version=cast(int, row[5]),
            floor_map_generation_version=cast(int, row[6]),
            floor_map_hash_algorithm=row[7],
            completed_at=row[8],
        )

    @staticmethod
    def _parse_image_reference_state_row(
        row: Sequence[object] | None,
    ) -> ImageReferenceState | None:
        if row is None:
            return None
        if (
            type(row[0]) is not int
            or type(row[1]) is not int
            or type(row[4]) is not int
        ):
            raise ValueError(f"backup图片引用状态整数列无效：{row!r}")
        if any(
            not isinstance(value, str) or not value
            for value in (row[2], row[3], row[5])
        ):
            raise ValueError(f"backup图片引用状态文本列无效：{row!r}")
        return ImageReferenceState(
            format_version=row[0],
            processed_archive_revision=row[1],
            post_overlays_fingerprint=cast(str, row[2]),
            post_version_selections_fingerprint=cast(str, row[3]),
            image_reference_extractor_version=row[4],
            completed_at=cast(str, row[5]),
        )

    @staticmethod
    def _parse_pending_missing_floor_retry_rows(
        rows: Sequence[tuple[object, object]],
    ) -> tuple[PendingMissingFloorRetry, ...]:
        retries: list[PendingMissingFloorRetry] = []
        for author_lou, last_attempt_at in rows:
            if type(author_lou) is not int or author_lou < 0:
                raise ValueError(
                    f"backup待重试缺失楼楼层无效：{author_lou!r}"
                )
            if not isinstance(last_attempt_at, str):
                raise ValueError(
                    f"backup待重试缺失楼时间无效：{last_attempt_at!r}"
                )
            try:
                parsed_last_attempt_at = datetime.datetime.fromisoformat(
                    last_attempt_at
                )
            except ValueError as error:
                raise ValueError(
                    f"backup待重试缺失楼时间无效：{last_attempt_at!r}"
                ) from error
            if (
                parsed_last_attempt_at.tzinfo is None
                or parsed_last_attempt_at.utcoffset() is None
            ):
                raise ValueError(
                    "backup待重试缺失楼时间缺少时区："
                    f"{last_attempt_at!r}"
                )
            retries.append(
                PendingMissingFloorRetry(author_lou, parsed_last_attempt_at)
            )
        return tuple(retries)

    @staticmethod
    def _parse_audio_processing_state_row(
        row: Sequence[object] | None,
    ) -> AudioProcessingState | None:
        if row is None:
            return None
        if any(type(value) is not int for value in row[:3]):
            raise ValueError(f"backup音频处理状态整数列无效：{row!r}")
        if cast(int, row[2]) < 0:
            raise ValueError(f"backup音频处理水位无效：{row!r}")
        if not isinstance(row[3], str) or not row[3]:
            raise ValueError(f"backup音频处理状态时间无效：{row!r}")
        return AudioProcessingState(
            format_version=cast(int, row[0]),
            extractor_version=cast(int, row[1]),
            processed_max_post_version_id=cast(int, row[2]),
            completed_at=row[3],
        )

    def _read_backup_processing_snapshot_from_state(
        self,
        change_state: ArchiveChangeState,
    ) -> BackupProcessingSnapshot:
        with self._state_read_connection() as connection:
            pending_rows = self._read_pending_media_retry_rows(
                connection,
                _PENDING_IMAGE_RETRIES,
            )
            pending_audio_rows = self._read_pending_media_retry_rows(
                connection,
                _PENDING_AUDIO_RETRIES,
            )
            pending_missing_floor_rows = cast(
                list[tuple[object, object]],
                connection.execute(
                    """
                    SELECT author_lou, last_attempt_at
                    FROM backup_pending_missing_floors
                    ORDER BY author_lou
                    """
                ).fetchall(),
            )
            pagination_row = cast(
                tuple[object, object, object, object] | None,
                connection.execute(
                    """
                    SELECT page_count, author_total_lou_count,
                           source_page_number, observed_at
                    FROM backup_current_pagination_state
                    WHERE singleton = 1
                    """
                ).fetchone(),
            )
            floor_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision,
                       processed_floor_map_revision, page_count,
                       author_total_lou_count, floor_map_format_version,
                       floor_map_generation_version, floor_map_hash_algorithm,
                       completed_at
                FROM backup_floor_processing_state WHERE singleton = 1
                """
            ).fetchone()
            image_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision,
                       post_overlays_fingerprint,
                       post_version_selections_fingerprint,
                       image_reference_extractor_version, completed_at
                FROM backup_image_reference_state WHERE singleton = 1
                """
            ).fetchone()
            audio_row = connection.execute(
                """
                SELECT format_version, extractor_version,
                       processed_max_post_version_id, completed_at
                FROM backup_audio_processing_state WHERE singleton = 1
                """
            ).fetchone()
        pending_image_retries = self._parse_pending_media_retry_rows(
            pending_rows,
            _PENDING_IMAGE_RETRIES,
        )
        floor_state = self._parse_floor_processing_state_row(floor_row)
        image_state = self._parse_image_reference_state_row(image_row)
        pending_missing_floor_retries = (
            self._parse_pending_missing_floor_retry_rows(
                pending_missing_floor_rows
            )
        )
        pending_audio_retries = self._parse_pending_media_retry_rows(
            pending_audio_rows,
            _PENDING_AUDIO_RETRIES,
        )
        audio_state = self._parse_audio_processing_state_row(audio_row)
        return BackupProcessingSnapshot(
            change_state=change_state,
            pending_image_retries=pending_image_retries,
            current_pagination_state=(
                self._current_pagination_state_from_row(pagination_row)
            ),
            floor_state=floor_state,
            image_state=image_state,
            audio_state=audio_state,
            pending_audio_retries=pending_audio_retries,
            pending_missing_floor_retries=pending_missing_floor_retries,
        )

    @staticmethod
    def _replace_pending_media_retries(
        connection: sqlite3.Connection,
        retries: tuple[PendingMediaRetry, ...],
        spec: _PendingMediaTableSpec,
    ) -> None:
        rows: list[tuple[str, str | None, str | None, int | None]] = []
        seen_urls: set[str] = set()
        for retry in sorted(retries, key=lambda item: item.url):
            if not retry.url:
                raise ValueError(f"backup待重试{spec.label}URL不能为空。")
            if retry.url in seen_urls:
                raise ValueError(
                    f"backup待重试{spec.label}URL重复：{retry.url}"
                )
            seen_urls.add(retry.url)
            if retry.last_attempt_at is None:
                if retry.failure_kind is not None or retry.http_status is not None:
                    raise ValueError(
                        f"backup待重试{spec.label}旧状态无效：{retry.url}"
                    )
                last_attempt_text = None
            else:
                if (
                    retry.last_attempt_at.tzinfo is None
                    or retry.last_attempt_at.utcoffset() is None
                ):
                    raise ValueError(
                        f"backup待重试{spec.label}时间缺少时区："
                        f"{retry.url}"
                    )
                if retry.failure_kind not in DOWNLOAD_FAILURE_KINDS:
                    raise ValueError(
                        f"backup待重试{spec.label}失败类别无效："
                        f"{retry.failure_kind!r}"
                    )
                last_attempt_text = retry.last_attempt_at.astimezone(
                    datetime.timezone.utc
                ).isoformat(timespec="microseconds")
            if retry.http_status is not None and (
                type(retry.http_status) is not int
                or retry.http_status < 100
                or retry.http_status > 599
            ):
                raise ValueError(
                    f"backup待重试{spec.label}HTTP状态无效："
                    f"{retry.http_status!r}"
                )
            rows.append(
                (
                    retry.url,
                    last_attempt_text,
                    retry.failure_kind,
                    retry.http_status,
                )
            )
        connection.execute(f"DELETE FROM {spec.table_name}")
        connection.executemany(
            f"""
            INSERT INTO {spec.table_name} (
                url,
                last_attempt_at,
                failure_kind,
                http_status
            )
            VALUES (?, ?, ?, ?)
            """,
            rows,
        )

    @classmethod
    def _replace_pending_images(
        cls,
        connection: sqlite3.Connection,
        pending_image_retries: tuple[PendingMediaRetry, ...],
    ) -> None:
        cls._replace_pending_media_retries(
            connection,
            pending_image_retries,
            _PENDING_IMAGE_RETRIES,
        )

    @staticmethod
    def _replace_pending_missing_floors(
        connection: sqlite3.Connection,
        retries: tuple[PendingMissingFloorRetry, ...],
    ) -> None:
        rows: list[tuple[int, str]] = []
        seen_lous: set[int] = set()
        for retry in sorted(retries, key=lambda item: item.author_lou):
            if type(retry.author_lou) is not int or retry.author_lou < 0:
                raise ValueError(
                    f"backup待重试缺失楼楼层无效：{retry.author_lou!r}"
                )
            if retry.author_lou in seen_lous:
                raise ValueError(
                    f"backup待重试缺失楼重复：{retry.author_lou}"
                )
            seen_lous.add(retry.author_lou)
            if (
                retry.last_attempt_at.tzinfo is None
                or retry.last_attempt_at.utcoffset() is None
            ):
                raise ValueError(
                    f"backup待重试缺失楼时间缺少时区：{retry.author_lou}"
                )
            rows.append(
                (
                    retry.author_lou,
                    retry.last_attempt_at.astimezone(
                        datetime.timezone.utc
                    ).isoformat(timespec="microseconds"),
                )
            )
        connection.execute("DELETE FROM backup_pending_missing_floors")
        connection.executemany(
            """
            INSERT INTO backup_pending_missing_floors (
                author_lou,
                last_attempt_at
            ) VALUES (?, ?)
            """,
            rows,
        )

    @classmethod
    def _replace_pending_audio(
        cls,
        connection: sqlite3.Connection,
        pending_audio_retries: tuple[PendingMediaRetry, ...],
    ) -> None:
        cls._replace_pending_media_retries(
            connection,
            pending_audio_retries,
            _PENDING_AUDIO_RETRIES,
        )

    @staticmethod
    def _clear_image_reference_manifest(
        connection: sqlite3.Connection,
    ) -> None:
        connection.execute("DELETE FROM backup_image_reference_manifest_entries")
        connection.execute("DELETE FROM backup_image_reference_manifest_posts")
        connection.execute("DELETE FROM backup_image_reference_manifest_urls")
        connection.execute("DELETE FROM backup_image_reference_manifest_state")

    @staticmethod
    def _validated_image_reference_manifest_posts(
        posts: tuple[ImageReferenceManifestPost, ...],
    ) -> tuple[
        tuple[ImageReferenceManifestPost, ...],
        Counter[str],
        dict[str, bool],
    ]:
        posts_by_lou: dict[int, ImageReferenceManifestPost] = {}
        reference_counts: Counter[str] = Counter()
        validity_by_url: dict[str, bool] = {}
        for post in posts:
            if type(post.lou) is not int or post.lou < 0:
                raise ValueError(f"图片引用清单楼层无效：{post.lou!r}")
            if not post.cache_key:
                raise ValueError(f"图片引用清单第{post.lou}楼缓存键为空。")
            if post.lou in posts_by_lou:
                raise ValueError(f"图片引用清单楼层重复：{post.lou}")
            previous_image_index = 0
            for reference in post.references:
                if (
                    type(reference.image_index) is not int
                    or reference.image_index <= previous_image_index
                ):
                    raise ValueError(
                        f"图片引用清单第{post.lou}楼序号无效："
                        f"{reference.image_index!r}"
                    )
                if not reference.url or type(reference.valid) is not bool:
                    raise ValueError(
                        f"图片引用清单第{post.lou}楼引用无效："
                        f"{reference!r}"
                    )
                previous_validity = validity_by_url.setdefault(
                    reference.url,
                    reference.valid,
                )
                if previous_validity != reference.valid:
                    raise ValueError(
                        f"图片引用清单URL合法性冲突：{reference.url}"
                    )
                reference_counts[reference.url] += 1
                previous_image_index = reference.image_index
            posts_by_lou[post.lou] = post
        return (
            tuple(posts_by_lou[lou] for lou in sorted(posts_by_lou)),
            reference_counts,
            validity_by_url,
        )
    @classmethod
    def _replace_image_reference_manifest(
        cls,
        connection: sqlite3.Connection,
        state: ImageReferenceManifestState,
        posts: tuple[ImageReferenceManifestPost, ...],
    ) -> None:
        if (
            type(state.format_version) is not int
            or type(state.processed_archive_revision) is not int
            or state.processed_archive_revision < 0
        ):
            raise ValueError(f"图片引用清单状态无效：{state!r}")
        ordered_posts, reference_counts, validity_by_url = (
            cls._validated_image_reference_manifest_posts(posts)
        )
        cls._clear_image_reference_manifest(connection)
        connection.execute(
            """
            INSERT INTO backup_image_reference_manifest_state
            (singleton, format_version, processed_archive_revision)
            VALUES (1, ?, ?)
            """,
            (state.format_version, state.processed_archive_revision),
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_posts (lou, cache_key)
            VALUES (?, ?)
            """,
            [(post.lou, post.cache_key) for post in ordered_posts],
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_entries
            (lou, image_index, url, valid)
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    post.lou,
                    reference.image_index,
                    reference.url,
                    int(reference.valid),
                )
                for post in ordered_posts
                for reference in post.references
            ],
        )
        connection.executemany(
            """
            INSERT INTO backup_image_reference_manifest_urls
            (url, reference_count, valid)
            VALUES (?, ?, ?)
            """,
            [
                (url, reference_counts[url], int(validity_by_url[url]))
                for url in sorted(reference_counts)
            ],
        )
    def read_image_reference_manifest(
        self,
    ) -> ImageReferenceManifestSnapshot | None:
        self.require_exists()
        if not self.state_store.exists():
            return None
        with self._state_read_connection() as connection:
            state_row = connection.execute(
                """
                SELECT format_version, processed_archive_revision
                FROM backup_image_reference_manifest_state
                WHERE singleton = 1
                """
            ).fetchone()
            post_rows = cast(
                list[tuple[object, object]],
                connection.execute(
                    """
                    SELECT lou, cache_key
                    FROM backup_image_reference_manifest_posts
                    ORDER BY lou
                    """
                ).fetchall(),
            )
            entry_rows = cast(
                list[tuple[object, object, object, object]],
                connection.execute(
                    """
                    SELECT lou, image_index, url, valid
                    FROM backup_image_reference_manifest_entries
                    ORDER BY lou, image_index
                    """
                ).fetchall(),
            )
            url_rows = cast(
                list[tuple[object, object, object]],
                connection.execute(
                    """
                    SELECT url, reference_count, valid
                    FROM backup_image_reference_manifest_urls
                    ORDER BY url
                    """
                ).fetchall(),
            )

        if state_row is None:
            if post_rows or entry_rows or url_rows:
                raise ValueError("图片引用清单缺少状态行。")
            return None
        if (
            len(state_row) != 2
            or type(state_row[0]) is not int
            or type(state_row[1]) is not int
            or state_row[1] < 0
        ):
            raise ValueError(f"图片引用清单状态行无效：{state_row!r}")
        state = ImageReferenceManifestState(state_row[0], state_row[1])

        references_by_lou: dict[int, list[ImageReferenceManifestEntry]] = {}
        cache_key_by_lou: dict[int, str] = {}
        for lou, cache_key in post_rows:
            if type(lou) is not int or lou < 0 or not isinstance(cache_key, str) or not cache_key:
                raise ValueError(f"图片引用清单帖子行无效：{(lou, cache_key)!r}")
            if lou in cache_key_by_lou:
                raise ValueError(f"图片引用清单楼层重复：{lou}")
            cache_key_by_lou[lou] = cache_key
            references_by_lou[lou] = []
        for lou, image_index, url, valid in entry_rows:
            if (
                type(lou) is not int
                or lou not in references_by_lou
                or type(image_index) is not int
                or not isinstance(url, str)
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单引用行无效："
                    f"{(lou, image_index, url, valid)!r}"
                )
            references_by_lou[lou].append(
                ImageReferenceManifestEntry(image_index, url, bool(valid))
            )
        posts = tuple(
            ImageReferenceManifestPost(
                lou=lou,
                cache_key=cache_key_by_lou[lou],
                references=tuple(references_by_lou[lou]),
            )
            for lou in sorted(cache_key_by_lou)
        )
        ordered_posts, reference_counts, validity_by_url = (
            self._validated_image_reference_manifest_posts(posts)
        )

        stored_url_counts: list[tuple[str, int, bool]] = []
        for url, reference_count, valid in url_rows:
            if (
                not isinstance(url, str)
                or not url
                or type(reference_count) is not int
                or reference_count <= 0
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单URL行无效："
                    f"{(url, reference_count, valid)!r}"
                )
            stored_url_counts.append((url, reference_count, bool(valid)))
        expected_url_counts = [
            (url, reference_counts[url], validity_by_url[url])
            for url in sorted(reference_counts)
        ]
        if stored_url_counts != expected_url_counts:
            raise ValueError("图片引用清单URL引用计数不一致。")
        return ImageReferenceManifestSnapshot(
            state=state,
            posts=ordered_posts,
            url_reference_counts=tuple(stored_url_counts),
        )
    def read_image_reference_manifest_state(
        self,
    ) -> ImageReferenceManifestState | None:
        self.require_exists()
        if not self.state_store.exists():
            return None
        with self._state_read_connection() as connection:
            row = connection.execute(
                """
                SELECT format_version, processed_archive_revision
                FROM backup_image_reference_manifest_state
                WHERE singleton = 1
                """
            ).fetchone()
        if row is None:
            return None
        if (
            len(row) != 2
            or type(row[0]) is not int
            or type(row[1]) is not int
            or row[1] < 0
        ):
            raise ValueError(f"图片引用清单状态行无效：{row!r}")
        return ImageReferenceManifestState(row[0], row[1])
    def read_image_reference_manifest_posts(
        self,
        lous: set[int],
    ) -> dict[int, ImageReferenceManifestPost]:
        self.require_exists()
        if not lous or not self.state_store.exists():
            return {}
        post_rows: list[tuple[object, object]] = []
        entry_rows: list[tuple[object, object, object, object]] = []
        with self._state_read_connection() as connection:
            for chunk in iter_in_clause_chunks(sorted(lous)):
                placeholders = ",".join("?" for _value in chunk)
                post_rows.extend(
                    cast(
                        list[tuple[object, object]],
                        connection.execute(
                            """
                            SELECT lou, cache_key
                            FROM backup_image_reference_manifest_posts
                            WHERE lou IN ("""
                            + placeholders
                            + ") ORDER BY lou",
                            chunk,
                        ).fetchall(),
                    )
                )
                entry_rows.extend(
                    cast(
                        list[tuple[object, object, object, object]],
                        connection.execute(
                            """
                            SELECT lou, image_index, url, valid
                            FROM backup_image_reference_manifest_entries
                            WHERE lou IN ("""
                            + placeholders
                            + ") ORDER BY lou, image_index",
                            chunk,
                        ).fetchall(),
                    )
                )

        cache_key_by_lou: dict[int, str] = {}
        references_by_lou: dict[int, list[ImageReferenceManifestEntry]] = {}
        for lou, cache_key in post_rows:
            if (
                type(lou) is not int
                or lou not in lous
                or not isinstance(cache_key, str)
                or not cache_key
                or lou in cache_key_by_lou
            ):
                raise ValueError(
                    f"图片引用清单帖子行无效：{(lou, cache_key)!r}"
                )
            cache_key_by_lou[lou] = cache_key
            references_by_lou[lou] = []
        for lou, image_index, url, valid in entry_rows:
            if (
                type(lou) is not int
                or lou not in references_by_lou
                or type(image_index) is not int
                or not isinstance(url, str)
                or type(valid) is not int
                or valid not in (0, 1)
            ):
                raise ValueError(
                    f"图片引用清单引用行无效："
                    f"{(lou, image_index, url, valid)!r}"
                )
            references_by_lou[lou].append(
                ImageReferenceManifestEntry(image_index, url, bool(valid))
            )
        posts = tuple(
            ImageReferenceManifestPost(
                lou=lou,
                cache_key=cache_key_by_lou[lou],
                references=tuple(references_by_lou[lou]),
            )
            for lou in sorted(cache_key_by_lou)
        )
        ordered_posts, _counts, _validity = (
            self._validated_image_reference_manifest_posts(posts)
        )
        return {post.lou: post for post in ordered_posts}
    def read_image_reference_manifest_url_counts(
        self,
        urls: set[str],
    ) -> dict[str, tuple[int, bool]]:
        self.require_exists()
        if not urls or not self.state_store.exists():
            return {}
        rows: list[tuple[object, object, object]] = []
        with self._state_read_connection() as connection:
            for chunk in iter_in_clause_chunks(sorted(urls)):
                placeholders = ",".join("?" for _value in chunk)
                rows.extend(
                    cast(
                        list[tuple[object, object, object]],
                        connection.execute(
                            """
                            SELECT url, reference_count, valid
                            FROM backup_image_reference_manifest_urls
                            WHERE url IN ("""
                            + placeholders
                            + ") ORDER BY url",
                            chunk,
                        ).fetchall(),
                    )
                )
        result: dict[str, tuple[int, bool]] = {}
        for url, reference_count, valid in rows:
            if (
                not isinstance(url, str)
                or url not in urls
                or type(reference_count) is not int
                or reference_count <= 0
                or type(valid) is not int
                or valid not in (0, 1)
                or url in result
            ):
                raise ValueError(
                    f"图片引用清单URL行无效："
                    f"{(url, reference_count, valid)!r}"
                )
            result[url] = (reference_count, bool(valid))
        return result
    def clear_backup_processing_state(self) -> None:
        if not self.exists():
            return
        with self._state_write_connection() as connection:
            with connection:
                connection.execute("DELETE FROM backup_pending_images")
                connection.execute("DELETE FROM backup_pending_missing_floors")
                connection.execute("DELETE FROM backup_floor_processing_state")
                connection.execute("DELETE FROM backup_image_reference_state")
                connection.execute("DELETE FROM backup_pending_audio")
                connection.execute("DELETE FROM backup_audio_processing_state")
                self._clear_image_reference_manifest(connection)
    def replace_pending_image_retries(
        self,
        pending_image_retries: tuple[PendingMediaRetry, ...],
    ) -> None:
        """Replace rebuildable retry state without marking image processing current."""
        self.require_exists()
        with self._state_write_connection() as connection:
            with connection:
                self._replace_pending_images(connection, pending_image_retries)

    def replace_pending_missing_floor_retries(
        self,
        retries: tuple[PendingMissingFloorRetry, ...],
    ) -> None:
        self.require_exists()
        with self._state_write_connection() as connection:
            with connection:
                self._replace_pending_missing_floors(connection, retries)

    def apply_missing_floor_retry_attempt(
        self,
        *,
        expected_retries: Sequence[PendingMissingFloorRetry],
        recovered_lous: Sequence[int],
        attempted_unresolved_lous: Sequence[int],
        attempted_at: datetime.datetime,
    ) -> bool:
        if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
            raise ValueError("缺失楼增量重试时间必须包含时区。")
        recovered = set(recovered_lous)
        attempted_unresolved = set(attempted_unresolved_lous)
        if recovered & attempted_unresolved:
            raise ValueError("已恢复与仍未恢复的缺失楼不能重叠。")
        target_lous = sorted(recovered | attempted_unresolved)
        if any(
            type(author_lou) is not int or author_lou < 0
            for author_lou in target_lous
        ):
            raise ValueError("缺失楼增量待办author_lou必须是非负整数。")
        if not target_lous:
            return True

        expected_by_lou: dict[int, str] = {}
        for retry in expected_retries:
            if retry.author_lou in expected_by_lou:
                raise ValueError(f"backup待重试缺失楼重复：{retry.author_lou}")
            if (
                retry.last_attempt_at.tzinfo is None
                or retry.last_attempt_at.utcoffset() is None
            ):
                raise ValueError(
                    f"backup待重试缺失楼时间缺少时区：{retry.author_lou}"
                )
            expected_by_lou[retry.author_lou] = retry.last_attempt_at.astimezone(
                datetime.timezone.utc
            ).isoformat(timespec="microseconds")
        if any(author_lou not in expected_by_lou for author_lou in target_lous):
            return False

        self.require_exists()
        with self._state_write_connection() as connection:
            with connection:
                current_by_lou: dict[int, str] = {}
                for chunk in iter_in_clause_chunks(target_lous):
                    placeholders = ", ".join("?" for _value in chunk)
                    rows = connection.execute(
                        f"""
                        SELECT author_lou, last_attempt_at
                        FROM backup_pending_missing_floors
                        WHERE author_lou IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                    for author_lou, last_attempt_at in rows:
                        if (
                            type(author_lou) is not int
                            or not isinstance(last_attempt_at, str)
                            or author_lou in current_by_lou
                        ):
                            raise ValueError(
                                "backup待重试缺失楼增量状态无效："
                                f"{(author_lou, last_attempt_at)!r}"
                            )
                        current_by_lou[author_lou] = last_attempt_at
                expected_targets = {
                    author_lou: expected_by_lou[author_lou]
                    for author_lou in target_lous
                }
                if current_by_lou != expected_targets:
                    return False

                for chunk in iter_in_clause_chunks(sorted(recovered)):
                    if not chunk:
                        continue
                    placeholders = ", ".join("?" for _value in chunk)
                    connection.execute(
                        f"""
                        DELETE FROM backup_pending_missing_floors
                        WHERE author_lou IN ({placeholders})
                        """,
                        chunk,
                    )
                attempted_at_text = attempted_at.astimezone(
                    datetime.timezone.utc
                ).isoformat(timespec="microseconds")
                connection.executemany(
                    """
                    UPDATE backup_pending_missing_floors
                    SET last_attempt_at = ?
                    WHERE author_lou = ?
                    """,
                    [
                        (attempted_at_text, author_lou)
                        for author_lou in sorted(attempted_unresolved)
                    ],
                )
        return True

    def commit_audio_processing_state(
        self,
        state: AudioProcessingState,
        pending_audio_retries: tuple[PendingMediaRetry, ...],
    ) -> bool:
        self.require_exists()
        if (
            type(state.format_version) is not int
            or state.format_version <= 0
            or type(state.extractor_version) is not int
            or state.extractor_version <= 0
            or type(state.processed_max_post_version_id) is not int
            or state.processed_max_post_version_id < 0
            or not state.completed_at
        ):
            raise ValueError(f"backup音频处理状态无效：{state!r}")
        expected_max_id = state.processed_max_post_version_id
        if self.max_post_version_id() != expected_max_id:
            return False
        with self._state_write_connection() as connection:
            with connection:
                connection.execute(
                    "DELETE FROM backup_audio_processing_state"
                )
                connection.execute(
                    """
                    INSERT INTO backup_audio_processing_state (
                        singleton,
                        format_version,
                        extractor_version,
                        processed_max_post_version_id,
                        completed_at
                    ) VALUES (1, ?, ?, ?, ?)
                    """,
                    (
                        state.format_version,
                        state.extractor_version,
                        state.processed_max_post_version_id,
                        state.completed_at,
                    ),
                )
                self._replace_pending_audio(
                    connection,
                    pending_audio_retries,
                )
        return self.max_post_version_id() == expected_max_id
    def commit_floor_processing_state(self, state: FloorProcessingState) -> bool:
        self.require_exists()
        expected_change_state = ArchiveChangeState(
            state.processed_archive_revision,
            state.processed_floor_map_revision,
        )
        if self._read_current_archive_change_state() != expected_change_state:
            return False
        with self._state_write_connection() as connection:
            with connection:
                pagination_row = cast(
                    tuple[object, object, object, object] | None,
                    connection.execute(
                        """
                        SELECT page_count, author_total_lou_count,
                               source_page_number, observed_at
                        FROM backup_current_pagination_state
                        WHERE singleton = 1
                        """
                    ).fetchone(),
                )
                pagination_state = self._current_pagination_state_from_row(
                    pagination_row
                )
                if pagination_state is None or (
                    pagination_state.page_count != state.page_count
                    or pagination_state.author_total_lou_count
                    != state.author_total_lou_count
                ):
                    return False
                connection.execute("DELETE FROM backup_floor_processing_state")
                connection.execute(
                    """
                    INSERT INTO backup_floor_processing_state VALUES
                    (1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        state.format_version,
                        state.processed_archive_revision,
                        state.processed_floor_map_revision,
                        state.page_count,
                        state.author_total_lou_count,
                        state.floor_map_format_version,
                        state.floor_map_generation_version,
                        state.floor_map_hash_algorithm,
                        state.completed_at,
                    ),
                )
        return self._read_current_archive_change_state() == expected_change_state
    def commit_image_reference_state(
        self,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingMediaRetry, ...],
        *,
        manifest_posts: tuple[ImageReferenceManifestPost, ...] | None = None,
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with self._state_write_connection() as connection:
            with connection:
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
                if manifest_posts is None:
                    self._clear_image_reference_manifest(connection)
                else:
                    self._replace_image_reference_manifest(
                        connection,
                        ImageReferenceManifestState(
                            format_version=IMAGE_REFERENCE_MANIFEST_VERSION,
                            processed_archive_revision=(
                                state.processed_archive_revision
                            ),
                        ),
                        manifest_posts,
                    )
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )
    @staticmethod
    def _image_reference_state_values(
        state: ImageReferenceState,
    ) -> tuple[int, int, str, str, int, str]:
        return (
            state.format_version,
            state.processed_archive_revision,
            state.post_overlays_fingerprint,
            state.post_version_selections_fingerprint,
            state.image_reference_extractor_version,
            state.completed_at,
        )
    @classmethod
    def _stored_image_reference_state_matches(
        cls,
        connection: sqlite3.Connection,
        expected_state: ImageReferenceState,
    ) -> bool:
        row = connection.execute(
            """
            SELECT format_version, processed_archive_revision,
                   post_overlays_fingerprint,
                   post_version_selections_fingerprint,
                   image_reference_extractor_version, completed_at
            FROM backup_image_reference_state
            WHERE singleton = 1
            """
        ).fetchone()
        return row == cls._image_reference_state_values(expected_state)
    @classmethod
    def _replace_image_reference_state(
        cls,
        connection: sqlite3.Connection,
        state: ImageReferenceState,
    ) -> None:
        connection.execute("DELETE FROM backup_image_reference_state")
        connection.execute(
            """
            INSERT INTO backup_image_reference_state
            VALUES (1, ?, ?, ?, ?, ?, ?)
            """,
            cls._image_reference_state_values(state),
        )
    def commit_bootstrapped_image_reference_state(
        self,
        expected_state: ImageReferenceState,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingMediaRetry, ...],
        manifest_posts: tuple[ImageReferenceManifestPost, ...],
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with self._state_write_connection() as connection:
            with connection:
                if not self._stored_image_reference_state_matches(
                    connection,
                    expected_state,
                ):
                    return False
                manifest_state = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision
                    FROM backup_image_reference_manifest_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if manifest_state is not None:
                    return False
                manifest_data_exists = any(
                    connection.execute(
                        f"SELECT EXISTS(SELECT 1 FROM {table_name} LIMIT 1)"
                    ).fetchone()
                    != (0,)
                    for table_name in (
                        "backup_image_reference_manifest_posts",
                        "backup_image_reference_manifest_entries",
                        "backup_image_reference_manifest_urls",
                    )
                )
                if manifest_data_exists:
                    raise ValueError("图片引用清单缺少状态行。")
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
                self._replace_image_reference_manifest(
                    connection,
                    ImageReferenceManifestState(
                        format_version=IMAGE_REFERENCE_MANIFEST_VERSION,
                        processed_archive_revision=state.processed_archive_revision,
                    ),
                    manifest_posts,
                )
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )
    def commit_incremental_image_reference_state(
        self,
        expected_state: ImageReferenceState,
        state: ImageReferenceState,
        pending_image_retries: tuple[PendingMediaRetry, ...],
        changed_posts: tuple[ImageReferenceManifestPost, ...],
    ) -> bool:
        if not changed_posts:
            raise ValueError("增量图片引用清单不能为空。")
        ordered_posts, new_reference_counts, new_validity_by_url = (
            self._validated_image_reference_manifest_posts(changed_posts)
        )
        changed_lous = [post.lou for post in ordered_posts]

        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != state.processed_archive_revision
        ):
            return False
        with self._state_write_connection() as connection:
            with connection:
                if not self._stored_image_reference_state_matches(
                    connection,
                    expected_state,
                ):
                    return False
                manifest_state = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision
                    FROM backup_image_reference_manifest_state
                    WHERE singleton = 1
                    """
                ).fetchone()
                if manifest_state != (
                    IMAGE_REFERENCE_MANIFEST_VERSION,
                    expected_state.processed_archive_revision,
                ):
                    return False

                old_reference_counts: Counter[str] = Counter()
                old_validity_by_url: dict[str, bool] = {}
                for chunk in iter_in_clause_chunks(changed_lous):
                    placeholders = ",".join("?" for _value in chunk)
                    old_rows = cast(
                        list[tuple[object, object]],
                        connection.execute(
                            """
                            SELECT url, valid
                            FROM backup_image_reference_manifest_entries
                            WHERE lou IN ("""
                            + placeholders
                            + ")",
                            chunk,
                        ).fetchall(),
                    )
                    for url, valid in old_rows:
                        if (
                            not isinstance(url, str)
                            or type(valid) is not int
                            or valid not in (0, 1)
                        ):
                            raise ValueError(
                                f"图片引用清单引用行无效："
                                f"{(url, valid)!r}"
                            )
                        old_reference_counts[url] += 1
                        previous_validity = old_validity_by_url.setdefault(
                            url,
                            bool(valid),
                        )
                        if previous_validity != bool(valid):
                            raise ValueError(
                                f"图片引用清单URL合法性冲突：{url}"
                            )

                for url, removed_count in old_reference_counts.items():
                    stored_row = connection.execute(
                        """
                        SELECT reference_count, valid
                        FROM backup_image_reference_manifest_urls
                        WHERE url = ?
                        """,
                        (url,),
                    ).fetchone()
                    if (
                        stored_row is None
                        or type(stored_row[0]) is not int
                        or stored_row[0] < removed_count
                        or type(stored_row[1]) is not int
                        or stored_row[1] not in (0, 1)
                        or bool(stored_row[1]) != old_validity_by_url[url]
                    ):
                        raise ValueError(
                            f"图片引用清单URL计数无效：{url}"
                        )
                    remaining_count = stored_row[0] - removed_count
                    if remaining_count == 0:
                        connection.execute(
                            "DELETE FROM backup_image_reference_manifest_urls WHERE url = ?",
                            (url,),
                        )
                    else:
                        connection.execute(
                            """
                            UPDATE backup_image_reference_manifest_urls
                            SET reference_count = ?
                            WHERE url = ?
                            """,
                            (remaining_count, url),
                        )

                connection.executemany(
                    "DELETE FROM backup_image_reference_manifest_entries WHERE lou = ?",
                    [(lou,) for lou in changed_lous],
                )
                connection.executemany(
                    "DELETE FROM backup_image_reference_manifest_posts WHERE lou = ?",
                    [(lou,) for lou in changed_lous],
                )
                connection.executemany(
                    """
                    INSERT INTO backup_image_reference_manifest_posts
                    (lou, cache_key) VALUES (?, ?)
                    """,
                    [(post.lou, post.cache_key) for post in ordered_posts],
                )
                connection.executemany(
                    """
                    INSERT INTO backup_image_reference_manifest_entries
                    (lou, image_index, url, valid) VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            post.lou,
                            reference.image_index,
                            reference.url,
                            int(reference.valid),
                        )
                        for post in ordered_posts
                        for reference in post.references
                    ],
                )
                for url, added_count in new_reference_counts.items():
                    stored_row = connection.execute(
                        """
                        SELECT reference_count, valid
                        FROM backup_image_reference_manifest_urls
                        WHERE url = ?
                        """,
                        (url,),
                    ).fetchone()
                    if stored_row is None:
                        connection.execute(
                            """
                            INSERT INTO backup_image_reference_manifest_urls
                            (url, reference_count, valid) VALUES (?, ?, ?)
                            """,
                            (url, added_count, int(new_validity_by_url[url])),
                        )
                    else:
                        if (
                            type(stored_row[0]) is not int
                            or stored_row[0] <= 0
                            or type(stored_row[1]) is not int
                            or stored_row[1] not in (0, 1)
                            or bool(stored_row[1]) != new_validity_by_url[url]
                        ):
                            raise ValueError(
                                f"图片引用清单URL计数无效：{url}"
                            )
                        connection.execute(
                            """
                            UPDATE backup_image_reference_manifest_urls
                            SET reference_count = reference_count + ?
                            WHERE url = ?
                            """,
                            (added_count, url),
                        )

                connection.execute(
                    """
                    UPDATE backup_image_reference_manifest_state
                    SET processed_archive_revision = ?
                    WHERE singleton = 1
                    """,
                    (state.processed_archive_revision,),
                )
                self._replace_image_reference_state(connection, state)
                self._replace_pending_images(connection, pending_image_retries)
        return (
            self._read_current_archive_change_state().archive_revision
            == state.processed_archive_revision
        )
    def replace_pending_images_for_image_state(
        self,
        expected_state: ImageReferenceState,
        pending_image_retries: tuple[PendingMediaRetry, ...],
    ) -> bool:
        self.require_exists()
        if (
            self._read_current_archive_change_state().archive_revision
            != expected_state.processed_archive_revision
        ):
            return False
        with self._state_write_connection() as connection:
            with connection:
                row = connection.execute(
                    """
                    SELECT format_version, processed_archive_revision, completed_at
                    FROM backup_image_reference_state WHERE singleton = 1
                    """
                ).fetchone()
                if row != (
                    expected_state.format_version,
                    expected_state.processed_archive_revision,
                    expected_state.completed_at,
                ):
                    return False
                self._replace_pending_images(connection, pending_image_retries)
        return (
            self._read_current_archive_change_state().archive_revision
            == expected_state.processed_archive_revision
        )

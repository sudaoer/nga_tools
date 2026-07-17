from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nga_tools.backup.archive_store import (
    _LATEST_AUTHOR_POST_REFS_QUERY,
    ThreadArchiveStore,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    FloorMapEntry,
    RecoveredMissingPost,
    StoredFloorMap,
)
from nga_tools.backup.processing_state import (
    IMAGE_REFERENCE_MANIFEST_VERSION,
    FloorProcessingState,
    ImageReferenceManifestEntry,
    ImageReferenceManifestPost,
    ImageReferenceManifestSnapshot,
    ImageReferenceManifestState,
    ImageReferenceState,
    PendingImageRetry,
)
from nga_tools.core.downloads import DownloadFailureKind
from nga_tools.core.hashing import hash_text
from nga_tools.storage import UnsupportedStorageFormatError
from nga_tools.word_count import WORD_COUNT_VERSION


_PENDING_RETRY_AT = datetime(2026, 7, 11, tzinfo=timezone.utc)


def _pending_retry(
    url: str,
    *,
    failure_kind: DownloadFailureKind = "http_4xx",
    http_status: int | None = 404,
) -> PendingImageRetry:
    return PendingImageRetry(
        url=url,
        last_attempt_at=_PENDING_RETRY_AT,
        failure_kind=failure_kind,
        http_status=http_status,
    )


def _stored_floor_map(
    entries: list[FloorMapEntry],
    *,
    input_signature: str = "fixture-signature",
) -> StoredFloorMap:
    return StoredFloorMap(
        version=FLOOR_MAP_VERSION,
        generation_version=FLOOR_MAP_GENERATION_VERSION,
        algorithm=FLOOR_MAP_HASH_ALGORITHM,
        tid=123,
        aid=456,
        input_signature=input_signature,
        entries=entries,
    )


class ThreadArchiveStoreTest:
    def test_floor_map_round_trip_preserves_zero_null_and_candidates(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            expected = _stored_floor_map(
                [
                    {"pid": 0, "author_lou": 0, "original_lou": 0},
                    {
                        "pid": None,
                        "author_lou": 1,
                        "original_lou": None,
                        "candidate_original_lous": [3, 4],
                    },
                    {
                        "pid": None,
                        "author_lou": 2,
                        "original_lou": 5,
                        "original_pid": 2002,
                    },
                ]
            )

            store.replace_floor_map(expected)
            actual = store.read_floor_map()
            with closing(sqlite3.connect(store.db_path)) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_schema
                        WHERE type = 'table' AND name LIKE 'floor_map_%'
                        """
                    )
                }

        assert actual == expected
        assert table_names == {
            "floor_map_state",
            "floor_map_entries",
            "floor_map_candidates",
        }

    def test_author_floor_refresh_inputs_share_one_read_connection(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "vrows": 2,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "body",
                            "author": {"uid": 456},
                        }
                    ],
                },
            )
            expected_floor_map = _stored_floor_map(
                [{"pid": 1001, "author_lou": 1, "original_lou": 3}]
            )
            store.replace_floor_map(expected_floor_map)

            reader = ThreadArchiveStore(store.thread_folder)
            with patch.object(
                reader,
                "_connect_read",
                wraps=reader._connect_read,
            ) as connect_read:
                inputs = reader.read_author_floor_refresh_inputs()

        assert connect_read.call_count == 1
        assert inputs.post_refs == ({"pid": 1001, "author_lou": 1},)
        assert inputs.stored_floor_map == expected_floor_map
        assert inputs.floor_map_error is None

    def test_latest_author_refs_keep_tie_break_and_anonymous_filtering(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            observed_at = "2026-07-13T01:00:00+00:00"
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "old",
                            "author": {"uid": 456},
                        },
                        {
                            "lou": 2,
                            "pid": 2001,
                            "content": "visible",
                            "author": {"uid": 456},
                        },
                    ],
                },
                observed_at=observed_at,
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1002,
                            "content": "new",
                            "author": {"uid": 456},
                        },
                        {
                            "lou": 2,
                            "pid": 2002,
                            "content": "anonymous",
                            "author": {"uid": -1},
                        },
                    ],
                },
                observed_at=observed_at,
            )

            refs = store.read_latest_author_post_refs()

        assert refs == [{"pid": 1002, "author_lou": 1}]

    def test_schema_rejects_legacy_latest_post_index_without_mutating_it(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "DROP INDEX idx_post_versions_latest_covering"
                )
                connection.execute(
                    """
                    CREATE INDEX idx_post_versions_latest
                    ON post_versions(lou, last_seen_at, id)
                    """
                )
                connection.commit()

            with pytest.raises(UnsupportedStorageFormatError, match="索引不符合"):
                ThreadArchiveStore(store.thread_folder).ensure_schema()
            with closing(sqlite3.connect(store.db_path)) as connection:
                indexes = dict(
                    connection.execute(
                        """
                        SELECT name, sql
                        FROM sqlite_schema
                        WHERE type = 'index'
                          AND name LIKE 'idx_post_versions_latest%'
                        """
                    ).fetchall()
                )
                query_plan = connection.execute(
                    f"EXPLAIN QUERY PLAN {_LATEST_AUTHOR_POST_REFS_QUERY}"
                ).fetchall()

        assert set(indexes) == {"idx_post_versions_latest"}
        assert not any(
            "idx_post_versions_latest_covering" in detail
            for _node_id, _parent_id, _unused, detail in query_plan
        )

    def test_floor_map_replace_removes_stale_entries_and_candidates(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            store.replace_floor_map(
                _stored_floor_map(
                    [
                        {
                            "pid": None,
                            "author_lou": 1,
                            "original_lou": None,
                            "candidate_original_lous": [10, 11],
                        }
                    ]
                )
            )
            replacement = _stored_floor_map(
                [{"pid": 1002, "author_lou": 2, "original_lou": 12}],
                input_signature="replacement",
            )

            store.replace_floor_map(replacement)
            actual = store.read_floor_map()

        assert actual == replacement

    def test_identical_floor_map_replace_does_not_increment_revision(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            floor_map = _stored_floor_map(
                [
                    {"pid": 1002, "author_lou": 2, "original_lou": 12},
                    {
                        "pid": None,
                        "author_lou": 1,
                        "original_lou": None,
                        "candidate_original_lous": [10, 11],
                    },
                ]
            )

            first_changed = store.replace_floor_map(floor_map)
            after_first = store.state.read_backup_processing_snapshot().change_state
            repeated_changed = store.replace_floor_map(floor_map)
            after_repeated = store.state.read_backup_processing_snapshot().change_state

        assert first_changed
        assert not repeated_changed
        assert after_first.floor_map_revision == 1
        assert after_repeated.floor_map_revision == 1

    def test_invalid_floor_map_does_not_replace_existing_data(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            existing = _stored_floor_map(
                [{"pid": 1001, "author_lou": 1, "original_lou": 10}]
            )
            store.replace_floor_map(existing)
            invalid = _stored_floor_map(
                [
                    {
                        "pid": 1001,
                        "author_lou": 1,
                        "original_lou": 10,
                        "original_pid": 2001,
                    }
                ]
            )

            with pytest.raises(ValueError, match="original_pid"):
                store.replace_floor_map(invalid)

            actual = store.read_floor_map()

        assert actual == existing

    def test_upsert_page_stores_word_count_fields(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "正文，"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                row = connection.execute(
                    """
                    SELECT
                        word_count_version,
                        word_count_chinese_chars,
                        word_count_chinese_with_punctuation
                    FROM post_versions
                    WHERE pid = 1001
                    """
                ).fetchone()

        assert row == (WORD_COUNT_VERSION, 2, 3)

    def test_upsert_pages_commits_one_batch_revision_and_preserves_counts(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            pages = {
                1: {
                    "currentPage": 1,
                    "totalPage": 2,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "第一页正文"}
                    ],
                },
                2: {
                    "currentPage": 2,
                    "totalPage": 2,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "第二页正文"}
                    ],
                },
            }

            first = store.upsert_pages(
                pages,
                observed_at="2026-07-11T01:00:00+00:00",
            )
            after_first = store.state.read_backup_processing_snapshot().change_state
            repeated = store.upsert_pages(
                pages,
                observed_at="2026-07-11T02:00:00+00:00",
            )
            after_repeated = store.state.read_backup_processing_snapshot().change_state
            records = store.read_effective_post_records()
            with closing(sqlite3.connect(store.db_path)) as connection:
                page_rows = connection.execute(
                    """
                    SELECT page_number, total_page, vrows, last_seen_at
                    FROM archive_pages ORDER BY page_number
                    """
                ).fetchall()
                post_seen_counts = connection.execute(
                    "SELECT seen_count FROM post_versions ORDER BY lou"
                ).fetchall()
                metadata_seen_counts = connection.execute(
                    "SELECT seen_count FROM post_latest_metadata ORDER BY lou"
                ).fetchall()

        assert first.pages_processed == 2
        assert first.post_versions_inserted == 2
        assert first.effective_changed_pages == 2
        assert first.effective_changed_lous == frozenset({1, 2})
        assert first.effective_added_lous == frozenset({1, 2})
        assert repeated.post_versions_inserted == 0
        assert repeated.effective_changed_pages == 0
        assert repeated.effective_changed_lous == frozenset()
        assert repeated.effective_added_lous == frozenset()
        assert after_first.archive_revision == 1
        assert after_repeated.archive_revision == 1
        assert [record["lou"] for record in records] == [1, 2]
        assert page_rows == [
            (1, 2, None, "2026-07-11T02:00:00+00:00"),
            (2, 2, None, "2026-07-11T02:00:00+00:00"),
        ]
        assert post_seen_counts == [(2,), (2,)]
        assert metadata_seen_counts == [(2,), (2,)]

    def test_page_change_preflight_is_read_only_and_matches_effective_inputs(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            first_page = {
                "currentPage": 1,
                "totalPage": 2,
                "vrows": 2,
                "result": [
                    {
                        "lou": 1,
                        "pid": 1001,
                        "content": "first",
                        "author": {"uid": 456},
                    }
                ],
            }
            store.upsert_page(1, first_page)
            revision_before = (
                store.state.read_backup_processing_snapshot()
                .change_state.archive_revision
            )

            unchanged = store.page_effective_processing_inputs_changed(
                1,
                first_page,
            )
            changed_page = dict(first_page)
            changed_page["result"] = [
                {
                    "lou": 1,
                    "pid": 1001,
                    "content": "edited",
                    "author": {"uid": 456},
                }
            ]
            changed = store.page_effective_processing_inputs_changed(
                1,
                changed_page,
            )
            revision_after = (
                store.state.read_backup_processing_snapshot()
                .change_state.archive_revision
            )

        assert not unchanged
        assert changed
        assert revision_after == revision_before

    def test_latest_page_one_pagination_uses_newest_state(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {"totalPage": 1, "vrows": 2, "result": []},
                observed_at="2026-07-11T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {"totalPage": 3, "vrows": 8, "result": []},
                observed_at="2026-07-11T02:00:00+00:00",
            )

            pagination = store.read_latest_page_one_pagination()

        assert pagination is not None
        assert pagination.page_count == 3
        assert pagination.vrows == 8

    def test_compact_page_state_rejects_stale_updates_and_clears_vrows(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {"totalPage": 3, "vrows": 8, "result": []},
                observed_at="2026-07-11T02:00:00+00:00",
            )
            store.upsert_page(
                1,
                {"totalPage": 1, "vrows": 2, "result": []},
                observed_at="2026-07-11T01:00:00+00:00",
            )
            after_stale = store.read_latest_page_one_pagination()
            with closing(sqlite3.connect(store.db_path)) as connection:
                stored_at = connection.execute(
                    "SELECT last_seen_at FROM archive_pages WHERE page_number = 1"
                ).fetchone()

            store.upsert_page(
                1,
                {"totalPage": 2, "result": []},
                observed_at="2026-07-11T03:00:00+00:00",
            )
            after_newer = store.read_latest_page_one_pagination()

        assert after_stale is not None
        assert after_stale.page_count == 3
        assert after_stale.vrows == 8
        assert stored_at == ("2026-07-11T02:00:00+00:00",)
        assert after_newer is not None
        assert after_newer.page_count == 2
        assert after_newer.vrows is None

    def test_runtime_rejects_unsupported_archive_schema(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            store = ThreadArchiveStore(thread_folder)
            store.upsert_page(1, {"totalPage": 1, "result": []})
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute("PRAGMA user_version = 0")
                connection.commit()

            reader = ThreadArchiveStore(thread_folder)
            with pytest.raises(UnsupportedStorageFormatError, match="版本不受支持"):
                reader.read_page_numbers()
            with pytest.raises(UnsupportedStorageFormatError, match="版本不受支持"):
                reader.ensure_schema()

    def test_upsert_pages_rolls_back_every_page_when_one_write_fails(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            original_upsert = store._upsert_post_latest_metadata
            call_count = 0

            def fail_second_post(*args: object, **kwargs: object) -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise RuntimeError("second page failed")
                original_upsert(*args, **kwargs)  # type: ignore[arg-type]

            with (
                patch.object(
                    store,
                    "_upsert_post_latest_metadata",
                    side_effect=fail_second_post,
                ),
                pytest.raises(RuntimeError, match="second page failed"),
            ):
                store.upsert_pages(
                    {
                        1: {
                            "totalPage": 2,
                            "result": [
                                {"lou": 1, "pid": 1001, "content": "first"}
                            ],
                        },
                        2: {
                            "totalPage": 2,
                            "result": [
                                {"lou": 2, "pid": 1002, "content": "second"}
                            ],
                        },
                    }
                )

            snapshot = store.state.read_backup_processing_snapshot()
            page_numbers = store.read_page_numbers()
            records = store.read_latest_post_records()

        assert page_numbers == set()
        assert records == []
        assert snapshot.change_state.archive_revision == 0

    def test_schema_initializes_once_and_reads_use_readonly_connections(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            store = ThreadArchiveStore(thread_folder)
            with patch.object(
                store,
                "_ensure_schema",
                wraps=store._ensure_schema,
            ) as ensure_schema:
                store.upsert_pages(
                    {
                        1: {
                            "totalPage": 1,
                            "result": [
                                {"lou": 1, "pid": 1001, "content": "body"}
                            ],
                        }
                    }
                )
                assert store.read_page_numbers() == {1}
                assert store.refresh_stored_word_counts() == 0
            assert ensure_schema.call_count == 1

            reader = ThreadArchiveStore(thread_folder)
            with patch.object(
                reader,
                "_ensure_schema",
                side_effect=AssertionError("read path initialized schema"),
            ) as ensure_schema_on_read:
                assert reader.read_page_numbers() == {1}
                assert len(reader.read_effective_post_records()) == 1
            ensure_schema_on_read.assert_not_called()
            with closing(reader._connect_read()) as connection:
                with pytest.raises(sqlite3.OperationalError, match="readonly"):
                    connection.execute("DELETE FROM archive_change_state")

    def test_page_refresh_preserves_posts_missing_from_new_response(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "old visible"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "new visible"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            records = store.read_latest_post_records()

        assert [record['lou'] for record in records] == [1, 2]
        assert records[0]['post']['content'] == 'old visible'
        assert records[1]['post']['content'] == 'new visible'

    def test_same_lou_uses_latest_version_but_keeps_old_version(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            records = store.read_latest_post_records()
            with closing(sqlite3.connect(store.db_path)) as connection:
                version_count = connection.execute(
                    "SELECT COUNT(*) FROM post_versions WHERE pid = 1001 AND lou = 1"
                ).fetchone()[0]

        assert len(records) == 1
        assert records[0]['post']['content'] == 'after edit'
        assert version_count == 2

    def test_effective_post_records_use_valid_manual_selection(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            store = ThreadArchiveStore(thread_folder)

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                old_version_id, old_source_hash = connection.execute(
                    """
                    SELECT id, source_hash
                    FROM post_versions
                    WHERE source_hash = ?
                    """,
                    (hash_text("before edit"),),
                ).fetchone()
            store.upsert_post_version_selection(1, old_version_id)

            latest_records = store.read_latest_post_records()
            effective_records = store.read_effective_post_records()
            summaries = store.read_effective_post_record_summaries()

        assert latest_records[0]["post"]["content"] == "after edit"
        assert effective_records[0]["post"]["content"] == "before edit"
        assert summaries[0]["source_hash"] == old_source_hash

    def test_effective_post_records_ignore_latest_manual_selection(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            store = ThreadArchiveStore(thread_folder)

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                latest_version_id = connection.execute(
                    """
                    SELECT id
                    FROM post_versions
                    WHERE source_hash = ?
                    """,
                    (hash_text("after edit"),),
                ).fetchone()[0]
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    """
                    INSERT INTO post_version_selections (
                        lou,
                        version_id,
                        selected_at
                    )
                    VALUES (?, ?, ?)
                    """,
                    (
                        1,
                        latest_version_id,
                        "2026-07-08T00:00:00+00:00",
                    ),
                )
                connection.commit()

            records = store.read_effective_post_records()

        assert records[0]["post"]["content"] == "after edit"

    def test_post_version_selection_crud_and_fingerprint(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                version_ids = {
                    source_hash: version_id
                    for version_id, source_hash in connection.execute(
                        "SELECT id, source_hash FROM post_versions"
                    )
                }

            empty_fingerprint = store.post_version_selections_fingerprint()
            with pytest.raises(
                ValueError,
                match="不能手动选择当前最新版",
            ):
                store.upsert_post_version_selection(
                    1,
                    version_ids[hash_text("after edit")],
                )

            selection = store.upsert_post_version_selection(
                1,
                version_ids[hash_text("before edit")],
            )
            selected_fingerprint = store.post_version_selections_fingerprint()
            repeated = store.upsert_post_version_selection(
                1,
                version_ids[hash_text("before edit")],
            )
            repeated_fingerprint = store.post_version_selections_fingerprint()
            with closing(sqlite3.connect(store.db_path)) as connection:
                stored_row = connection.execute(
                    """
                    SELECT lou, version_id, selected_at
                    FROM post_version_selections
                    """
                ).fetchone()

            latest_version_id = store.delete_post_version_selection(1)
            cleared_selections = store.read_valid_post_version_selections()
            cleared_fingerprint = store.post_version_selections_fingerprint()

        assert selection["source_hash"]
        assert repeated["version_id"] == version_ids[hash_text("before edit")]
        assert stored_row == (
            1,
            version_ids[hash_text("before edit")],
            repeated["selected_at"],
        )
        assert selected_fingerprint != empty_fingerprint
        assert repeated_fingerprint == selected_fingerprint
        assert latest_version_id == version_ids[hash_text("after edit")]
        assert cleared_selections == {}
        assert cleared_fingerprint == empty_fingerprint

    def test_missing_selection_table_is_rejected_without_recreation(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            store = ThreadArchiveStore(thread_folder)
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                old_version_id = connection.execute(
                    """
                    SELECT id FROM post_versions WHERE source_hash = ?
                    """,
                    (hash_text("before edit"),),
                ).fetchone()[0]
                connection.execute("DROP TABLE post_version_selections")
                connection.commit()

            reopened = ThreadArchiveStore(thread_folder)
            with pytest.raises(UnsupportedStorageFormatError):
                reopened.read_valid_post_version_selections()
            with pytest.raises(UnsupportedStorageFormatError):
                reopened.upsert_post_version_selection(1, old_version_id)
            with closing(sqlite3.connect(store.db_path)) as connection:
                table_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_schema
                    WHERE type = 'table'
                      AND name = 'post_version_selections'
                    """
                ).fetchone()

        assert table_exists is None

    def test_metadata_only_change_does_not_create_post_version(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            first = store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "same body",
                            "postdate": 1001,
                            "vote_good": 0,
                            "author": {
                                "uid": 2001,
                                "username": "author",
                                "postnum": 10,
                            },
                        },
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            second = store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "same body",
                            "postdate": 1002,
                            "vote_good": 1,
                            "author": {
                                "uid": 2001,
                                "username": "author",
                                "postnum": 11,
                            },
                        },
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            with closing(sqlite3.connect(store.db_path)) as connection:
                version_row = connection.execute(
                    """
                    SELECT COUNT(*), source_hash, seen_count
                    FROM post_versions
                    WHERE pid = 1001 AND lou = 1
                    """
                ).fetchone()
                metadata_row = connection.execute(
                    """
                    SELECT author_name, author_uid, postdate_json, seen_count
                    FROM post_latest_metadata
                    WHERE pid = 1001 AND lou = 1
                    """
                ).fetchone()

        assert first.post_versions_inserted == 1
        assert second.post_versions_inserted == 0
        assert version_row == (1, hash_text("same body"), 2)
        assert metadata_row == ("author", 2001, "1002", 2)

    def test_old_schema_is_rejected_without_mutation(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE page_snapshots (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        page_number INTEGER NOT NULL,
                        response_hash TEXT NOT NULL,
                        page_json TEXT NOT NULL,
                        current_page INTEGER,
                        total_page INTEGER,
                        vrows INTEGER,
                        msg TEXT,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        seen_count INTEGER NOT NULL,
                        UNIQUE(page_number, response_hash)
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO page_snapshots (
                        page_number, response_hash, page_json, total_page,
                        first_seen_at, last_seen_at, seen_count
                    ) VALUES
                        (1, 'page-1', '{}', 1, '2026-07-07T01:00:00+00:00',
                         '2026-07-07T01:00:00+00:00', 1),
                        (1, 'page-2', '{}', 2, '2026-07-07T02:00:00+00:00',
                         '2026-07-07T02:00:00+00:00', 1)
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE post_versions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        pid INTEGER NOT NULL,
                        lou INTEGER NOT NULL,
                        post_hash TEXT NOT NULL,
                        source_hash TEXT NOT NULL,
                        post_json TEXT NOT NULL,
                        content TEXT NOT NULL,
                        first_seen_at TEXT NOT NULL,
                        last_seen_at TEXT NOT NULL,
                        seen_count INTEGER NOT NULL
                    )
                    """
                )
                for post_hash, postnum, vote_good, seen_at in [
                    ("raw-1", 10, 0, "2026-07-07T01:00:00+00:00"),
                    ("raw-2", 11, 1, "2026-07-07T02:00:00+00:00"),
                ]:
                    connection.execute(
                        """
                        INSERT INTO post_versions (
                            pid,
                            lou,
                            post_hash,
                            source_hash,
                            post_json,
                            content,
                            first_seen_at,
                            last_seen_at,
                            seen_count
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            1001,
                            1,
                            post_hash,
                            f"source-{post_hash}",
                            json.dumps(
                                {
                                    "lou": 1,
                                    "pid": 1001,
                                    "content": "same body",
                                    "vote_good": vote_good,
                                    "author": {
                                        "uid": 2001,
                                        "username": "author",
                                        "postnum": postnum,
                                    },
                                },
                                ensure_ascii=False,
                            ),
                            "same body",
                            seen_at,
                            seen_at,
                            1,
                        ),
                    )
                connection.commit()

            with pytest.raises(UnsupportedStorageFormatError):
                store.ensure_schema()
            with closing(sqlite3.connect(store.db_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute(
                        "PRAGMA table_info(post_versions)"
                    ).fetchall()
                }
                version_count = connection.execute(
                    "SELECT COUNT(*) FROM post_versions"
                ).fetchone()[0]

        assert "post_hash" in columns
        assert "post_json" in columns
        assert version_count == 2

    def test_read_latest_post_record_summaries_skip_post_json(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 2, "pid": 1002, "content": "second"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )

            summaries = store.read_latest_post_record_summaries()

        assert [record['lou'] for record in summaries] == [1, 2]
        assert [record['pid'] for record in summaries] == [1001, 1002]
        assert all(record['post'] is None for record in summaries)
        assert all(record['html'] is None for record in summaries)
        assert all(record['source_hash'] for record in summaries)

    def test_read_latest_post_records_can_filter_lous(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 2, "pid": 1002, "content": "second"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )

            records = store.read_latest_post_records({2})
            empty_records = store.read_latest_post_records(set())

        assert [record['lou'] for record in records] == [2]
        assert records[0]['post']['content'] == 'second'
        assert empty_records == []

    def test_read_latest_author_total_lou_count_uses_latest_page_one_snapshot(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "vrows": 2,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "vrows": 4,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 3, "pid": 1003, "content": "third"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            total_lou_count = store.read_latest_author_total_lou_count()

        assert total_lou_count == 4

    def test_read_latest_author_total_lou_count_ignores_stale_vrows(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "vrows": 4,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            total_lou_count = store.read_latest_author_total_lou_count()

        assert total_lou_count is None

    def test_json_pages_are_not_imported(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            json_dir = thread_folder / "json"
            json_dir.mkdir()
            page_path = json_dir / "page_1.json"
            page_path.write_text(
                json.dumps(
                    {
                        "totalPage": 1,
                        "result": [
                            {"lou": 1, "pid": 1001, "content": "from json"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = ThreadArchiveStore(thread_folder)

            store.ensure_schema()
            json_still_exists = page_path.is_file()
            page_numbers = store.read_page_numbers()

        assert json_still_exists
        assert page_numbers == set()

    def test_processing_state_and_pending_images_round_trip_atomically(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            floor_state = FloorProcessingState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                processed_floor_map_revision=(
                    initial.change_state.floor_map_revision
                ),
                page_count=2,
                author_total_lou_count=21,
                floor_map_format_version=1,
                floor_map_generation_version=1,
                floor_map_hash_algorithm="sha256",
                completed_at="2026-07-11T00:00:00+00:00",
            )
            image_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )

            assert store.state.commit_floor_processing_state(floor_state)
            manifest_posts = (
                ImageReferenceManifestPost(
                    lou=1,
                    cache_key="cache-1",
                    references=(
                        ImageReferenceManifestEntry(
                            image_index=1,
                            url=(
                                "https://img.nga.178.com/attachments/"
                                "mon_202607/11/manifest-a.png"
                            ),
                            valid=True,
                        ),
                        ImageReferenceManifestEntry(
                            image_index=2,
                            url=(
                                "https://img.nga.178.com/attachments/"
                                "mon_202607/11/manifest-b.png"
                            ),
                            valid=True,
                        ),
                    ),
                ),
                ImageReferenceManifestPost(
                    lou=2,
                    cache_key="cache-2",
                    references=(),
                ),
            )
            assert store.state.commit_image_reference_state(
                image_state,
                (
                    _pending_retry("https://example.invalid/b.png"),
                    _pending_retry("https://example.invalid/a.png"),
                ),
                manifest_posts=manifest_posts,
            )
            stored = store.state.read_backup_processing_snapshot()
            stored_manifest = store.state.read_image_reference_manifest()
            store.state.clear_backup_processing_state()
            cleared = store.state.read_backup_processing_snapshot()
            cleared_manifest = store.state.read_image_reference_manifest()
            with closing(sqlite3.connect(store.state.db_path)) as connection:
                table_names = {
                    row[0]
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_schema
                        WHERE type = 'table' AND name IN (
                            'backup_floor_processing_state',
                            'backup_image_reference_state',
                            'backup_image_reference_manifest_state',
                            'backup_image_reference_manifest_posts',
                            'backup_image_reference_manifest_entries',
                            'backup_image_reference_manifest_urls',
                            'backup_pending_images'
                        )
                        """
                    )
                }

        assert stored.floor_state == floor_state
        assert stored.image_state == image_state
        assert stored_manifest == ImageReferenceManifestSnapshot(
            state=ImageReferenceManifestState(
                format_version=IMAGE_REFERENCE_MANIFEST_VERSION,
                processed_archive_revision=(
                    initial.change_state.archive_revision
                ),
            ),
            posts=manifest_posts,
            url_reference_counts=(
                (
                    "https://img.nga.178.com/attachments/"
                    "mon_202607/11/manifest-a.png",
                    1,
                    True,
                ),
                (
                    "https://img.nga.178.com/attachments/"
                    "mon_202607/11/manifest-b.png",
                    1,
                    True,
                ),
            ),
        )
        assert stored.pending_image_retries == (
            _pending_retry("https://example.invalid/a.png"),
            _pending_retry("https://example.invalid/b.png"),
        )
        assert cleared.floor_state is None
        assert cleared.image_state is None
        assert cleared.pending_image_retries == ()
        assert cleared_manifest is None
        assert table_names == {
            "backup_floor_processing_state",
            "backup_image_reference_state",
            "backup_image_reference_manifest_state",
            "backup_image_reference_manifest_posts",
            "backup_image_reference_manifest_entries",
            "backup_image_reference_manifest_urls",
            "backup_pending_images",
        }

    def test_unexpected_archive_table_is_rejected_without_mutation(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            image_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    "CREATE TABLE backup_image_references (url TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO backup_image_references VALUES (?)",
                    ("https://example.invalid/legacy.png",),
                )
                connection.commit()

            with pytest.raises(UnsupportedStorageFormatError):
                store.state.commit_image_reference_state(image_state, ())
            with closing(sqlite3.connect(store.db_path)) as connection:
                legacy_rows = connection.execute(
                    "SELECT url FROM backup_image_references"
                ).fetchall()

        assert legacy_rows == [("https://example.invalid/legacy.png",)]

    def test_processing_snapshot_does_not_scan_manifest_rows(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            image_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            manifest_posts = (
                ImageReferenceManifestPost(
                    lou=1,
                    cache_key="cache-1",
                    references=(
                        ImageReferenceManifestEntry(
                            image_index=1,
                            url=(
                                "https://img.nga.178.com/attachments/"
                                "mon_202607/11/corrupt-count.png"
                            ),
                            valid=True,
                        ),
                    ),
                ),
            )
            assert store.state.commit_image_reference_state(
                image_state,
                (),
                manifest_posts=manifest_posts,
            )
            with closing(sqlite3.connect(store.state.db_path)) as connection:
                connection.execute(
                    """
                    UPDATE backup_image_reference_manifest_urls
                    SET reference_count = 2
                    """
                )
                connection.commit()

            snapshot = store.state.read_backup_processing_snapshot()
            with pytest.raises(ValueError, match="URL引用计数不一致"):
                store.state.read_image_reference_manifest()

        assert snapshot.image_state == image_state

    def test_incremental_manifest_commit_updates_changed_lous_and_counts(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            old_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            old_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/incremental-old.png"
            )
            new_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/incremental-new.png"
            )
            shared_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/incremental-shared.png"
            )
            old_posts = (
                ImageReferenceManifestPost(
                    1,
                    "cache-old-1",
                    (ImageReferenceManifestEntry(1, old_url, True),),
                ),
                ImageReferenceManifestPost(
                    2,
                    "cache-old-2",
                    (ImageReferenceManifestEntry(1, shared_url, True),),
                ),
            )
            assert store.state.commit_image_reference_state(
                old_state,
                (_pending_retry(old_url),),
                manifest_posts=old_posts,
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [{"lou": 1, "pid": 1001, "content": "changed"}],
                },
            )
            current = store.state.read_backup_processing_snapshot()
            new_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=current.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T01:00:00+00:00",
            )
            changed_posts = (
                ImageReferenceManifestPost(
                    1,
                    "cache-new-1",
                    (
                        ImageReferenceManifestEntry(1, new_url, True),
                        ImageReferenceManifestEntry(2, shared_url, True),
                    ),
                ),
            )

            assert store.state.read_image_reference_manifest_posts({1}) == {
                1: old_posts[0]
            }
            assert store.state.read_image_reference_manifest_url_counts(
                {old_url, shared_url, new_url}
            ) == {
                old_url: (1, True),
                shared_url: (1, True),
            }
            assert store.state.commit_incremental_image_reference_state(
                old_state,
                new_state,
                (_pending_retry(new_url),),
                changed_posts,
            )
            manifest = store.state.read_image_reference_manifest()
            assert not store.state.commit_incremental_image_reference_state(
                old_state,
                new_state,
                (),
                changed_posts,
            )

        assert manifest is not None
        assert manifest.posts == (changed_posts[0], old_posts[1])
        assert manifest.url_reference_counts == (
            (new_url, 1, True),
            (shared_url, 2, True),
        )

    def test_bootstrap_manifest_commit_requires_absent_manifest(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            old_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            assert store.state.commit_image_reference_state(old_state, ())
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [{"lou": 1, "pid": 1001, "content": "new"}],
                },
            )
            current = store.state.read_backup_processing_snapshot()
            new_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=current.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T01:00:00+00:00",
            )
            posts = (
                ImageReferenceManifestPost(1, "cache-new", ()),
            )

            assert store.state.commit_bootstrapped_image_reference_state(
                old_state,
                new_state,
                (),
                posts,
            )
            assert not store.state.commit_bootstrapped_image_reference_state(
                old_state,
                new_state,
                (),
                posts,
            )
            manifest = store.state.read_image_reference_manifest()

        assert manifest is not None
        assert manifest.posts == posts

    def test_legacy_processing_state_is_not_read_by_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            with closing(sqlite3.connect(store.db_path)) as connection:
                connection.execute(
                    """
                    CREATE TABLE backup_processing_state (
                        singleton INTEGER PRIMARY KEY,
                        format_version INTEGER NOT NULL,
                        processed_archive_revision INTEGER NOT NULL,
                        processed_floor_map_revision INTEGER NOT NULL,
                        page_count INTEGER NOT NULL,
                        author_total_lou_count INTEGER,
                        post_overlays_fingerprint TEXT NOT NULL,
                        post_version_selections_fingerprint TEXT NOT NULL,
                        floor_map_format_version INTEGER NOT NULL,
                        floor_map_generation_version INTEGER NOT NULL,
                        floor_map_hash_algorithm TEXT NOT NULL,
                        image_reference_extractor_version INTEGER NOT NULL,
                        completed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO backup_processing_state VALUES
                    (1, 1, 0, 0, 2, 21, 'overlay', 'selection',
                     1, 1, 'sha256', 1, '2026-07-11T00:00:00+00:00')
                    """
                )
                connection.commit()

            with pytest.raises(UnsupportedStorageFormatError):
                store.ensure_schema()
            with closing(sqlite3.connect(store.db_path)) as connection:
                legacy_exists = connection.execute(
                    """
                    SELECT 1 FROM sqlite_schema
                    WHERE type = 'table' AND name = 'backup_processing_state'
                    """
                ).fetchone()

        assert legacy_exists == (1,)
        assert not store.state.db_path.exists()

    def test_legacy_pending_image_urls_are_not_read_by_runtime(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            db_path = thread_folder / "archive.sqlite3"
            with closing(sqlite3.connect(db_path)) as connection:
                connection.execute(
                    "CREATE TABLE backup_pending_images (url TEXT PRIMARY KEY)"
                )
                connection.execute(
                    "INSERT INTO backup_pending_images (url) VALUES (?)",
                    ("https://example.invalid/legacy-retry.png",),
                )
                connection.commit()

            store = ThreadArchiveStore(thread_folder)
            with pytest.raises(UnsupportedStorageFormatError):
                store.state.ensure_schema()
                store.cache.ensure_schema()

        assert not store.state.db_path.exists()

    def test_current_processing_schema_skips_full_schema_initialization(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            ThreadArchiveStore(thread_folder).ensure_schema()
            store = ThreadArchiveStore(thread_folder)

            with patch.object(
                ThreadArchiveStore,
                "_ensure_schema",
                autospec=True,
            ) as ensure_schema:
                store.state.ensure_schema()
                store.cache.ensure_schema()
                snapshot = store.state.read_backup_processing_snapshot()
                committed = store.state.commit_image_reference_state(
                    ImageReferenceState(
                        format_version=1,
                        processed_archive_revision=(
                            snapshot.change_state.archive_revision
                        ),
                        post_overlays_fingerprint="overlay-hash",
                        post_version_selections_fingerprint="selection-hash",
                        image_reference_extractor_version=1,
                        completed_at="2026-07-11T00:00:00+00:00",
                    ),
                    (),
                )

        assert committed
        ensure_schema.assert_not_called()

    def test_page_revision_tracks_only_effective_processing_inputs(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            base_page = {
                "totalPage": 1,
                "result": [
                    {
                        "lou": 1,
                        "pid": 1001,
                        "content": "same body",
                        "author": {"uid": 456},
                        "attches": [],
                    }
                ],
            }

            first = store.upsert_page(
                1,
                base_page,
                observed_at="2026-07-11T01:00:00+00:00",
            )
            after_first = store.state.read_backup_processing_snapshot().change_state
            repeated = store.upsert_page(
                1,
                base_page,
                observed_at="2026-07-11T02:00:00+00:00",
            )
            after_repeated = store.state.read_backup_processing_snapshot().change_state
            changed_attachments = store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "same body",
                            "author": {"uid": 456},
                            "attches": [
                                {
                                    "type": "img",
                                    "attachurl": "mon_202607/11/new.png",
                                }
                            ],
                        }
                    ],
                },
                observed_at="2026-07-11T03:00:00+00:00",
            )
            after_attachments = (
                store.state.read_backup_processing_snapshot().change_state
            )
            changed_author = store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "same body",
                            "author": {"uid": -1},
                            "attches": [
                                {
                                    "type": "img",
                                    "attachurl": "mon_202607/11/new.png",
                                }
                            ],
                        }
                    ],
                },
                observed_at="2026-07-11T04:00:00+00:00",
            )
            after_author = store.state.read_backup_processing_snapshot().change_state

        assert first.effective_processing_inputs_changed
        assert first.effective_changed_lous == frozenset({1})
        assert first.effective_added_lous == frozenset({1})
        assert not repeated.effective_processing_inputs_changed
        assert repeated.effective_changed_lous == frozenset()
        assert repeated.effective_added_lous == frozenset()
        assert not changed_attachments.effective_processing_inputs_changed
        assert changed_attachments.effective_changed_lous == frozenset()
        assert changed_attachments.effective_added_lous == frozenset()
        assert changed_author.effective_processing_inputs_changed
        assert changed_author.effective_changed_lous == frozenset({1})
        assert changed_author.effective_added_lous == frozenset()
        assert after_first.archive_revision == 1
        assert after_repeated.archive_revision == 1
        assert after_attachments.archive_revision == 1
        assert after_author.archive_revision == 2

    def test_historical_version_becoming_latest_bumps_archive_revision(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            for observed_at, content in (
                ("2026-07-11T01:00:00+00:00", "version A"),
                ("2026-07-11T02:00:00+00:00", "version B"),
                ("2026-07-11T03:00:00+00:00", "version A"),
            ):
                result = store.upsert_page(
                    1,
                    {
                        "totalPage": 1,
                        "result": [
                            {"lou": 1, "pid": 1001, "content": content}
                        ],
                    },
                    observed_at=observed_at,
                )
                assert result.effective_processing_inputs_changed

            snapshot = store.state.read_backup_processing_snapshot()
            records = store.read_latest_post_records()

        assert snapshot.change_state.archive_revision == 3
        assert records[0]["post"]["content"] == "version A"

    def test_floor_map_and_recovered_body_increment_their_revisions(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            store.replace_floor_map(
                _stored_floor_map(
                    [{"pid": 1001, "author_lou": 1, "original_lou": 10}]
                )
            )
            after_floor_map = store.state.read_backup_processing_snapshot().change_state
            recovered: RecoveredMissingPost = {
                "original_pid": 2002,
                "original_lou": 11,
                "content": "recovered body",
                "raw_post": {
                    "lou": 11,
                    "pid": 2002,
                    "content": "recovered body",
                    "author": {"uid": -1},
                    "attches": [],
                },
            }
            first_recovery = store.upsert_recovered_posts({2: recovered})
            after_recovery = store.state.read_backup_processing_snapshot().change_state
            repeated_recovery = store.upsert_recovered_posts({2: recovered})
            after_repeat = store.state.read_backup_processing_snapshot().change_state

        assert after_floor_map.floor_map_revision == 1
        assert after_floor_map.archive_revision == 0
        assert first_recovery.inserted_count == 1
        assert first_recovery.effective_changed_lous == frozenset({2})
        assert first_recovery.effective_added_lous == frozenset({2})
        assert after_recovery.archive_revision == 1
        assert repeated_recovery.inserted_count == 0
        assert repeated_recovery.effective_changed_lous == frozenset()
        assert repeated_recovery.effective_added_lous == frozenset()
        assert after_repeat.archive_revision == 1

    def test_processing_state_commit_rejects_stale_archive_revision(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            stale_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [{"lou": 1, "pid": 1001, "content": "new"}],
                },
            )

            committed = store.state.commit_image_reference_state(
                stale_state,
                (_pending_retry("https://example.invalid/stale.png"),),
            )
            snapshot = store.state.read_backup_processing_snapshot()

        assert not committed
        assert snapshot.image_state is None
        assert snapshot.pending_image_retries == ()

    def test_pending_retry_update_rejects_changed_archive(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))
            store.ensure_schema()
            initial = store.state.read_backup_processing_snapshot()
            image_state = ImageReferenceState(
                format_version=1,
                processed_archive_revision=initial.change_state.archive_revision,
                post_overlays_fingerprint="overlay-hash",
                post_version_selections_fingerprint="selection-hash",
                image_reference_extractor_version=1,
                completed_at="2026-07-11T00:00:00+00:00",
            )
            assert store.state.commit_image_reference_state(
                image_state,
                (_pending_retry("https://example.invalid/original.png"),),
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [{"lou": 1, "pid": 1001, "content": "new"}],
                },
            )

            replaced = store.state.replace_pending_images_for_image_state(
                image_state,
                (_pending_retry("https://example.invalid/replacement.png"),),
            )
            snapshot = store.state.read_backup_processing_snapshot()

        assert not replaced
        assert snapshot.pending_image_retries == (
            _pending_retry("https://example.invalid/original.png"),
        )

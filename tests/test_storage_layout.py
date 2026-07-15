from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup.archive_store import (
    PostImageReferenceCacheEntry,
    ThreadArchiveStore,
)
from nga_tools.backup.processing_state import ArchiveChangeState, ImageReferenceState
from nga_tools.storage import STORAGE_LAYOUT_VERSION, read_storage_metadata


def _table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_schema
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                """
            )
            if isinstance(row[0], str)
        }


def test_thread_databases_have_disjoint_roles_and_source_binding(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "123_456")
    store.ensure_schema()
    store.read_backup_processing_snapshot()
    store.upsert_post_image_reference_cache(
        [
            PostImageReferenceCacheEntry(
                cache_key="key",
                source_hash="source",
                extractor_version=1,
                references_json="[]",
            )
        ]
    )

    data_tables = _table_names(store.db_path)
    state_tables = _table_names(store.state_store.db_path)
    cache_tables = _table_names(store.cache_store.db_path)
    assert "archive_change_state" in data_tables
    assert "backup_floor_processing_state" not in data_tables
    assert "post_image_reference_cache" not in data_tables
    assert "backup_floor_processing_state" in state_tables
    assert "archive_change_state" not in state_tables
    assert cache_tables == {"storage_metadata", "post_image_reference_cache"}

    with (
        closing(sqlite3.connect(store.db_path)) as data_connection,
        closing(sqlite3.connect(store.state_store.db_path)) as state_connection,
        closing(sqlite3.connect(store.cache_store.db_path)) as cache_connection,
    ):
        data_metadata = read_storage_metadata(data_connection)
        state_metadata = read_storage_metadata(state_connection)
        cache_metadata = read_storage_metadata(cache_connection)

    assert data_metadata is not None
    assert state_metadata is not None
    assert cache_metadata is not None
    assert data_metadata.role == "archive_data"
    assert state_metadata.role == "archive_state"
    assert cache_metadata.role == "archive_cache"
    assert data_metadata.layout_version == STORAGE_LAYOUT_VERSION
    assert state_metadata.source_store_id == data_metadata.store_id
    assert cache_metadata.source_store_id == data_metadata.store_id


def test_mismatched_state_store_is_quarantined_and_rebuilt(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "123_456")
    store.ensure_schema()
    snapshot = store.read_backup_processing_snapshot()
    state = ImageReferenceState(
        format_version=1,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint="overlay",
        post_version_selections_fingerprint="selection",
        image_reference_extractor_version=1,
        completed_at="2026-07-15T00:00:00+00:00",
    )
    assert store.commit_image_reference_state(state, ())
    with closing(sqlite3.connect(store.state_store.db_path)) as connection:
        connection.execute(
            "UPDATE storage_metadata SET source_store_id = 'wrong' WHERE singleton = 1"
        )
        connection.commit()

    rebuilt = store.read_backup_processing_snapshot()

    assert rebuilt.image_state is None
    assert list(store.thread_folder.glob("archive_state.sqlite3.corrupt-*"))
    with closing(sqlite3.connect(store.state_store.db_path)) as connection:
        metadata = read_storage_metadata(connection)
    assert metadata is not None
    assert metadata.source_store_id == store.archive_store_id()


def test_state_commit_double_checks_archive_revision(tmp_path: Path) -> None:
    store = ThreadArchiveStore(tmp_path / "123_456")
    store.ensure_schema()
    snapshot = store.read_backup_processing_snapshot()
    state = ImageReferenceState(
        format_version=1,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint="overlay",
        post_version_selections_fingerprint="selection",
        image_reference_extractor_version=1,
        completed_at="2026-07-15T00:00:00+00:00",
    )
    changed = ArchiveChangeState(
        archive_revision=snapshot.change_state.archive_revision + 1,
        floor_map_revision=snapshot.change_state.floor_map_revision,
    )

    with patch.object(
        store,
        "_read_current_archive_change_state",
        side_effect=[snapshot.change_state, changed],
    ):
        committed = store.commit_image_reference_state(state, ())

    assert not committed
    reread = store.read_backup_processing_snapshot()
    assert reread.image_state == state
    assert reread.image_state.processed_archive_revision != changed.archive_revision

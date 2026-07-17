from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nga_tools.backup import image_index, image_validation_store
from nga_tools.backup.archive_schema import ARCHIVE_SCHEMA_VERSION
from nga_tools.backup.archive_store import (
    PostImageReferenceCacheEntry,
    ThreadArchiveStore,
)
from nga_tools.backup.processing_state import ArchiveChangeState, ImageReferenceState
from nga_tools.storage import (
    STORAGE_LAYOUT_VERSION,
    UnsupportedStorageFormatError,
    ensure_storage_metadata,
    read_storage_metadata,
)


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
    assert "archive_pages" in data_tables
    assert "post_version_selections" in data_tables
    assert "page_snapshots" not in data_tables
    assert "post_observations" not in data_tables
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
        archive_schema_version = data_connection.execute(
            "PRAGMA user_version"
        ).fetchone()
        state_metadata = read_storage_metadata(state_connection)
        cache_metadata = read_storage_metadata(cache_connection)

    assert data_metadata is not None
    assert state_metadata is not None
    assert cache_metadata is not None
    assert data_metadata.role == "archive_data"
    assert state_metadata.role == "archive_state"
    assert cache_metadata.role == "archive_cache"
    assert data_metadata.layout_version == STORAGE_LAYOUT_VERSION
    assert archive_schema_version == (ARCHIVE_SCHEMA_VERSION,)
    assert state_metadata.source_store_id == data_metadata.store_id
    assert cache_metadata.source_store_id == data_metadata.store_id


def test_mismatched_state_store_is_rejected_without_mutation(
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

    with pytest.raises(UnsupportedStorageFormatError):
        store.read_backup_processing_snapshot()

    assert not list(store.thread_folder.glob("archive_state.sqlite3.corrupt-*"))
    with closing(sqlite3.connect(store.state_store.db_path)) as connection:
        metadata = read_storage_metadata(connection)
    assert metadata is not None
    assert metadata.source_store_id == "wrong"


def test_missing_state_index_is_rejected_without_recreation(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "123_456")
    store.ensure_schema()
    store.ensure_backup_processing_schema()
    with closing(sqlite3.connect(store.state_store.db_path)) as connection:
        connection.execute("DROP INDEX idx_image_reference_manifest_entries_url")
        connection.commit()

    reopened = ThreadArchiveStore(store.thread_folder)
    with pytest.raises(UnsupportedStorageFormatError):
        reopened.read_backup_processing_snapshot()

    with closing(sqlite3.connect(store.state_store.db_path)) as connection:
        index_exists = connection.execute(
            """
            SELECT 1 FROM sqlite_schema
            WHERE type = 'index'
              AND name = 'idx_image_reference_manifest_entries_url'
            """
        ).fetchone()
    assert index_exists is None


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


def test_data_only_handoff_reads_without_state_or_cache_databases(
    tmp_path: Path,
) -> None:
    source_output = tmp_path / "source"
    source_thread = source_output / "123_456"
    source_store = ThreadArchiveStore(source_thread)
    source_store.upsert_page(
        1,
        {
            "currentPage": 1,
            "totalPage": 1,
            "result": [{"lou": 1, "pid": 1001, "content": "portable body"}],
        },
        observed_at="2026-07-15T00:00:00+00:00",
    )
    source_store.ensure_backup_processing_schema()
    source_store.upsert_post_image_reference_cache(
        [
            PostImageReferenceCacheEntry(
                cache_key="drop-me",
                source_hash="source",
                extractor_version=1,
                references_json="[]",
            )
        ]
    )
    with closing(
        sqlite3.connect(source_output / image_index.IMAGE_INDEX_FILENAME)
    ) as connection:
        ensure_storage_metadata(connection, role="image_index")
        connection.execute(
            """
            CREATE TABLE image_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO image_mappings VALUES (?, ?)",
            ("https://img.nga.178.com/portable.png", "images_unique/image.png"),
        )
        connection.commit()
    target_output = tmp_path / "handoff"
    target_thread = target_output / "123_456"
    target_thread.mkdir(parents=True)
    shutil.copy2(source_store.db_path, target_thread / source_store.db_path.name)
    shutil.copy2(
        source_output / image_index.IMAGE_INDEX_FILENAME,
        target_output / image_index.IMAGE_INDEX_FILENAME,
    )

    target_store = ThreadArchiveStore(target_thread)
    target_post = target_store.read_effective_post_records()[0]["post"]
    assert target_post is not None
    assert target_post["content"] == "portable body"
    assert not target_store.state_store.db_path.exists()
    assert not target_store.cache_store.db_path.exists()
    assert target_store.read_post_image_reference_cache({"drop-me"}) == {}
    with patch(
        "nga_tools.config.get_config",
        return_value=SimpleNamespace(output_dir=str(target_output)),
    ):
        mappings = image_index.ImageIndexStore(target_output).mappings_by_url()
    assert mappings["https://img.nga.178.com/portable.png"].unique_rel_path == (
        "images_unique/image.png"
    )
    assert not (
        target_output / image_validation_store.IMAGE_CACHE_FILENAME
    ).exists()

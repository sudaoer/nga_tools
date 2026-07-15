from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.post_version_selection import selections_fingerprint
from nga_tools.storage import layout_migration, read_storage_metadata
from nga_tools.storage.layout_migration import migrate_layout, rollback_layout


_IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/15/layout-test.png"
)


def _make_legacy_thread(output_root: Path, name: str = "123_456") -> Path:
    thread_folder = output_root / name
    store = ThreadArchiveStore(thread_folder)
    store.upsert_page(
        1,
        {
            "currentPage": 1,
            "totalPage": 1,
            "result": [
                {
                    "lou": 1,
                    "pid": 1001,
                    "content": f"body [img]{_IMAGE_URL}[/img]",
                }
            ],
        },
        observed_at="2026-07-15T00:00:00+00:00",
    )
    overlays_hash = store.post_overlays_fingerprint()
    selections_hash = selections_fingerprint(thread_folder)
    with closing(sqlite3.connect(store.db_path)) as connection:
        revision_row = connection.execute(
            """
            SELECT archive_revision, floor_map_revision
            FROM archive_change_state WHERE singleton = 1
            """
        ).fetchone()
        assert revision_row is not None
        archive_revision, floor_map_revision = revision_row
        connection.executescript(
            """
            CREATE TABLE backup_floor_processing_state (
                singleton INTEGER PRIMARY KEY,
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL,
                processed_floor_map_revision INTEGER NOT NULL,
                page_count INTEGER NOT NULL,
                author_total_lou_count INTEGER,
                floor_map_format_version INTEGER NOT NULL,
                floor_map_generation_version INTEGER NOT NULL,
                floor_map_hash_algorithm TEXT NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE backup_image_reference_state (
                singleton INTEGER PRIMARY KEY,
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL,
                post_overlays_fingerprint TEXT NOT NULL,
                post_version_selections_fingerprint TEXT NOT NULL,
                image_reference_extractor_version INTEGER NOT NULL,
                completed_at TEXT NOT NULL
            );
            CREATE TABLE backup_image_reference_manifest_state (
                singleton INTEGER PRIMARY KEY,
                format_version INTEGER NOT NULL,
                processed_archive_revision INTEGER NOT NULL
            );
            CREATE TABLE backup_image_reference_manifest_posts (
                lou INTEGER PRIMARY KEY,
                cache_key TEXT NOT NULL
            );
            CREATE TABLE backup_image_reference_manifest_entries (
                lou INTEGER NOT NULL,
                image_index INTEGER NOT NULL,
                url TEXT NOT NULL,
                valid INTEGER NOT NULL,
                PRIMARY KEY(lou, image_index)
            );
            CREATE TABLE backup_image_reference_manifest_urls (
                url TEXT PRIMARY KEY,
                reference_count INTEGER NOT NULL,
                valid INTEGER NOT NULL
            );
            CREATE TABLE backup_pending_images (
                url TEXT PRIMARY KEY,
                last_attempt_at TEXT,
                failure_kind TEXT,
                http_status INTEGER
            );
            CREATE TABLE post_image_reference_cache (
                cache_key TEXT PRIMARY KEY,
                source_hash TEXT NOT NULL,
                extractor_version INTEGER NOT NULL,
                references_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE backup_image_references (url TEXT PRIMARY KEY);
            """
        )
        connection.execute(
            """
            INSERT INTO backup_floor_processing_state VALUES
            (1, 1, ?, ?, 1, NULL, 1, 1, 'sha256', ?)
            """,
            (
                archive_revision,
                floor_map_revision,
                "2026-07-15T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO backup_image_reference_state VALUES
            (1, 1, ?, ?, ?, 1, ?)
            """,
            (
                archive_revision,
                overlays_hash,
                selections_hash,
                "2026-07-15T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO backup_image_reference_manifest_state VALUES (1, 1, ?)",
            (archive_revision,),
        )
        connection.execute(
            "INSERT INTO backup_image_reference_manifest_posts VALUES (1, 'cache')"
        )
        connection.execute(
            "INSERT INTO backup_image_reference_manifest_entries VALUES (1, 1, ?, 1)",
            (_IMAGE_URL,),
        )
        connection.execute(
            "INSERT INTO backup_image_reference_manifest_urls VALUES (?, 1, 1)",
            (_IMAGE_URL,),
        )
        connection.execute(
            "INSERT INTO backup_pending_images VALUES (?, NULL, NULL, NULL)",
            ("https://example.invalid/retry.png",),
        )
        connection.execute(
            """
            INSERT INTO post_image_reference_cache VALUES
            ('cache', 'source', 1, '[]', ?, ?)
            """,
            (
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO backup_image_references VALUES ('legacy-poison')"
        )
        connection.execute("DROP TABLE storage_metadata")
        connection.commit()
    return thread_folder


def _table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as connection:
        return {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
            if isinstance(row[0], str)
        }


def test_layout_migration_splits_valid_state_cache_and_preserves_data(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    thread_folder = _make_legacy_thread(output_root)
    old_archive_size = (thread_folder / "archive.sqlite3").stat().st_size

    result = migrate_layout(output_root, [thread_folder])

    assert result.migrated_count == 1
    assert result.failures == ()
    store = ThreadArchiveStore(thread_folder)
    assert store.read_effective_post_records()[0]["post"] is not None
    snapshot = store.read_backup_processing_snapshot()
    assert snapshot.floor_state is not None
    assert snapshot.image_state is not None
    assert len(snapshot.pending_image_retries) == 1
    manifest = store.read_image_reference_manifest()
    assert manifest is not None
    assert len(manifest.posts) == 1
    assert "cache" in store.read_post_image_reference_cache({"cache"})

    archive_tables = _table_names(store.db_path)
    assert "backup_image_references" not in archive_tables
    assert "backup_image_reference_state" not in archive_tables
    assert "post_image_reference_cache" not in archive_tables
    assert store.db_path.stat().st_size < old_archive_size
    with closing(sqlite3.connect(store.db_path)) as connection:
        metadata = read_storage_metadata(connection)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
    assert metadata is not None and metadata.role == "archive_data"

    run_root = output_root / ".migration-backups" / result.run_id
    rollback_archive = run_root / "files" / thread_folder.name / "archive.sqlite3"
    assert rollback_archive.is_file()
    with closing(sqlite3.connect(rollback_archive)) as connection:
        assert connection.execute(
            "SELECT url FROM backup_image_references"
        ).fetchone() == ("legacy-poison",)
    manifest_data = json.loads(
        (run_root / "manifest.json").read_text(encoding="utf-8")
    )
    assert manifest_data["status"] == "completed"
    assert manifest_data["entries"][thread_folder.name]["durable_fingerprints"]


def test_layout_migration_resumes_failed_run_and_rollback_restores_old_layout(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    thread_folder = _make_legacy_thread(output_root)
    original_drop = layout_migration._drop_legacy_archive_tables

    with patch.object(
        layout_migration,
        "_drop_legacy_archive_tables",
        side_effect=RuntimeError("interrupted"),
    ):
        failed = migrate_layout(output_root, [thread_folder])
    assert len(failed.failures) == 1

    with patch.object(
        layout_migration,
        "_drop_legacy_archive_tables",
        wraps=original_drop,
    ):
        resumed = migrate_layout(output_root, [thread_folder])
    assert resumed.run_id == failed.run_id
    assert resumed.migrated_count == 1
    assert resumed.failures == ()

    rollback = rollback_layout(output_root, resumed.run_id)
    assert rollback.restored_count == 1
    assert not (thread_folder / "archive_state.sqlite3").exists()
    assert not (thread_folder / "archive_cache.sqlite3").exists()
    with closing(sqlite3.connect(thread_folder / "archive.sqlite3")) as connection:
        assert read_storage_metadata(connection) is None
        assert connection.execute(
            "SELECT url FROM backup_image_references"
        ).fetchone() == ("legacy-poison",)


def test_layout_migration_skips_invalid_cache_rows(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    thread_folder = _make_legacy_thread(output_root)
    with closing(sqlite3.connect(thread_folder / "archive.sqlite3")) as connection:
        connection.execute(
            "UPDATE post_image_reference_cache SET references_json = '{broken'"
        )
        connection.commit()

    result = migrate_layout(output_root, [thread_folder])

    assert result.failures == ()
    store = ThreadArchiveStore(thread_folder)
    assert store.read_post_image_reference_cache({"cache"}) == {}
    assert "post_image_reference_cache" not in _table_names(store.db_path)

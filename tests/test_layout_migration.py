from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nga_tools.backup.archive_store import (
    PostImageReferenceCacheEntry,
    ThreadArchiveStore,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
)
from nga_tools.backup.image_reference_cache import (
    IMAGE_REFERENCE_EXTRACTOR_VERSION,
)
from nga_tools.backup.image_store import image_mappings_by_url
from nga_tools.backup.processing_state import FloorProcessingState
from nga_tools.forum.ankebak_state import AnkebakStateStore
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
    selections_hash = store.post_version_selections_fingerprint()
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
            (1, 1, ?, ?, ?, ?, ?)
            """,
            (
                archive_revision,
                overlays_hash,
                selections_hash,
                IMAGE_REFERENCE_EXTRACTOR_VERSION,
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
            ('cache', 'source', ?, '[]', ?, ?)
            """,
            (
                IMAGE_REFERENCE_EXTRACTOR_VERSION,
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
            ),
        )
        connection.execute(
            "INSERT INTO backup_image_references VALUES ('legacy-poison')"
        )
        connection.executescript(
            """
            DROP TABLE archive_pages;
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
            );
            INSERT INTO page_snapshots (
                page_number, response_hash, page_json, current_page,
                total_page, vrows, first_seen_at, last_seen_at, seen_count
            ) VALUES
                (1, 'old', '{"snapshot":"old"}', 1, 3, 30,
                 '2026-07-14T00:00:00+00:00',
                 '2026-07-14T00:00:00+00:00', 1),
                (1, 'new', '{"snapshot":"new"}', 1, 1, NULL,
                 '2026-07-15T00:00:00+00:00',
                 '2026-07-15T00:00:00+00:00', 1);
            CREATE TABLE post_observations (
                page_snapshot_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                post_version_id INTEGER NOT NULL,
                PRIMARY KEY(page_snapshot_id, position)
            );
            INSERT INTO post_observations VALUES (2, 0, 1001, 1, 1);
            PRAGMA user_version = 0;
            """
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


def _make_legacy_globals(output_root: Path) -> tuple[Path, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    forum_path = output_root / "forum_threads.sqlite3"
    with closing(sqlite3.connect(forum_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE forum_threads_fid_784 (
                tid INTEGER PRIMARY KEY,
                aid INTEGER NOT NULL,
                author TEXT NOT NULL,
                subject TEXT NOT NULL,
                postdate INTEGER NOT NULL,
                postdate_text TEXT NOT NULL,
                lastpost INTEGER NOT NULL,
                lastpost_text TEXT NOT NULL,
                replies INTEGER NOT NULL
            );
            CREATE TABLE ankebak_thread_state (
                target_key TEXT PRIMARY KEY,
                tid INTEGER NOT NULL,
                aid INTEGER,
                forum_replies INTEGER,
                forum_lastpost INTEGER,
                last_backup_success_at TEXT NOT NULL,
                last_full_backup_success_at TEXT
            );
            INSERT INTO forum_threads_fid_784 VALUES
            (123, 456, 'author', 'subject', 1, 'one', 2, 'two', 3);
            INSERT INTO ankebak_thread_state VALUES
            ('123:456', 123, 456, 3, 2,
             '2026-07-15T00:00:00+00:00',
             '2026-07-15T00:00:00+00:00');
            """
        )

    image_file = output_root / "images_unique" / "abc.png"
    image_file.parent.mkdir()
    image_file.write_bytes(b"image-bytes")
    image_stat = image_file.stat()
    image_index_path = output_root / "image_index.sqlite3"
    with closing(sqlite3.connect(image_index_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE image_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE image_validation_cache (
                canonical_path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            INSERT INTO image_mappings VALUES
            ('https://img.nga.178.com/attachments/mon_202607/15/abc.png',
             'images_unique/abc.png', 'created', 'updated');
            """
        )
        connection.execute(
            "INSERT INTO image_validation_cache VALUES (?, ?, ?, 1, 'updated')",
            (str(image_file.resolve()), image_stat.st_size, image_stat.st_mtime_ns),
        )
        connection.commit()
    return forum_path, image_index_path


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
    assert "archive_pages" in archive_tables
    assert "page_snapshots" not in archive_tables
    assert "post_observations" not in archive_tables
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
    assert manifest_data["archive_schema_version"] == 1
    thread_entry = manifest_data["entries"][thread_folder.name]
    assert thread_entry["durable_fingerprints"]
    assert thread_entry["stats"]["page_snapshot_rows_removed"] == 2
    assert thread_entry["stats"]["post_observation_rows_removed"] == 1
    assert thread_entry["stats"]["archive_page_rows"] == 1
    with closing(sqlite3.connect(store.db_path)) as connection:
        assert connection.execute(
            "SELECT page_number, total_page, vrows FROM archive_pages"
        ).fetchall() == [(1, 1, None)]
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)


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


def test_archive_schema_migration_preserves_split_state_and_cache(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    thread_folder = output_root / "123_456"
    store = ThreadArchiveStore(thread_folder)
    store.upsert_page(
        1,
        {"totalPage": 1, "vrows": 4, "result": []},
        observed_at="2026-07-15T00:00:00+00:00",
    )
    snapshot = store.read_backup_processing_snapshot()
    floor_state = FloorProcessingState(
        format_version=1,
        processed_archive_revision=snapshot.change_state.archive_revision,
        processed_floor_map_revision=snapshot.change_state.floor_map_revision,
        page_count=1,
        author_total_lou_count=4,
        floor_map_format_version=FLOOR_MAP_VERSION,
        floor_map_generation_version=FLOOR_MAP_GENERATION_VERSION,
        floor_map_hash_algorithm=FLOOR_MAP_HASH_ALGORITHM,
        completed_at="2026-07-15T00:00:00+00:00",
    )
    assert store.commit_floor_processing_state(floor_state)
    cache_entry = PostImageReferenceCacheEntry(
        cache_key="cache-key",
        source_hash="source-hash",
        extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        references_json="[]",
    )
    store.upsert_post_image_reference_cache([cache_entry])

    with closing(sqlite3.connect(store.db_path)) as connection:
        page_row = connection.execute(
            """
            SELECT page_number, total_page, vrows, last_seen_at
            FROM archive_pages
            """
        ).fetchone()
        assert page_row is not None
        connection.executescript(
            """
            DROP TABLE archive_pages;
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
            );
            PRAGMA user_version = 0;
            """
        )
        connection.execute(
            """
            INSERT INTO page_snapshots (
                page_number, response_hash, page_json, total_page, vrows,
                first_seen_at, last_seen_at, seen_count
            ) VALUES (?, 'snapshot', '{}', ?, ?, ?, ?, 1)
            """,
            (
                page_row[0],
                page_row[1],
                page_row[2],
                page_row[3],
                page_row[3],
            ),
        )
        connection.commit()

    result = migrate_layout(output_root, [thread_folder])

    assert result.failures == ()
    migrated_store = ThreadArchiveStore(thread_folder)
    assert (
        migrated_store.read_backup_processing_snapshot().floor_state
        == floor_state
    )
    assert migrated_store.read_post_image_reference_cache({"cache-key"}) == {
        "cache-key": cache_entry
    }
    pagination = migrated_store.read_latest_page_one_pagination()
    assert pagination is not None
    assert pagination.page_count == 1
    assert pagination.vrows == 4


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


def test_layout_migration_splits_global_data_state_and_cache(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    forum_path, image_index_path = _make_legacy_globals(output_root)

    result = migrate_layout(output_root, [], include_global=True)

    assert result.migrated_count == 1
    assert result.failures == ()
    assert "ankebak_thread_state" not in _table_names(forum_path)
    assert "image_validation_cache" not in _table_names(image_index_path)
    with closing(sqlite3.connect(image_index_path)) as connection:
        assert [
            row[1] for row in connection.execute(
                "PRAGMA table_info(image_mappings)"
            )
        ] == ["url", "unique_rel_path"]
    state_store = AnkebakStateStore(output_root / "backup_state.sqlite3")
    assert state_store.load_states()["123:456"].tid == 123
    with closing(sqlite3.connect(output_root / "image_cache.sqlite3")) as connection:
        rows = connection.execute(
            """
            SELECT relative_path, size, valid
            FROM image_validation_cache
            """
        ).fetchall()
    assert rows == [("images_unique/abc.png", 11, 1)]
    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_root)),
    ):
        mappings = image_mappings_by_url()
    assert list(mappings) == [
        "https://img.nga.178.com/attachments/mon_202607/15/abc.png"
    ]

    rollback_layout(output_root, result.run_id)
    assert not (output_root / "backup_state.sqlite3").exists()
    assert not (output_root / "image_cache.sqlite3").exists()
    assert "ankebak_thread_state" in _table_names(forum_path)
    assert "image_validation_cache" in _table_names(image_index_path)

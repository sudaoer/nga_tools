from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.content_codec import decode_content
from nga_tools.storage.content_migration import (
    migrate_content,
    rollback_content,
)


def _make_legacy_archive(thread_folder: Path) -> tuple[int, int]:
    store = ThreadArchiveStore(thread_folder)
    store.upsert_page(
        1,
        {
            "totalPage": 1,
            "result": [
                {"lou": 1, "pid": 1001, "content": "before edit"},
            ],
        },
        observed_at="2026-07-17T00:00:00+00:00",
    )
    store.upsert_page(
        1,
        {
            "totalPage": 1,
            "result": [
                {"lou": 1, "pid": 1001, "content": "after edit"},
            ],
        },
        observed_at="2026-07-17T01:00:00+00:00",
    )
    with closing(sqlite3.connect(thread_folder / "archive.sqlite3")) as connection:
        rows = connection.execute(
            """
            SELECT
                id,
                pid,
                lou,
                source_hash,
                content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at,
                last_seen_at,
                seen_count
            FROM post_versions
            ORDER BY id
            """
        ).fetchall()
        decoded_rows = [
            (*row[:4], decode_content(row[4]), *row[5:])
            for row in rows
        ]
        old_version_id = rows[0][0]
        latest_version_id = rows[1][0]
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE post_versions")
        connection.execute(
            """
            CREATE TABLE post_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid INTEGER NOT NULL,
                lou INTEGER NOT NULL,
                source_hash TEXT NOT NULL,
                content TEXT NOT NULL,
                word_count_version INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_chars INTEGER NOT NULL DEFAULT 0,
                word_count_chinese_with_punctuation INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                seen_count INTEGER NOT NULL,
                UNIQUE(pid, lou, source_hash)
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO post_versions (
                id,
                pid,
                lou,
                source_hash,
                content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            decoded_rows,
        )
        connection.execute(
            """
            CREATE INDEX idx_post_versions_latest_covering
            ON post_versions(lou, last_seen_at DESC, id DESC, pid)
            """
        )
        connection.execute(
            """
            INSERT INTO post_version_selections (lou, version_id, selected_at)
            VALUES (1, ?, ?)
            """,
            (old_version_id, "2026-07-17T02:00:00+00:00"),
        )
        connection.commit()
    return old_version_id, latest_version_id


def test_content_migration_preserves_ids_and_selection(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    thread_folder = output_dir / "101_201"
    old_version_id, latest_version_id = _make_legacy_archive(thread_folder)

    result = migrate_content(output_dir, [thread_folder])

    assert result.migrated_count == 1
    assert result.failures == ()
    assert result.run_id is not None
    with closing(sqlite3.connect(thread_folder / "archive.sqlite3")) as connection:
        content_type, raw_content = connection.execute(
            "SELECT typeof(content), content FROM post_versions WHERE id = ?",
            (old_version_id,),
        ).fetchone()
        assert content_type == "blob"
        assert decode_content(raw_content) == "before edit"
        assert connection.execute(
            "SELECT id FROM post_versions WHERE id = ?",
            (latest_version_id,),
        ).fetchone() == (latest_version_id,)
        assert connection.execute(
            "SELECT lou, version_id FROM post_version_selections"
        ).fetchone() == (1, old_version_id)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    rollback = rollback_content(output_dir, result.run_id)

    assert rollback.restored_count == 1
    with closing(sqlite3.connect(thread_folder / "archive.sqlite3")) as connection:
        assert connection.execute(
            "SELECT typeof(content), content FROM post_versions WHERE id = ?",
            (old_version_id,),
        ).fetchone() == ("text", "before edit")


def test_content_migration_dry_run_does_not_modify_archive(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    thread_folder = output_dir / "101_201"
    _make_legacy_archive(thread_folder)
    before = (thread_folder / "archive.sqlite3").read_bytes()

    result = migrate_content(output_dir, [thread_folder], dry_run=True)

    assert result.run_id is None
    assert result.migrated_count == 0
    assert result.skipped_count == 0
    assert result.failures == ()
    assert result.stats[0].raw_content_bytes == len(
        "before edit".encode("utf-8")
    ) + len("after edit".encode("utf-8"))
    assert result.stats[0].compressed_content_bytes > 0
    assert (thread_folder / "archive.sqlite3").read_bytes() == before

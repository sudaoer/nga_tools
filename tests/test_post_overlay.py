from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records,
    make_post_overlay,
    post_overlay_from_storage,
    render_overlay_html,
    source_hashes_by_lou_with_post_overlays,
)
from nga_tools.core.hashing import hash_text


def test_save_load_and_apply_post_overlay(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    store = ThreadArchiveStore(thread_dir)
    store.ensure_schema()
    before_fingerprint = store.post_overlays_fingerprint()

    overlay = store.upsert_post_overlay(
        1,
        make_post_overlay("[quote]覆盖[/quote]"),
    )
    records: list[PostRecord] = [
        {
            "lou": 1,
            "pid": 1001,
            "post": {"lou": 1, "pid": 1001, "content": "原文", "image_attachments": []},
            "html": None,
            "source_hash": "original-hash",
        },
        {
            "lou": 2,
            "pid": 1002,
            "post": {"lou": 2, "pid": 1002, "content": "其他", "image_attachments": []},
            "html": None,
            "source_hash": "other-hash",
        },
    ]

    loaded = store.read_post_overlays()
    applied_records = apply_post_overlays_to_records(loaded, records)
    source_hashes = source_hashes_by_lou_with_post_overlays(loaded, records)

    assert loaded[1] == overlay
    assert store.post_overlays_fingerprint() != before_fingerprint
    assert applied_records[0]["post"] is None
    assert '<blockquote class="nga-quote">覆盖</blockquote>' in applied_records[0]["html"]
    assert applied_records[1] == records[1]
    assert source_hashes[1] != "original-hash"
    assert source_hashes[2] == "other-hash"

    assert store.delete_post_overlay(1) is True
    assert store.delete_post_overlay(1) is False
    assert store.read_post_overlays() == {}


def test_old_archive_without_overlay_table_ignores_legacy_json(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    store = ThreadArchiveStore(thread_dir)
    sqlite3.connect(store.db_path).close()
    legacy_path = thread_dir / "post_overlays.json"
    legacy_text = '{"version":1,"overlays":{"1":{"bbcode":"legacy"}}}\n'
    legacy_path.write_text(legacy_text, encoding="utf-8")

    assert store.read_post_overlays() == {}

    overlay = store.upsert_post_overlay(1, make_post_overlay("database"))

    assert store.read_post_overlays() == {1: overlay}
    assert legacy_path.read_text(encoding="utf-8") == legacy_text


def test_invalid_stored_overlay_is_rejected(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    store = ThreadArchiveStore(thread_dir)
    store.ensure_schema()
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO post_overlays (
                lou, mode, bbcode, content_hash, updated_at
            )
            VALUES (
                1,
                'replace',
                'content',
                'wrong-hash',
                '2026-07-12T00:00:00+00:00'
            )
            """
        )

    with pytest.raises(ValueError, match="content_hash"):
        store.read_post_overlays()

    with pytest.raises(ValueError, match="ISO时间"):
        post_overlay_from_storage(
            mode="replace",
            bbcode="content",
            content_hash=hash_text("content"),
            updated_at="not-a-timestamp",
        )


def test_overlay_rendering_sanitizes_html_and_rejects_external_media() -> None:
    html = render_overlay_html('<script>alert(1)</script>[b]safe[/b]')

    assert "<script" not in html.lower()
    assert "<strong>safe</strong>" in html
    with pytest.raises(ValueError, match="图片或媒体外链"):
        render_overlay_html("[img]https://example.test/a.png[/img]")
    with pytest.raises(ValueError, match="图片或媒体外链"):
        render_overlay_html('<img src="https://example.test/a.png" />')

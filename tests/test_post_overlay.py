from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from PIL import Image

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records,
    make_post_overlay,
    make_existing_overlay_image_src_resolver,
    post_overlay_from_storage,
    render_overlay_html,
    source_hashes_by_lou_with_post_overlays,
)
from nga_tools.core.hashing import hash_text
from nga_tools.storage import UnsupportedStorageFormatError, ensure_storage_metadata


def _write_existing_image(output_dir: Path, image_url: str) -> Path:
    image_path = output_dir / "images_unique" / "existing.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (2, 2), color="white").save(image_path)
    with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
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
            """
            INSERT INTO image_mappings (url, unique_rel_path)
            VALUES (?, ?)
            """,
            (
                image_url,
                "images_unique/existing.png",
            ),
        )
    return image_path


def test_save_load_and_apply_post_overlay(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    store = ThreadArchiveStore(thread_dir)
    store.ensure_schema()
    before_fingerprint = store.overlays.post_overlays_fingerprint()

    overlay = store.overlays.upsert_post_overlay(
        1,
        make_post_overlay("[quote]覆盖[/quote]"),
    )
    records: list[PostRecord] = [
        {
            "lou": 1,
            "pid": 1001,
            "post": {"lou": 1, "pid": 1001, "content": "原文"},
            "html": None,
            "source_hash": "original-hash",
        },
        {
            "lou": 2,
            "pid": 1002,
            "post": {"lou": 2, "pid": 1002, "content": "其他"},
            "html": None,
            "source_hash": "other-hash",
        },
    ]

    loaded = store.overlays.read_post_overlays()
    applied_records = apply_post_overlays_to_records(loaded, records)
    source_hashes = source_hashes_by_lou_with_post_overlays(loaded, records)

    assert loaded[1] == overlay
    assert store.overlays.post_overlays_fingerprint() != before_fingerprint
    assert applied_records[0]["post"] is None
    assert '<blockquote class="nga-quote">覆盖</blockquote>' in applied_records[0]["html"]
    assert applied_records[1] == records[1]
    assert source_hashes[1] != "original-hash"
    assert source_hashes[2] == "other-hash"

    assert store.overlays.delete_post_overlay(1) is True
    assert store.overlays.delete_post_overlay(1) is False
    assert store.overlays.read_post_overlays() == {}


def test_empty_post_overlay_is_stored_and_replaces_the_floor(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    store = ThreadArchiveStore(thread_dir)
    overlay = store.overlays.upsert_post_overlay(1, make_post_overlay(""))
    records: list[PostRecord] = [
        {
            "lou": 1,
            "pid": 1001,
            "post": {
                "lou": 1,
                "pid": 1001,
                "content": "原文",
            },
            "html": None,
            "source_hash": "original-hash",
        }
    ]

    applied = apply_post_overlays_to_records({1: overlay}, records)

    assert store.overlays.read_post_overlays() == {1: overlay}
    assert overlay["bbcode"] == ""
    assert overlay["content_hash"] == hash_text("")
    assert applied[0]["html"] == ""
    assert applied[0]["source_hash"] != records[0]["source_hash"]


def test_old_overlay_constraint_is_rejected_without_mutation(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    store = ThreadArchiveStore(thread_dir)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            CREATE TABLE post_overlays (
                lou INTEGER PRIMARY KEY CHECK(lou >= 0),
                mode TEXT NOT NULL CHECK(mode = 'replace'),
                bbcode TEXT NOT NULL CHECK(length(trim(bbcode)) > 0),
                content_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO post_overlays (
                lou, mode, bbcode, content_hash, updated_at
            )
            VALUES (1, 'replace', 'existing', ?, '2026-07-12T00:00:00+00:00')
            """,
            (hash_text("existing"),),
        )

    with pytest.raises(UnsupportedStorageFormatError):
        store.ensure_schema()
    with sqlite3.connect(store.db_path) as connection:
        schema_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name = 'post_overlays'"
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT lou, bbcode FROM post_overlays"
        ).fetchall()
    assert "length(trim(bbcode))" in schema_sql
    assert rows == [(1, "existing")]


def test_old_archive_without_overlay_table_ignores_legacy_json(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    store = ThreadArchiveStore(thread_dir)
    store.ensure_schema()
    legacy_path = thread_dir / "post_overlays.json"
    legacy_text = '{"version":1,"overlays":{"1":{"bbcode":"legacy"}}}\n'
    legacy_path.write_text(legacy_text, encoding="utf-8")

    assert store.overlays.read_post_overlays() == {}

    overlay = store.overlays.upsert_post_overlay(1, make_post_overlay("database"))

    assert store.overlays.read_post_overlays() == {1: overlay}
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
        store.overlays.read_post_overlays()

    with pytest.raises(ValueError, match="ISO时间"):
        post_overlay_from_storage(
            mode="replace",
            bbcode="content",
            content_hash=hash_text("content"),
            updated_at="not-a-timestamp",
        )


def test_overlay_rendering_sanitizes_html_and_supports_existing_nga_images(
    tmp_path: Path,
) -> None:
    image_url = (
        "https://img.nga.178.com/attachments/"
        "mon_202607/12/existing.png"
    )
    output_dir = tmp_path / "output"
    _write_existing_image(output_dir, image_url)
    html = render_overlay_html('<script>alert(1)</script>[b]safe[/b]')
    image_bbcode = f"[img]{image_url}[/img]"
    resolver = make_existing_overlay_image_src_resolver(
        image_bbcode,
        output_dir,
        require_all=True,
    )
    image_html = render_overlay_html(
        image_bbcode,
        image_src_resolver=resolver,
    )

    assert "<script" not in html.lower()
    assert "<strong>safe</strong>" in html
    assert f'src="{image_url}"' in image_html
    assert "<img" not in render_overlay_html(image_bbcode)
    assert image_bbcode in render_overlay_html(image_bbcode)
    with pytest.raises(ValueError, match="完整的NGA图片URL"):
        make_existing_overlay_image_src_resolver(
            "[img]https://example.test/a.png[/img]",
            output_dir,
            require_all=True,
        )
    with pytest.raises(ValueError, match=r"只支持\[img\]"):
        render_overlay_html('<img src="https://example.test/a.png" />')
    with pytest.raises(ValueError, match=r"只支持\[img\]"):
        render_overlay_html(f"[img={image_url}]caption[/img]")
    with pytest.raises(ValueError, match=r"\[flash\]"):
        render_overlay_html("[flash]https://example.test/a.swf[/flash]")


def test_overlay_rendering_decodes_double_encoded_numeric_entities() -> None:
    html = render_overlay_html("before&amp;#160;after")

    assert html == "before&nbsp;after"


def test_overlay_rejects_unmapped_or_invalid_local_image(tmp_path: Path) -> None:
    mapped_url = (
        "https://img.nga.178.com/attachments/"
        "mon_202607/12/corrupt.png"
    )
    missing_url = (
        "https://img.nga.178.com/attachments/"
        "mon_202607/12/missing.png"
    )
    output_dir = tmp_path / "output"
    image_path = _write_existing_image(output_dir, mapped_url)
    image_path.write_bytes(b"not an image")
    loose_resolver = make_existing_overlay_image_src_resolver(
        f"[img]{mapped_url}[/img]",
        output_dir,
    )

    assert "<img" not in render_overlay_html(
        f"[img]{mapped_url}[/img]",
        image_src_resolver=loose_resolver,
    )
    with pytest.raises(ValueError, match="本地文件无效"):
        make_existing_overlay_image_src_resolver(
            f"[img]{mapped_url}[/img]",
            output_dir,
            require_all=True,
        )
    with pytest.raises(ValueError, match="尚未下载"):
        make_existing_overlay_image_src_resolver(
            f"[img]{missing_url}[/img]",
            output_dir,
            require_all=True,
        )

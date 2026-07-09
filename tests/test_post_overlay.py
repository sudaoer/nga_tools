from __future__ import annotations

from pathlib import Path

import pytest

from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import (
    POST_OVERLAYS_FILENAME,
    apply_post_overlays_to_records,
    load_post_overlays,
    post_overlays_fingerprint,
    render_overlay_html,
    save_post_overlay,
    source_hashes_by_lou_with_post_overlays,
)


def test_save_load_and_apply_post_overlay(tmp_path: Path) -> None:
    thread_dir = tmp_path / "101_201"
    thread_dir.mkdir()
    before_fingerprint = post_overlays_fingerprint(thread_dir)

    overlay = save_post_overlay(thread_dir, 1, "[quote]覆盖[/quote]")
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

    loaded = load_post_overlays(thread_dir)
    applied_records = apply_post_overlays_to_records(thread_dir, records)
    source_hashes = source_hashes_by_lou_with_post_overlays(thread_dir, records)

    assert (thread_dir / POST_OVERLAYS_FILENAME).is_file()
    assert loaded[1] == overlay
    assert post_overlays_fingerprint(thread_dir) != before_fingerprint
    assert applied_records[0]["post"] is None
    assert '<blockquote class="nga-quote">覆盖</blockquote>' in applied_records[0]["html"]
    assert applied_records[1] == records[1]
    assert source_hashes[1] != "original-hash"
    assert source_hashes[2] == "other-hash"


def test_overlay_rendering_sanitizes_html_and_rejects_external_media() -> None:
    html = render_overlay_html('<script>alert(1)</script>[b]safe[/b]')

    assert "<script" not in html.lower()
    assert "<strong>safe</strong>" in html
    with pytest.raises(ValueError, match="图片或媒体外链"):
        render_overlay_html("[img]https://example.test/a.png[/img]")
    with pytest.raises(ValueError, match="图片或媒体外链"):
        render_overlay_html('<img src="https://example.test/a.png" />')

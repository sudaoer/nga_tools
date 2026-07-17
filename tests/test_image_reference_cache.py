from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.image_pipeline import parse_post_htmls_for_images
from nga_tools.backup.image_reference_cache import (
    collect_image_download_tasks_for_records,
    deserialize_image_references,
    image_reference_cache_key,
)
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records,
    make_post_overlay,
)
from nga_tools.console import use_warning_log
from nga_tools.core.hashing import hash_text


def _image_url(name: str) -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202607/11/{name}.png"
    )


def _post_record(
    content: str,
    *,
    source_hash: str | None = None,
) -> PostRecord:
    return {
        "lou": 1,
        "pid": 1001,
        "post": {
            "lou": 1,
            "pid": 1001,
            "content": content,
        },
        "html": None,
        "source_hash": source_hash or hash_text(content),
    }


def test_unchanged_records_reuse_cached_references_without_parsing(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "thread")
    store.ensure_schema()
    image_url = _image_url("unchanged")

    with patch(
        "nga_tools.backup.image_reference_cache.parse_post_htmls_for_images",
        wraps=parse_post_htmls_for_images,
    ) as parser_mock:
        first = collect_image_download_tasks_for_records(
            store,
            [_post_record(f"[img]{image_url}[/img]")],
            FloorLabels.plain(),
        )
        second = collect_image_download_tasks_for_records(
            store,
            [_post_record(f"[img]{image_url}[/img]")],
            FloorLabels.plain(),
        )

    assert first.tasks == [{"url": image_url}]
    assert first.manifest_posts[0].lou == 1
    assert first.manifest_posts[0].cache_key == image_reference_cache_key(
        _post_record(f"[img]{image_url}[/img]")
    )
    assert first.manifest_posts[0].references[0].url == image_url
    assert first.cache_hit_count == 0
    assert first.cache_miss_lous == frozenset({1})
    assert second.tasks == [{"url": image_url}]
    assert second.cache_hit_count == 1
    assert second.cache_miss_lous == frozenset()
    assert parser_mock.call_count == 1


def test_cached_invalid_reference_reemits_same_warning(tmp_path: Path) -> None:
    store = ThreadArchiveStore(tmp_path / "thread")
    store.ensure_schema()
    invalid_html = '<img src="./broken.png" />'
    record: PostRecord = {
        "lou": 1,
        "pid": 1001,
        "post": None,
        "html": invalid_html,
        "source_hash": hash_text(invalid_html),
    }
    first_log = tmp_path / "first-warning.log"
    second_log = tmp_path / "second-warning.log"

    with use_warning_log(first_log):
        first = collect_image_download_tasks_for_records(
            store,
            [record],
            FloorLabels.plain(),
        )
    with use_warning_log(second_log):
        second = collect_image_download_tasks_for_records(
            store,
            [record],
            FloorLabels.plain(),
        )

    assert first.tasks == []
    assert second.cache_hit_count == 1
    assert first_log.read_text(encoding="utf-8") == second_log.read_text(
        encoding="utf-8"
    )
    assert "第1楼的第1张图片链接无效：./broken.png" in second_log.read_text(
        encoding="utf-8"
    )


def test_corrupt_cached_json_is_reparsed_and_overwritten(tmp_path: Path) -> None:
    store = ThreadArchiveStore(tmp_path / "thread")
    store.ensure_schema()
    image_url = _image_url("corrupt-cache")
    record = _post_record(f"[img]{image_url}[/img]")
    collect_image_download_tasks_for_records(
        store,
        [record],
        FloorLabels.plain(),
    )
    cache_key = image_reference_cache_key(record)
    with closing(sqlite3.connect(store.cache.db_path)) as connection:
        connection.execute(
            """
            UPDATE post_image_reference_cache
            SET references_json = ?
            WHERE cache_key = ?
            """,
            ("{broken", cache_key),
        )
        connection.commit()

    warning_path = tmp_path / "corrupt-warning.log"
    with (
        patch(
            "nga_tools.backup.image_reference_cache.parse_post_htmls_for_images",
            wraps=parse_post_htmls_for_images,
        ) as parser_mock,
        use_warning_log(warning_path),
    ):
        result = collect_image_download_tasks_for_records(
            store,
            [record],
            FloorLabels.plain(),
        )

    cached_entry = store.cache.read_post_image_reference_cache({cache_key})[cache_key]
    references = deserialize_image_references(cached_entry.references_json)
    assert result.tasks == [{"url": image_url}]
    assert result.cache_miss_count == 1
    assert parser_mock.call_count == 1
    assert references[0].url == image_url
    assert "图片引用缓存损坏，重新解析并覆盖" in warning_path.read_text(
        encoding="utf-8"
    )


def test_overlay_add_change_and_remove_select_distinct_cache_identity(
    tmp_path: Path,
) -> None:
    thread_folder = tmp_path / "thread"
    store = ThreadArchiveStore(thread_folder)
    record = _post_record("original")
    original_key = image_reference_cache_key(record)

    store.upsert_post_overlay(1, make_post_overlay("first overlay"))
    first_overlay_record = apply_post_overlays_to_records(
        store.read_post_overlays(),
        [record],
    )[0]
    first_overlay_key = image_reference_cache_key(first_overlay_record)
    store.upsert_post_overlay(1, make_post_overlay("second overlay"))
    second_overlay_record = apply_post_overlays_to_records(
        store.read_post_overlays(),
        [record],
    )[0]
    second_overlay_key = image_reference_cache_key(second_overlay_record)
    store.delete_post_overlay(1)
    restored_record = apply_post_overlays_to_records(
        store.read_post_overlays(),
        [record],
    )[0]

    assert first_overlay_key != original_key
    assert second_overlay_key != first_overlay_key
    assert image_reference_cache_key(restored_record) == original_key


def test_effective_source_hash_change_invalidates_cache_identity() -> None:
    first = _post_record("same", source_hash="selected-version-one")
    second = _post_record("same", source_hash="selected-version-two")

    assert image_reference_cache_key(first) != image_reference_cache_key(second)

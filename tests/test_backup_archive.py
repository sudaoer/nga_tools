from __future__ import annotations

import io
import json
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from nga_tools.backup.archive import backup_thread, backup_thread_sub
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels, FloorMapBuildResult
from nga_tools.backup.floor_models import RecoveredMissingPost
from nga_tools.backup.image_pipeline import (
    collect_image_download_tasks,
    collect_image_download_tasks_from_parsed,
    parse_post_htmls_for_images,
)
from nga_tools.backup.models import ParsedPostHtml, PostHtml
from nga_tools.backup.page_store import fetch_backup_page
from nga_tools.backup.post_html import (
    build_post_htmls,
    find_missing_lou,
    merge_missing_lou,
)
from nga_tools.ngaclient.client import NGAPageError
from nga_tools.timing import use_timing_log


class MutableFakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = [
            {"lou": 1, "pid": 1001, "content": "first"},
            {"lou": 2, "pid": 1002, "content": "second"},
        ]
        self.vrows: int | None = 3

    def get_page_count(self, tid: int, aid: int | None) -> int:
        del tid, aid
        return 1

    def get_page(
        self,
        tid: int,
        aid: int | None,
        page: int,
    ) -> dict[str, object]:
        del tid, page
        data: dict[str, object] = {
            "currentPage": 1,
            "totalPage": 1,
            "result": [dict(post) for post in self.posts],
        }
        if aid is not None and self.vrows is not None:
            data["vrows"] = self.vrows
        return data


def _fake_get_folder(thread_dir: Path) -> Callable[..., str]:
    def get_folder(
        tid: int,
        aid: int | None,
        subfolder: str | None = None,
        *,
        create: bool = True,
    ) -> str:
        del tid, aid
        path = thread_dir if subfolder is None else thread_dir / subfolder
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return str(path)

    return get_folder


def _run_backup(
    thread_dir: Path,
    client: object,
    *,
    mode: str = "sub",
    write_json: bool = False,
    floor_map_result: FloorMapBuildResult | None = None,
    parsed_lous: list[list[int]] | None = None,
    downloaded_urls: list[list[str]] | None = None,
) -> None:
    floor_map_result = floor_map_result or FloorMapBuildResult(
        FloorLabels.plain(),
        {},
    )

    original_parse = parse_post_htmls_for_images

    def capture_parse(htmls: list[PostHtml]) -> list[ParsedPostHtml]:
        if parsed_lous is not None:
            parsed_lous.append([item["lou"] for item in htmls])
        return original_parse(htmls)

    def capture_download(
        tid: int,
        aid: int | None,
        tasks: list[dict[str, str]],
    ) -> dict[str, list[object]]:
        del tid, aid
        if downloaded_urls is not None:
            downloaded_urls.append([task["url"] for task in tasks])
        return {"succeeded": [], "failed": []}

    with ExitStack() as stack:
        stack.enter_context(patch("nga_tools.backup.archive.NGAClient", return_value=client))
        stack.enter_context(
            patch(
                "nga_tools.backup.archive.utils.get_folder",
                side_effect=_fake_get_folder(thread_dir),
            )
        )
        stack.enter_context(
            patch(
                "nga_tools.backup.archive._build_floor_map_for_post_refs",
                return_value=floor_map_result,
            )
        )
        stack.enter_context(
            patch(
                "nga_tools.backup.archive._parse_post_htmls_for_images",
                side_effect=capture_parse,
            )
        )
        stack.enter_context(
            patch(
                "nga_tools.backup.archive._download_images",
                side_effect=capture_download,
            )
        )
        stack.enter_context(patch("builtins.print"))
        stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
        if mode == "all":
            backup_thread(123, 456, write_json=write_json)
        else:
            backup_thread_sub(123, 456, write_json=write_json)


class ImageReferenceCollectionTest:
    def test_collects_normalized_valid_image_without_rewriting_html(self) -> None:
        raw_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-,552eXuT3cS10p-7f7.png"
        )
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{raw_url}" />'}
        ]

        tasks = collect_image_download_tasks(htmls, FloorLabels.plain())

        assert tasks == [{"url": raw_url.replace(",", "")}]
        assert raw_url in htmls[0]["html"]

    def test_skips_invalid_and_hidden_images_but_uses_lazy_source(self) -> None:
        lazy_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202604/24/lsQ2x-ji3jKnT3cS14u-c3.webp"
        )
        htmls: list[PostHtml] = [
            {
                "lou": 1,
                "pid": 1001,
                "html": (
                    '<img src="about:blank" style="display:none" />'
                    f'<img src="about:blank" data-srcorg="{lazy_url}" />'
                    '<img src="./broken.png" />'
                ),
            }
        ]

        with redirect_stdout(io.StringIO()) as output:
            parsed = parse_post_htmls_for_images(htmls)
            tasks = collect_image_download_tasks_from_parsed(
                parsed,
                FloorLabels.plain(),
            )

        assert tasks == [{"url": lazy_url}]
        assert "第1楼的第3张图片链接无效" in output.getvalue()

    def test_attachment_metadata_repairs_relative_image_during_temporary_render(self) -> None:
        page_data = {
            "result": [
                {
                    "lou": 1,
                    "pid": 1001,
                    "content": "[img]./mon_202506/06/example.png[/img]",
                    "attches": [
                        {
                            "type": "img",
                            "attachurl": "mon_202506/06/example.png",
                        }
                    ],
                }
            ]
        }

        html = build_post_htmls({1: page_data})[0]["html"]

        assert (
            'src="https://img.nga.178.com/attachments/'
            'mon_202506/06/example.png"'
        ) in html


class MissingFloorTest:
    def test_merge_missing_lou_sorts_and_deduplicates(self) -> None:
        assert merge_missing_lou([3, 1], [2, 3]) == [1, 2, 3]

    def test_find_missing_lou_treats_vrows_as_count(self) -> None:
        posts: list[PostHtml] = [
            {"lou": 0, "pid": 1000, "html": "zero"},
            {"lou": 1, "pid": 1001, "html": "one"},
            {"lou": 3, "pid": 1003, "html": "three"},
        ]

        assert find_missing_lou(posts, total_lou_count=4) == [2]


class SparseAuthorPageTest:
    class Client:
        def get_page(
            self,
            tid: int,
            aid: int | None,
            page: int,
        ) -> dict[str, object]:
            del tid, aid, page
            raise NGAPageError(None, "找不到内容 或 没有更多页了")

    def test_in_range_author_empty_page_becomes_empty_snapshot(self) -> None:
        first_page = {"currentPage": 1, "totalPage": 3, "vrows": 40, "result": []}

        with redirect_stdout(io.StringIO()):
            page = fetch_backup_page(
                self.Client(), 123, 456, 2, 3, first_page
            )

        assert page["currentPage"] == 2
        assert page["result"] == []
        assert page["msg"] == "作者筛选空页"

    def test_original_thread_empty_page_error_still_fails(self) -> None:
        with pytest.raises(NGAPageError):
            fetch_backup_page(
                self.Client(),
                123,
                None,
                2,
                3,
                {"currentPage": 1, "totalPage": 3, "result": []},
            )


class BackupRawArchiveTest:
    @pytest.mark.parametrize("mode", ["sub", "all"])
    def test_backup_writes_archive_without_intermediate_html(
        self,
        tmp_path: Path,
        mode: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"

        _run_backup(thread_dir, MutableFakeClient(), mode=mode)

        assert (thread_dir / "archive.sqlite3").is_file()
        assert not (thread_dir / "html_modified").exists()
        assert not (thread_dir / "backup_state.json").exists()
        assert not (thread_dir / "floor_map.json").exists()
        assert not (thread_dir / "json").exists()

    @pytest.mark.parametrize("mode", ["sub", "all"])
    def test_backup_keeps_old_html_artifacts_untouched(
        self,
        tmp_path: Path,
        mode: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        old_html = thread_dir / "html_modified" / "post_1.html"
        old_overlay = thread_dir / "overlay" / "post_1.html"
        old_html.parent.mkdir(parents=True)
        old_overlay.parent.mkdir(parents=True)
        old_html.write_text("old html sentinel", encoding="utf-8")
        old_overlay.write_text("old overlay sentinel", encoding="utf-8")
        (thread_dir / "backup_state.json").write_text("old state", encoding="utf-8")
        (thread_dir / "floor_map.json").write_text("old map", encoding="utf-8")

        _run_backup(thread_dir, MutableFakeClient(), mode=mode)

        assert old_html.read_text(encoding="utf-8") == "old html sentinel"
        assert old_overlay.read_text(encoding="utf-8") == "old overlay sentinel"
        assert (thread_dir / "backup_state.json").read_text(encoding="utf-8") == "old state"
        assert (thread_dir / "floor_map.json").read_text(encoding="utf-8") == "old map"

    @pytest.mark.parametrize("mode", ["sub", "all"])
    def test_write_json_remains_explicit(
        self,
        tmp_path: Path,
        mode: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"

        _run_backup(
            thread_dir,
            MutableFakeClient(),
            mode=mode,
            write_json=True,
        )

        assert (thread_dir / "json" / "page_1.json").is_file()

    def test_sub_requires_explicit_migration_for_legacy_json(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        json_dir = thread_dir / "json"
        json_dir.mkdir(parents=True)
        (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(RuntimeError, match="正常备份不再读取旧JSON"):
            _run_backup(thread_dir, MutableFakeClient())

        assert not (thread_dir / "archive.sqlite3").exists()

    def test_sub_fully_parses_all_effective_records_each_run(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        parsed_lous: list[list[int]] = []
        _run_backup(thread_dir, client, parsed_lous=parsed_lous)
        client.posts[1]["content"] = "second changed"

        _run_backup(thread_dir, client, parsed_lous=parsed_lous)

        assert parsed_lous == [[1, 2], [1, 2]]

    def test_failed_image_is_reconsidered_on_every_sub_run(self, tmp_path: Path) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/failed.png"
        )
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{image_url}[/img]"}
        ]
        client.vrows = 2
        downloaded_urls: list[list[str]] = []
        thread_dir = tmp_path / "123_456"

        _run_backup(thread_dir, client, downloaded_urls=downloaded_urls)
        _run_backup(thread_dir, client, downloaded_urls=downloaded_urls)

        assert downloaded_urls == [[image_url], [image_url]]

    def test_archive_keeps_historical_lou_when_latest_page_loses_it(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.posts = [{"lou": 1, "pid": 1001, "content": "old visible"}]
        client.vrows = 2
        _run_backup(thread_dir, client)
        client.posts = [{"lou": 2, "pid": 1002, "content": "new visible"}]
        client.vrows = 3

        _run_backup(thread_dir, client)
        records = ThreadArchiveStore(thread_dir).read_effective_post_records()

        assert [record["lou"] for record in records] == [1, 2]
        assert [record["post"]["content"] for record in records] == [
            "old visible",
            "new visible",
        ]

    def test_recovered_anonymous_post_is_persisted_as_normal_archive_version(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": "first"},
            {"lou": 3, "pid": 1003, "content": "third"},
        ]
        client.vrows = 4
        recovered: RecoveredMissingPost = {
            "original_pid": 2002,
            "original_lou": 11,
            "content": "anonymous body",
            "raw_post": {
                "lou": 11,
                "pid": 2002,
                "content": "anonymous body",
                "author": {"uid": -1, "username": "匿名"},
                "postdate": 123456,
                "attches": [],
            },
        }
        floor_result = FloorMapBuildResult(
            FloorLabels(
                original_lou_by_author_lou={1: 10, 2: 11, 3: 12},
                candidate_original_lous_by_author_lou={},
                show_original=True,
            ),
            {2: recovered},
        )

        _run_backup(thread_dir, client, floor_map_result=floor_result)
        store = ThreadArchiveStore(thread_dir)
        records = store.read_effective_post_records()
        author_refs = store.read_latest_author_post_refs()

        assert [record["lou"] for record in records] == [1, 2, 3]
        assert records[1]["post"]["content"] == "anonymous body"
        assert {ref["author_lou"] for ref in author_refs} == {1, 3}

    @pytest.mark.parametrize("mode", ["sub", "all"])
    def test_timing_records_raw_render_stages(
        self,
        tmp_path: Path,
        mode: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        timing_path = tmp_path / f"{mode}.timing.log"

        with use_timing_log(timing_path, task_name=f"backup {mode}"):
            _run_backup(thread_dir, MutableFakeClient(), mode=mode)

        timing_text = timing_path.read_text(encoding="utf-8")
        for stage_name in (
            "读取完整归档记录",
            "正文解析与图片处理",
            "BBCode转临时HTML",
            "图片解析与任务收集",
        ):
            assert f"阶段：{stage_name}，开始时间：" in timing_text
            assert f"阶段：{stage_name}，结束时间：" in timing_text


def test_recovered_post_upsert_is_idempotent_and_preserves_metadata(
    tmp_path: Path,
) -> None:
    store = ThreadArchiveStore(tmp_path / "thread")
    store.upsert_page(
        1,
        {
            "totalPage": 1,
            "vrows": 3,
            "result": [{"lou": 1, "pid": 1001, "content": "first"}],
        },
    )
    recovered: RecoveredMissingPost = {
        "original_pid": 2002,
        "original_lou": 11,
        "content": "anonymous body",
        "raw_post": {
            "lou": 11,
            "pid": 2002,
            "content": "anonymous body",
            "author": {"uid": -1, "username": "匿名"},
            "postdate": "2026-07-11 10:00",
            "attches": [
                {
                    "type": "img",
                    "attachurl": "mon_202506/06/example.png",
                }
            ],
        },
    }

    assert store.upsert_recovered_posts({2: recovered}) == 1
    assert store.upsert_recovered_posts({2: recovered}) == 0
    rows = store.read_effective_post_rows({2})

    assert len(rows) == 1
    assert rows[0].lou == 2
    assert rows[0].pid == 2002
    assert rows[0].author_uid == -1
    assert rows[0].postdate_json == '"2026-07-11 10:00"'
    assert rows[0].image_attachments_json is not None
    assert store.read_latest_author_post_refs() == [
        {"pid": 1001, "author_lou": 1}
    ]

from __future__ import annotations

import io
import json
import sqlite3
from collections.abc import Sequence
from contextlib import ExitStack, closing, redirect_stdout
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from nga_tools.backup import archive as archive_module
from nga_tools.backup.archive import (
    FloorMapProcessingResult,
    backup_local_work_kind,
    maintain_thread_backup,
    backup_thread,
    backup_thread_sub,
)
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels, FloorMapBuildResult
from nga_tools.backup.floor_models import RecoveredMissingPost
from nga_tools.backup.image_pipeline import (
    ImageDownloadOutcome,
    collect_image_download_tasks,
    collect_image_download_tasks_from_parsed,
    parse_post_htmls_for_images,
)
from nga_tools.backup.models import ParsedPostHtml, PostHtml, PostRecord
from nga_tools.backup.page_store import fetch_backup_page
from nga_tools.backup.post_html import (
    build_post_htmls,
    find_missing_lou,
    merge_missing_lou,
)
from nga_tools.backup.post_overlay import make_post_overlay
from nga_tools.backup.post_version_selection import write_selections
from nga_tools.backup.processing_state import BackupProcessingSnapshot
from nga_tools.core.downloads import DownloadFailureKind, DownloadFileResult
from nga_tools.ngaclient.client import NGAPageError
from nga_tools.timing import use_timing_log


class MutableFakeClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = [
            {"lou": 1, "pid": 1001, "content": "first"},
            {"lou": 2, "pid": 1002, "content": "second"},
        ]
        self.vrows: int | None = 3
        self.total_page = 1
        self.get_page_calls: list[int] = []
        self.page_cache: dict[str, dict[str, object]] = {}

    def clear_page_cache(self) -> int:
        cleared_count = len(self.page_cache)
        self.page_cache.clear()
        return cleared_count

    def get_page_count(self, tid: int, aid: int | None) -> int:
        del tid, aid
        return 1

    def get_page(
        self,
        tid: int,
        aid: int | None,
        page: int,
    ) -> dict[str, object]:
        del tid
        self.get_page_calls.append(page)
        data: dict[str, object] = {
            "currentPage": page,
            "totalPage": self.total_page,
            "result": [dict(post) for post in self.posts],
        }
        if aid is not None and self.vrows is not None:
            data["vrows"] = self.vrows
        return data

    def get_pages(
        self,
        tid: int,
        aid: int | None,
        pages: Sequence[int],
        *,
        on_page_complete: Callable[[int, int, int], None] | None = None,
    ) -> dict[int, dict[str, object]]:
        result: dict[int, dict[str, object]] = {}
        total = len(pages)
        for completed, page in enumerate(pages, start=1):
            result[page] = self.get_page(tid, aid, page)
            if on_page_complete is not None:
                on_page_complete(page, completed, total)
        return result


class FailingTailFakeClient(MutableFakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.total_page = 2
        self.vrows = 2
        self.posts_by_page: dict[int, list[dict[str, object]]] = {
            1: [{"lou": 1, "pid": 1001, "content": "first"}],
            2: [{"lou": 2, "pid": 1002, "content": "second"}],
        }
        self.fail_tail = False

    def get_page(
        self,
        tid: int,
        aid: int | None,
        page: int,
    ) -> dict[str, object]:
        del tid
        self.get_page_calls.append(page)
        if page == 2 and self.fail_tail:
            raise RuntimeError("tail fetch failed")
        data: dict[str, object] = {
            "currentPage": page,
            "totalPage": self.total_page,
            "result": [dict(post) for post in self.posts_by_page[page]],
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
    failed_download_urls: set[str] | None = None,
    failed_download_kind: DownloadFailureKind = "unexpected_download",
    failed_http_status: int | None = None,
    floor_map_cacheable: bool = True,
    aid: int | None = 456,
    full_processing_calls: list[str] | None = None,
    floor_map_calls: list[str] | None = None,
    captured_output: io.StringIO | None = None,
    download_error: Exception | None = None,
    force_processing: bool = False,
    allow_unchanged_author_fast_path: bool = False,
) -> None:
    floor_map_result = floor_map_result or FloorMapBuildResult(
        FloorLabels.plain(),
        {},
    )

    original_parse = parse_post_htmls_for_images
    original_run_full_processing = archive_module._run_full_processing

    def capture_parse(htmls: list[PostHtml]) -> list[ParsedPostHtml]:
        if parsed_lous is not None:
            parsed_lous.append([item["lou"] for item in htmls])
        return original_parse(htmls)

    def capture_download(
        tid: int,
        aid: int | None,
        tasks: list[dict[str, str]],
    ) -> ImageDownloadOutcome:
        del tid, aid
        if downloaded_urls is not None:
            downloaded_urls.append([task["url"] for task in tasks])
        if download_error is not None:
            raise download_error
        failed_urls = failed_download_urls or set()
        failed: list[DownloadFileResult] = []
        for task in tasks:
            if task["url"] not in failed_urls:
                continue
            result: DownloadFileResult = {
                "url": task["url"],
                "save_path": "",
                "success": False,
                "failure_kind": failed_download_kind,
            }
            if failed_http_status is not None:
                result["http_status"] = failed_http_status
            failed.append(result)
        return ImageDownloadOutcome(len(tasks) - len(failed), failed)

    def capture_full_processing(*args: object, **kwargs: object) -> None:
        if full_processing_calls is not None:
            full_processing_calls.append("full")
        original_run_full_processing(*args, **kwargs)  # type: ignore[arg-type]

    def capture_floor_map(*args: object, **kwargs: object) -> FloorMapProcessingResult:
        del args, kwargs
        if floor_map_calls is not None:
            floor_map_calls.append("build")
        return FloorMapProcessingResult(
            floor_map_result,
            cacheable=floor_map_cacheable,
        )

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
                side_effect=capture_floor_map,
            )
        )
        stack.enter_context(
            patch(
                "nga_tools.backup.archive._run_full_processing",
                side_effect=capture_full_processing,
            )
        )
        stack.enter_context(
            patch(
                "nga_tools.backup.image_reference_cache."
                "parse_post_htmls_for_images",
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
        stack.enter_context(
            patch(
                "sys.stdout",
                new=captured_output if captured_output is not None else io.StringIO(),
            )
        )
        if mode == "all":
            backup_thread(
                123,
                aid,
                write_json=write_json,
                force_processing=force_processing,
            )
        elif mode == "maintenance":
            maintain_thread_backup(123, aid)
        else:
            backup_thread_sub(
                123,
                aid,
                write_json=write_json,
                force_processing=force_processing,
                allow_unchanged_author_fast_path=(
                    allow_unchanged_author_fast_path
                ),
            )


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

    def test_attachment_metadata_does_not_repair_relative_image(self) -> None:
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

        assert html == "[img]./mon_202506/06/example.png[/img]"


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
    def test_local_work_planning_ignores_legacy_archive_processing_schema(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        _run_backup(thread_dir, MutableFakeClient())
        with sqlite3.connect(thread_dir / "archive.sqlite3") as connection:
            connection.execute(
                """
                CREATE TABLE backup_processing_state (
                    singleton INTEGER PRIMARY KEY,
                    format_version INTEGER NOT NULL,
                    processed_archive_revision INTEGER NOT NULL,
                    processed_floor_map_revision INTEGER NOT NULL,
                    page_count INTEGER NOT NULL,
                    author_total_lou_count INTEGER,
                    post_overlays_fingerprint TEXT NOT NULL,
                    post_version_selections_fingerprint TEXT NOT NULL,
                    floor_map_format_version INTEGER NOT NULL,
                    floor_map_generation_version INTEGER NOT NULL,
                    floor_map_hash_algorithm TEXT NOT NULL,
                    image_reference_extractor_version INTEGER NOT NULL,
                    completed_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO backup_processing_state VALUES
                (1, 999, 999, 999, 999, 999, 'legacy', 'legacy',
                 999, 999, 'legacy', 999, 'legacy')
                """
            )
            connection.commit()
        with sqlite3.connect(thread_dir / "archive_state.sqlite3") as connection:
            connection.execute("DELETE FROM backup_image_reference_state")
            connection.commit()

        with patch.object(
            archive_module.utils,
            "get_folder",
            side_effect=_fake_get_folder(thread_dir),
        ):
            work_kind = backup_local_work_kind(123, 456)

        snapshot = ThreadArchiveStore(thread_dir).read_backup_processing_snapshot()
        assert work_kind == "refresh"
        assert snapshot.floor_state is not None
        assert snapshot.image_state is None

    @pytest.mark.parametrize("mode", ["sub", "all"])
    def test_backup_writes_archive_without_intermediate_html(
        self,
        tmp_path: Path,
        mode: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"

        _run_backup(thread_dir, MutableFakeClient(), mode=mode)

        assert (thread_dir / "archive.sqlite3").is_file()
        store = ThreadArchiveStore(thread_dir)
        processing_snapshot = store.read_backup_processing_snapshot()
        manifest = store.read_image_reference_manifest()
        assert manifest is not None
        assert processing_snapshot.image_state is not None
        assert manifest.state.processed_archive_revision == (
            processing_snapshot.image_state.processed_archive_revision
        )
        assert [post.lou for post in manifest.posts] == [1, 2]
        assert not (thread_dir / "html_modified").exists()
        assert not (thread_dir / "backup_state.json").exists()
        assert not (thread_dir / "floor_map.json").exists()
        assert not (thread_dir / "json").exists()
        assert not (thread_dir / "debug_json").exists()

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

        assert (thread_dir / "debug_json" / "page_1.json").is_file()
        assert not (thread_dir / "json").exists()

    def test_sub_requires_explicit_migration_for_legacy_json(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        json_dir = thread_dir / "json"
        json_dir.mkdir(parents=True)
        (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(RuntimeError, match="正常备份不再读取旧JSON"):
            _run_backup(thread_dir, MutableFakeClient())

        assert not (thread_dir / "archive.sqlite3").exists()

    def test_sub_only_parses_changed_effective_records_after_cache_warmup(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        parsed_lous: list[list[int]] = []
        full_processing_calls: list[str] = []
        labels: list[tuple[str, str]] = []
        _run_backup(
            thread_dir,
            client,
            parsed_lous=parsed_lous,
            full_processing_calls=full_processing_calls,
        )
        client.posts[1]["content"] = "second changed"

        with patch(
            "nga_tools.backup.archive.record_timing_label",
            side_effect=lambda name, value: labels.append((name, value)),
        ):
            _run_backup(
                thread_dir,
                client,
                parsed_lous=parsed_lous,
                full_processing_calls=full_processing_calls,
            )

        assert parsed_lous == [[1, 2], [2]]
        assert full_processing_calls == ["full"]
        assert ("图片引用处理模式", "delta") in labels

    def test_incremental_image_manifest_prepares_only_new_urls(
        self,
        tmp_path: Path,
    ) -> None:
        old_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/11/delta-old.png"
        )
        new_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/11/delta-new.png"
        )
        shared_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/11/delta-shared.png"
        )
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{old_url}[/img]"},
            {"lou": 2, "pid": 1002, "content": f"[img]{shared_url}[/img]"},
        ]
        downloaded_urls: list[list[str]] = []
        full_processing_calls: list[str] = []
        thread_dir = tmp_path / "123_456"

        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            full_processing_calls=full_processing_calls,
        )
        client.posts[0]["content"] = f"[img]{new_url}[/img]"
        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            full_processing_calls=full_processing_calls,
        )

        manifest = ThreadArchiveStore(
            thread_dir
        ).read_image_reference_manifest()
        assert downloaded_urls == [[old_url, shared_url], [new_url]]
        assert full_processing_calls == ["full"]
        assert manifest is not None
        assert manifest.url_reference_counts == (
            (new_url, 1, True),
            (shared_url, 1, True),
        )

    def test_legacy_image_state_lazily_bootstraps_manifest_on_change(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        parsed_lous: list[list[int]] = []
        full_processing_calls: list[str] = []
        labels: list[tuple[str, str]] = []
        _run_backup(
            thread_dir,
            client,
            parsed_lous=parsed_lous,
            full_processing_calls=full_processing_calls,
        )
        store = ThreadArchiveStore(thread_dir)
        with closing(sqlite3.connect(store.state_store.db_path)) as connection:
            connection.execute(
                "DELETE FROM backup_image_reference_manifest_entries"
            )
            connection.execute("DELETE FROM backup_image_reference_manifest_posts")
            connection.execute("DELETE FROM backup_image_reference_manifest_urls")
            connection.execute("DELETE FROM backup_image_reference_manifest_state")
            connection.commit()
        client.posts[1]["content"] = "second changed for bootstrap"

        with patch(
            "nga_tools.backup.archive.record_timing_label",
            side_effect=lambda name, value: labels.append((name, value)),
        ):
            _run_backup(
                thread_dir,
                client,
                parsed_lous=parsed_lous,
                full_processing_calls=full_processing_calls,
            )

        manifest = store.read_image_reference_manifest()
        assert parsed_lous == [[1, 2], [2]]
        assert full_processing_calls == ["full"]
        assert ("图片引用处理模式", "bootstrap") in labels
        assert manifest is not None
        assert [post.lou for post in manifest.posts] == [1, 2]

    def test_legacy_image_state_does_not_bootstrap_on_unchanged_hit(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        parsed_lous: list[list[int]] = []
        _run_backup(thread_dir, client, parsed_lous=parsed_lous)
        store = ThreadArchiveStore(thread_dir)
        with closing(sqlite3.connect(store.state_store.db_path)) as connection:
            connection.execute(
                "DELETE FROM backup_image_reference_manifest_entries"
            )
            connection.execute("DELETE FROM backup_image_reference_manifest_posts")
            connection.execute("DELETE FROM backup_image_reference_manifest_urls")
            connection.execute("DELETE FROM backup_image_reference_manifest_state")
            connection.commit()

        _run_backup(thread_dir, client, parsed_lous=parsed_lous)

        assert parsed_lous == [[1, 2]]
        assert store.read_image_reference_manifest() is None

    def test_partial_image_manifest_falls_back_to_full_rebuild(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        labels: list[tuple[str, str]] = []
        _run_backup(thread_dir, client)
        store = ThreadArchiveStore(thread_dir)
        with closing(sqlite3.connect(store.state_store.db_path)) as connection:
            connection.execute(
                "DELETE FROM backup_image_reference_manifest_entries WHERE lou = 2"
            )
            connection.execute(
                "DELETE FROM backup_image_reference_manifest_posts WHERE lou = 2"
            )
            connection.commit()
        client.posts[1]["content"] = "changed after manifest corruption"

        with patch(
            "nga_tools.backup.archive.record_timing_label",
            side_effect=lambda name, value: labels.append((name, value)),
        ):
            _run_backup(thread_dir, client)

        manifest = store.read_image_reference_manifest()
        assert ("图片引用处理模式", "full") in labels
        assert manifest is not None
        assert [post.lou for post in manifest.posts] == [1, 2]

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

        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
        )
        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
        )

        assert downloaded_urls == [[image_url], [image_url]]

    def test_second_unchanged_sub_uses_thread_fast_path(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        parsed_lous: list[list[int]] = []
        full_processing_calls: list[str] = []
        output = io.StringIO()

        _run_backup(
            thread_dir,
            client,
            parsed_lous=parsed_lous,
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            parsed_lous=parsed_lous,
            full_processing_calls=full_processing_calls,
            captured_output=output,
        )

        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        assert full_processing_calls == ["full"]
        assert parsed_lous == [[1, 2]]
        assert snapshot.floor_state is not None
        assert snapshot.image_state is not None
        assert "归档与派生输入未变化，跳过完整处理" in output.getvalue()

    def test_full_backup_warms_state_for_following_sub(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            mode="all",
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )

        assert full_processing_calls == ["full"]
        assert client.get_page_calls == [1, 1, 1, 1]

    @pytest.mark.parametrize("aid", [456, None])
    def test_second_unchanged_full_backup_reuses_processing_state(
        self,
        tmp_path: Path,
        aid: int | None,
    ) -> None:
        thread_dir = tmp_path / f"123_{aid if aid is not None else 'all'}"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            mode="all",
            aid=aid,
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            mode="all",
            aid=aid,
            full_processing_calls=full_processing_calls,
        )

        assert full_processing_calls == ["full"]
        assert client.get_page_calls == [1, 1, 1, 1]

    def test_force_processing_bypasses_reusable_state(self, tmp_path: Path) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        timing_path = tmp_path / "forced.timing.log"
        _run_backup(
            thread_dir,
            client,
            mode="all",
            full_processing_calls=full_processing_calls,
        )

        with use_timing_log(
            timing_path,
            task_name="backup all forced",
        ) as timing_log:
            assert timing_log is not None
            _run_backup(
                thread_dir,
                client,
                mode="all",
                full_processing_calls=full_processing_calls,
                force_processing=True,
            )

        assert full_processing_calls == ["full", "full"]
        assert (
            "标签：处理状态复用结果，值：forced\n"
            in timing_log.path.read_text(encoding="utf-8")
        )

    def test_attachment_change_writes_empty_metadata_without_reprocessing_images(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        parsed_lous: list[list[int]] = []
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
            parsed_lous=parsed_lous,
        )
        client.posts[0]["attches"] = [
            {
                "type": "img",
                "attachurl": "mon_202607/11/new.png",
            }
        ]

        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
            parsed_lous=parsed_lous,
        )

        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            attachment_row = connection.execute(
                """
                SELECT image_attachments_json
                FROM post_latest_metadata
                WHERE pid = 1001 AND lou = 1
                """
            ).fetchone()
            attachment_column = next(
                row
                for row in connection.execute(
                    "PRAGMA table_info(post_latest_metadata)"
                ).fetchall()
                if row[1] == "image_attachments_json"
            )

        assert attachment_row == ("[]",)
        assert attachment_column[2] == "TEXT"
        assert attachment_column[3] == 1
        assert full_processing_calls == ["full"]
        assert parsed_lous == [[1, 2]]

    @pytest.mark.parametrize("changed_input", ["page_count", "vrows"])
    def test_remote_derived_input_changes_invalidate_fast_path(
        self,
        tmp_path: Path,
        changed_input: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        parsed_lous: list[list[int]] = []
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
            parsed_lous=parsed_lous,
        )

        if changed_input == "page_count":
            client.total_page = 2
        else:
            client.vrows = 4

        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
            parsed_lous=parsed_lous,
        )

        assert full_processing_calls == ["full"]
        assert parsed_lous == [[1, 2]]

    @pytest.mark.parametrize(
        "changed_input",
        ["overlay", "selection", "algorithm", "image_extractor"],
    )
    def test_local_derived_input_changes_invalidate_fast_path(
        self,
        tmp_path: Path,
        changed_input: str,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )

        if changed_input == "overlay":
            ThreadArchiveStore(thread_dir).upsert_post_overlay(
                1,
                make_post_overlay("overlay replacement"),
            )
            _run_backup(
                thread_dir,
                client,
                full_processing_calls=full_processing_calls,
            )
        elif changed_input == "selection":
            write_selections(
                thread_dir,
                {
                    1: {
                        "version_id": 999,
                        "source_hash": "not-a-real-version",
                        "selected_at": "2026-07-11T00:00:00+00:00",
                    }
                },
            )
            _run_backup(
                thread_dir,
                client,
                full_processing_calls=full_processing_calls,
            )
        elif changed_input == "algorithm":
            with patch(
                "nga_tools.backup.archive.FLOOR_MAP_GENERATION_VERSION",
                999,
            ):
                _run_backup(
                    thread_dir,
                    client,
                    full_processing_calls=full_processing_calls,
                )
        else:
            with patch(
                "nga_tools.backup.archive.IMAGE_REFERENCE_EXTRACTOR_VERSION",
                999,
            ):
                _run_backup(
                    thread_dir,
                    client,
                    full_processing_calls=full_processing_calls,
                )

        assert full_processing_calls == ["full"]

    def test_original_thread_backup_reuses_processing_state(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_all"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            aid=None,
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            aid=None,
            full_processing_calls=full_processing_calls,
        )

        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        assert full_processing_calls == ["full"]
        assert snapshot.floor_state is not None
        assert snapshot.image_state is not None

    def test_floor_state_hit_without_missing_lous_skips_floor_map_refresh(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        floor_map_calls: list[str] = []

        _run_backup(thread_dir, client, floor_map_calls=floor_map_calls)
        _run_backup(thread_dir, client, floor_map_calls=floor_map_calls)

        assert floor_map_calls == ["build"]

    def test_maintenance_reuses_preloaded_processing_snapshot(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        _run_backup(thread_dir, client)
        original_read = ThreadArchiveStore.read_backup_processing_snapshot
        read_count = 0

        def capture_read(store: ThreadArchiveStore) -> BackupProcessingSnapshot:
            nonlocal read_count
            read_count += 1
            return original_read(store)

        with patch.object(
            ThreadArchiveStore,
            "read_backup_processing_snapshot",
            autospec=True,
            side_effect=capture_read,
        ):
            _run_backup(thread_dir, client, mode="maintenance")

        assert read_count == 1

    def test_successful_pending_retry_clears_queue_without_history_scan(
        self,
        tmp_path: Path,
    ) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/retry.png"
        )
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{image_url}[/img]"}
        ]
        client.vrows = 2
        downloaded_urls: list[list[str]] = []
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            full_processing_calls=full_processing_calls,
        )
        after_success = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            full_processing_calls=full_processing_calls,
        )

        assert full_processing_calls == ["full"]
        assert downloaded_urls == [[image_url], [image_url], []]
        assert after_success.pending_image_retries == ()

    def test_recent_404_is_deferred_and_force_processing_retries_it(
        self,
        tmp_path: Path,
    ) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/deferred.png"
        )
        thread_dir = tmp_path / "123_all"
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{image_url}[/img]"}
        ]
        downloaded_urls: list[list[str]] = []

        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            failed_download_kind="http_4xx",
            failed_http_status=404,
        )
        with closing(
            sqlite3.connect(thread_dir / "archive_state.sqlite3")
        ) as connection:
            connection.execute(
                """
                UPDATE backup_pending_images
                SET last_attempt_at = '2099-01-01T00:00:00+00:00'
                WHERE url = ?
                """,
                (image_url,),
            )
            connection.commit()

        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            failed_download_kind="http_4xx",
            failed_http_status=404,
        )
        deferred_snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        with patch.object(
            archive_module.utils,
            "get_folder",
            side_effect=_fake_get_folder(thread_dir),
        ):
            local_work = backup_local_work_kind(123, None)

        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            force_processing=True,
        )
        forced_snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()

        assert downloaded_urls == [[image_url], [], [image_url]]
        assert local_work is None
        deferred_at = deferred_snapshot.pending_image_retries[0].last_attempt_at
        assert deferred_at is not None
        assert deferred_at.year == 2099
        assert forced_snapshot.pending_image_retries == ()

    def test_404_at_deadline_is_due_for_local_maintenance(
        self,
        tmp_path: Path,
    ) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/due.png"
        )
        thread_dir = tmp_path / "123_all"
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{image_url}[/img]"}
        ]
        downloaded_urls: list[list[str]] = []

        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            failed_download_kind="http_4xx",
            failed_http_status=404,
        )
        with closing(
            sqlite3.connect(thread_dir / "archive_state.sqlite3")
        ) as connection:
            connection.execute(
                """
                UPDATE backup_pending_images
                SET last_attempt_at = '2000-01-01T00:00:00+00:00'
                WHERE url = ?
                """,
                (image_url,),
            )
            connection.commit()
        with patch.object(
            archive_module.utils,
            "get_folder",
            side_effect=_fake_get_folder(thread_dir),
        ):
            local_work = backup_local_work_kind(123, None)

        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
        )
        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()

        assert local_work == "maintenance"
        assert downloaded_urls == [[image_url], [image_url]]
        assert snapshot.pending_image_retries == ()

    def test_removed_image_reference_prunes_deferred_retry(
        self,
        tmp_path: Path,
    ) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/removed.png"
        )
        thread_dir = tmp_path / "123_all"
        client = MutableFakeClient()
        client.posts = [
            {"lou": 1, "pid": 1001, "content": f"[img]{image_url}[/img]"}
        ]
        downloaded_urls: list[list[str]] = []
        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            failed_download_kind="http_4xx",
            failed_http_status=404,
        )

        client.posts = [{"lou": 1, "pid": 1001, "content": "image removed"}]
        _run_backup(
            thread_dir,
            client,
            aid=None,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            failed_download_kind="http_4xx",
            failed_http_status=404,
        )
        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()

        assert downloaded_urls == [[image_url], []]
        assert snapshot.pending_image_retries == ()

    def test_unresolved_missing_floor_uses_fast_path_after_initial_processing(
        self,
        tmp_path: Path,
    ) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/mon_202506/06/missing.png"
        )
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.posts = [
            {
                "lou": 1,
                "pid": 1001,
                "content": f"first [img]{image_url}[/img]",
            },
            {"lou": 3, "pid": 1003, "content": "third"},
        ]
        client.vrows = 4
        downloaded_urls: list[list[str]] = []
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            full_processing_calls=full_processing_calls,
        )
        first_snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        _run_backup(
            thread_dir,
            client,
            downloaded_urls=downloaded_urls,
            failed_download_urls={image_url},
            full_processing_calls=full_processing_calls,
        )

        assert first_snapshot.floor_state is not None
        assert first_snapshot.image_state is not None
        assert tuple(
            retry.url for retry in first_snapshot.pending_image_retries
        ) == (image_url,)
        assert downloaded_urls == [[image_url], [image_url]]
        assert full_processing_calls == ["full"]

    def test_failed_missing_floor_retry_preserves_existing_state(
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
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )
        before_retry = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        _run_backup(
            thread_dir,
            client,
            floor_map_cacheable=False,
            full_processing_calls=full_processing_calls,
        )
        after_retry = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()

        assert before_retry.floor_state is not None
        assert before_retry.image_state is not None
        assert after_retry.floor_state == before_retry.floor_state
        assert after_retry.image_state == before_retry.image_state
        assert full_processing_calls == ["full", "full"]

    def test_recovered_missing_floor_triggers_one_new_full_processing(
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
            "content": "recovered body",
            "raw_post": {
                "lou": 11,
                "pid": 2002,
                "content": "recovered body",
                "author": {"uid": -1, "username": "匿名"},
                "postdate": 123456,
                "attches": [],
            },
        }
        unresolved_result = FloorMapBuildResult(FloorLabels.plain(), {})
        recovered_result = FloorMapBuildResult(
            FloorLabels(
                original_lou_by_author_lou={1: 10, 2: 11, 3: 12},
                candidate_original_lous_by_author_lou={},
                show_original=True,
            ),
            {2: recovered},
        )
        full_processing_calls: list[str] = []

        _run_backup(
            thread_dir,
            client,
            floor_map_result=unresolved_result,
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            floor_map_result=recovered_result,
            full_processing_calls=full_processing_calls,
        )
        _run_backup(
            thread_dir,
            client,
            floor_map_result=recovered_result,
            full_processing_calls=full_processing_calls,
        )

        records = ThreadArchiveStore(thread_dir).read_effective_post_records()
        assert full_processing_calls == ["full"]
        assert [record["lou"] for record in records] == [1, 2, 3]
        assert records[1]["post"]["content"] == "recovered body"

    def test_full_processing_reuses_initial_records_without_recovery_write(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        original_read = ThreadArchiveStore.read_effective_post_records
        read_count = 0

        def capture_read(
            store: ThreadArchiveStore,
            lous: set[int] | None = None,
        ) -> list[PostRecord]:
            nonlocal read_count
            read_count += 1
            return original_read(store, lous)

        with patch.object(
            ThreadArchiveStore,
            "read_effective_post_records",
            autospec=True,
            side_effect=capture_read,
        ):
            _run_backup(thread_dir, MutableFakeClient())

        assert read_count == 1

    def test_full_processing_rereads_records_after_recovery_write(
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
            "content": "recovered body",
            "raw_post": {
                "lou": 11,
                "pid": 2002,
                "content": "recovered body",
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
        original_read = ThreadArchiveStore.read_effective_post_records
        read_count = 0

        def capture_read(
            store: ThreadArchiveStore,
            lous: set[int] | None = None,
        ) -> list[PostRecord]:
            nonlocal read_count
            read_count += 1
            return original_read(store, lous)

        with patch.object(
            ThreadArchiveStore,
            "read_effective_post_records",
            autospec=True,
            side_effect=capture_read,
        ):
            _run_backup(thread_dir, client, floor_map_result=floor_result)

        assert read_count == 2

    def test_floor_map_failure_does_not_write_fast_path_state(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()

        _run_backup(thread_dir, client, floor_map_cacheable=False)

        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        assert snapshot.floor_state is None

    def test_interrupted_full_processing_leaves_no_fast_path_state(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"

        with pytest.raises(RuntimeError, match="download interrupted"):
            _run_backup(
                thread_dir,
                MutableFakeClient(),
                download_error=RuntimeError("download interrupted"),
            )

        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        assert snapshot.floor_state is None
        assert snapshot.image_state is None
        assert snapshot.pending_image_retries == ()

    def test_invalid_processing_state_is_treated_as_fast_path_miss(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )
        with sqlite3.connect(thread_dir / "archive_state.sqlite3") as connection:
            connection.execute(
                "UPDATE backup_image_reference_state SET completed_at = ''"
            )
            connection.commit()
        output = io.StringIO()
        timing_path = tmp_path / "invalid-state.timing.log"

        with use_timing_log(
            timing_path,
            task_name="invalid state",
        ) as timing_log:
            assert timing_log is not None
            _run_backup(
                thread_dir,
                client,
                full_processing_calls=full_processing_calls,
                captured_output=output,
            )

        snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        assert full_processing_calls == ["full", "full"]
        assert snapshot.image_state is not None
        assert "处理状态无效，改为完整处理" in output.getvalue()
        assert (
            "标签：处理状态复用结果，值：state_invalid\n"
            in timing_log.path.read_text(encoding="utf-8")
        )

    def test_changed_archive_then_downstream_failure_forces_next_full_run(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        full_processing_calls: list[str] = []
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )
        client.posts[1]["content"] = "changed before failure"

        with pytest.raises(RuntimeError, match="downstream failed"):
            _run_backup(
                thread_dir,
                client,
                full_processing_calls=full_processing_calls,
                download_error=RuntimeError("downstream failed"),
            )
        failed_snapshot = ThreadArchiveStore(
            thread_dir
        ).read_backup_processing_snapshot()
        _run_backup(
            thread_dir,
            client,
            full_processing_calls=full_processing_calls,
        )

        assert failed_snapshot.image_state is not None
        assert (
            failed_snapshot.image_state.processed_archive_revision
            != failed_snapshot.change_state.archive_revision
        )
        assert full_processing_calls == ["full"]

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

        with use_timing_log(
            timing_path,
            task_name=f"backup {mode}",
        ) as timing_log:
            assert timing_log is not None
            _run_backup(thread_dir, MutableFakeClient(), mode=mode)

        timing_text = timing_log.path.read_text(encoding="utf-8")
        for stage_name in (
            "楼主最新回复索引读取",
            "历史未恢复缺失楼读取",
            "读取完整归档记录",
            "正文解析与图片处理",
            "图片引用缓存读取",
            "BBCode转临时HTML",
            "图片解析与任务收集",
            "图片引用缓存写入",
        ):
            assert f"阶段：{stage_name}，开始时间：" in timing_text
            assert f"阶段：{stage_name}，结束时间：" in timing_text
        assert "指标：图片引用记录数，值：2\n" in timing_text
        assert "指标：恢复正文写入引发归档重读，值：0\n" in timing_text

    def test_changed_archive_records_floor_refresh_substages(
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
        _run_backup(thread_dir, client)
        client.posts[1]["content"] = "third edited"
        timing_path = tmp_path / "floor-refresh.timing.log"

        with use_timing_log(
            timing_path,
            task_name="backup sub floor refresh",
        ) as timing_log:
            assert timing_log is not None
            _run_backup(thread_dir, client)

        timing_text = timing_log.path.read_text(encoding="utf-8")
        for stage_name in (
            "楼主最新回复索引读取",
            "历史未恢复缺失楼读取",
            "缺失楼恢复与楼层映射",
            "恢复正文事务写入",
            "处理状态快照重读",
            "楼层状态提交",
        ):
            assert f"阶段：{stage_name}，开始时间：" in timing_text
            assert f"阶段：{stage_name}，结束时间：" in timing_text

    def test_fast_path_timing_omits_full_archive_and_image_stages(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        _run_backup(thread_dir, client)
        timing_path = tmp_path / "fast.timing.log"

        with use_timing_log(
            timing_path,
            task_name="backup sub fast",
        ) as timing_log:
            assert timing_log is not None
            _run_backup(thread_dir, client)

        timing_text = timing_log.path.read_text(encoding="utf-8")
        assert "阶段：处理状态复用判定，开始时间：" in timing_text
        assert "阶段：未完成缺失楼重试，开始时间：" in timing_text
        assert "阶段：未完成图片重试，开始时间：" in timing_text
        assert "指标：处理状态复用命中，值：1\n" in timing_text
        assert "标签：处理状态复用结果，值：hit\n" in timing_text
        assert "指标：增量有效变更页数，值：0\n" in timing_text
        assert "指标：待恢复缺失楼数，值：0\n" in timing_text
        assert "指标：缺失楼重试引发完整处理，值：0\n" in timing_text
        assert "阶段：读取完整归档记录，开始时间：" not in timing_text
        assert "阶段：正文解析与图片处理，开始时间：" not in timing_text
        assert "阶段：图片缓存文件校验，开始时间：" not in timing_text

    def test_smart_author_fast_path_does_not_refresh_tail_page(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.total_page = 2
        _run_backup(thread_dir, client)
        client.get_page_calls.clear()

        _run_backup(
            thread_dir,
            client,
            allow_unchanged_author_fast_path=True,
        )

        assert client.get_page_calls == [1]

    def test_smart_author_fast_path_refreshes_tail_when_first_page_changes(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        client.total_page = 2
        _run_backup(thread_dir, client)
        client.get_page_calls.clear()
        client.posts[0]["content"] = "first edited"

        _run_backup(
            thread_dir,
            client,
            allow_unchanged_author_fast_path=True,
        )

        assert client.get_page_calls == [1, 2]

    def test_smart_author_fast_path_retries_tail_before_any_page_commit(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = FailingTailFakeClient()
        _run_backup(thread_dir, client)
        store = ThreadArchiveStore(thread_dir)

        client.posts_by_page[1][0]["content"] = "first edited"
        client.posts_by_page[2][0]["content"] = "second edited"
        client.fail_tail = True
        client.get_page_calls.clear()
        with pytest.raises(RuntimeError, match="tail fetch failed"):
            _run_backup(
                thread_dir,
                client,
                allow_unchanged_author_fast_path=True,
            )

        records_after_failure = store.read_effective_post_records()
        assert [record["post"]["content"] for record in records_after_failure] == [
            "first",
            "second",
        ]
        assert client.get_page_calls == [1, 2]

        client.fail_tail = False
        client.get_page_calls.clear()
        _run_backup(
            thread_dir,
            client,
            allow_unchanged_author_fast_path=True,
        )

        records_after_retry = store.read_effective_post_records()
        assert [record["post"]["content"] for record in records_after_retry] == [
            "first edited",
            "second edited",
        ]
        assert client.get_page_calls == [1, 2]

    def test_maintenance_uses_latest_archived_pagination_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        thread_dir = tmp_path / "123_456"
        client = MutableFakeClient()
        _run_backup(thread_dir, client)
        store = ThreadArchiveStore(thread_dir)
        store.upsert_pages(
            {
                1: {
                    "currentPage": 1,
                    "totalPage": 2,
                    "vrows": 5,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"}
                    ],
                },
                2: {
                    "currentPage": 2,
                    "totalPage": 2,
                    "vrows": 5,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "second"}
                    ],
                },
            }
        )

        _run_backup(thread_dir, client, mode="maintenance")

        state = store.read_backup_processing_snapshot().floor_state
        assert state is not None
        assert state.page_count == 2
        assert state.author_total_lou_count == 5


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

    first_recovery = store.upsert_recovered_posts({2: recovered})
    repeated_recovery = store.upsert_recovered_posts({2: recovered})
    rows = store.read_effective_post_rows({2})
    with closing(sqlite3.connect(store.db_path)) as connection:
        recovered_attachment_row = connection.execute(
            """
            SELECT image_attachments_json
            FROM post_latest_metadata
            WHERE pid = 2002 AND lou = 2
            """
        ).fetchone()

    assert first_recovery.inserted_count == 1
    assert first_recovery.effective_changed_lous == frozenset({2})
    assert first_recovery.effective_added_lous == frozenset({2})
    assert repeated_recovery.inserted_count == 0
    assert repeated_recovery.effective_changed_lous == frozenset()
    assert repeated_recovery.effective_added_lous == frozenset()
    assert len(rows) == 1
    assert rows[0].lou == 2
    assert rows[0].pid == 2002
    assert rows[0].author_uid == -1
    assert rows[0].postdate_json == '"2026-07-11 10:00"'
    assert recovered_attachment_row == ("[]",)
    assert store.read_latest_author_post_refs() == [
        {"pid": 1001, "author_lou": 1}
    ]

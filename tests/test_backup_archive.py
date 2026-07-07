from __future__ import annotations

import io
import json
import sqlite3
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from PIL import Image

from nga_tools import utils
from nga_tools.backup import html_modified_manifest, image_store
from nga_tools.backup.archive import (
    backup_thread,
    backup_thread_sub,
    _build_floor_map_for_backup,
)
from nga_tools.backup.image_pipeline import (
    collect_image_download_tasks as _collect_image_download_tasks,
    collect_image_download_tasks_from_parsed as _collect_image_download_tasks_from_parsed,
    download_images as _download_images,
    parse_post_htmls_for_images as _parse_post_htmls_for_images,
    rewrite_image_links as _rewrite_image_links,
    rewrite_parsed_image_links as _rewrite_parsed_image_links,
)
from nga_tools.backup.models import ParsedPostHtml, PostHtml
from nga_tools.backup.post_html import (
    build_post_htmls as _build_post_htmls,
    fill_missing_lou as _fill_missing_lou,
    load_post_htmls_for_records as _load_post_htmls_for_records,
    merge_missing_lou as _merge_missing_lou,
    prepare_post_records as _prepare_post_records,
    recovered_missing_post_htmls as _recovered_missing_post_htmls,
)
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    AuthorPostRef,
    FloorLabels,
    FloorMapBuildResult,
    RecoveredMissingPost,
    floor_map_input_signature,
)
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import NGAPageError


class RewriteImageLinksTest(unittest.TestCase):
    def test_collects_valid_image_and_rewrites_after_download(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
        )
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{image_url}" alt="" />'}
        ]

        with TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            thread_dir = output_dir / "123_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_path = unique_dir / "hash.png"
            unique_path.write_bytes(b"image")

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = thread_dir if subfolder is None else thread_dir / subfolder
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with (
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(output_dir)})(),
                ),
            ):
                tasks = _collect_image_download_tasks(htmls, FloorLabels.plain())
                image_store.upsert_image_mapping(image_url, unique_path)
                image_lookup = image_store.ImageLookupCache.for_tasks(tasks)
                with patch(
                    "nga_tools.backup.image_pipeline.image_store.unique_image_src_from_html_dir",
                    side_effect=AssertionError("unexpected per-image lookup"),
                ):
                    completed_lous = _rewrite_image_links(
                        htmls,
                        123,
                        None,
                        FloorLabels.plain(),
                        image_lookup=image_lookup,
                    )

        self.assertEqual(
            tasks,
            [
                {
                    "url": image_url,
                }
            ],
        )
        self.assertEqual(completed_lous, {1})
        self.assertIn(
            'src="../../images_unique/hash.png"',
            htmls[0]["html"],
        )

    def test_collects_and_rewrites_from_preparsed_htmls(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
        )
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{image_url}" alt="" />'}
        ]

        with TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            thread_dir = output_dir / "123_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_path = unique_dir / "hash.png"
            unique_path.write_bytes(b"image")

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = thread_dir if subfolder is None else thread_dir / subfolder
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with (
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(output_dir)})(),
                ),
            ):
                parsed_htmls = _parse_post_htmls_for_images(htmls)
                tasks = _collect_image_download_tasks_from_parsed(
                    parsed_htmls,
                    FloorLabels.plain(),
                )
                image_store.upsert_image_mapping(image_url, unique_path)
                image_lookup = image_store.ImageLookupCache.for_tasks(tasks)
                completed_lous = _rewrite_parsed_image_links(
                    parsed_htmls,
                    123,
                    None,
                    FloorLabels.plain(),
                    image_lookup=image_lookup,
                )

        self.assertEqual(tasks, [{"url": image_url}])
        self.assertEqual(completed_lous, {1})
        self.assertIn('src="../../images_unique/hash.png"', htmls[0]["html"])

    def test_removes_comma_before_validating_and_downloading_image(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-,552eXuT3cS10p-7f7.png"
        )
        normalized_url = image_url.replace(",", "")
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{image_url}" alt="" />'}
        ]

        with TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            thread_dir = output_dir / "123_all"

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = thread_dir if subfolder is None else thread_dir / subfolder
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with patch(
                "nga_tools.backup.archive.utils.get_folder",
                side_effect=fake_get_folder,
            ):
                tasks = _collect_image_download_tasks(htmls, FloorLabels.plain())

        self.assertEqual(tasks[0]["url"], normalized_url)
        self.assertIn("lsQkle-,552eXuT3cS10p-7f7.png", htmls[0]["html"])

    def test_skips_invalid_image_download_task(self) -> None:
        invalid_url = "./mon_202506/06/lsQkle-8g6uXvT3cS10o-75l.png[/img</span></div>]"
        htmls: list[PostHtml] = [
            {"lou": 3095, "pid": 826501105, "html": f'<img src="{invalid_url}" />'}
        ]

        with (
            patch(
                "nga_tools.backup.archive.utils.get_folder",
                return_value="/tmp/html_modified",
            ),
            redirect_stdout(io.StringIO()) as output,
        ):
            tasks = _collect_image_download_tasks(htmls, FloorLabels.plain())

        self.assertEqual(tasks, [])
        self.assertIn("警告：第3095楼的第1张图片链接无效", output.getvalue())

    def test_rewrites_failed_download_to_placeholder_without_mapping(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/failed.png"
        )
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{image_url}" alt="" />'}
        ]

        with TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            thread_dir = output_dir / "123_all"

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = thread_dir if subfolder is None else thread_dir / subfolder
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with (
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(output_dir)})(),
                ),
            ):
                completed_lous = _rewrite_image_links(
                    htmls,
                    123,
                    None,
                    FloorLabels.plain(),
                    {image_url},
                )
                placeholder_path = (
                    output_dir
                    / "images_unique"
                    / image_store.PLACEHOLDER_IMAGE_FILENAME
                )
                with Image.open(placeholder_path) as image:
                    image.verify()
                connection = sqlite3.connect(output_dir / "image_index.sqlite3")
                try:
                    placeholder_mappings = connection.execute(
                        """
                        SELECT COUNT(*) FROM image_mappings
                        WHERE unique_rel_path = ?
                        """,
                        (
                            f"images_unique/"
                            f"{image_store.PLACEHOLDER_IMAGE_FILENAME}",
                        ),
                    ).fetchone()[0]
                finally:
                    connection.close()

        self.assertIn(
            f'src="../../images_unique/{image_store.PLACEHOLDER_IMAGE_FILENAME}"',
            htmls[0]["html"],
        )
        self.assertEqual(completed_lous, set())
        self.assertEqual(placeholder_mappings, 0)


class WritePostHtmlsImageRepairTest(unittest.TestCase):
    def test_prepare_records_does_not_render_html_until_needed(self) -> None:
        page_one = {
            "result": [
                {
                    "lou": 1,
                    "pid": 1001,
                    "content": "first",
                    "attches": [],
                }
            ]
        }
        page_two = {
            "result": [
                {
                    "lou": 2,
                    "pid": 1002,
                    "content": "second",
                    "attches": [],
                }
            ]
        }

        with patch(
            "nga_tools.backup.post_html.post_html_from_content",
            side_effect=AssertionError("prepare should not render HTML"),
        ):
            records = _prepare_post_records({1: page_one, 2: page_two})

        html_by_lou = {record["lou"]: record["html"] for record in records}
        self.assertEqual(html_by_lou, {1: None, 2: None})

        def fake_convert(post: object) -> str:
            post_data = cast(dict[str, object], post)
            return f"{post_data['lou']} rendered"

        with patch(
            "nga_tools.backup.post_html.post_html_from_content",
            side_effect=fake_convert,
        ):
            htmls = _load_post_htmls_for_records([records[1]])

        self.assertEqual(htmls, [{"lou": 2, "pid": 1002, "html": "2 rendered"}])
        self.assertIsNone(records[0]["html"])
        self.assertEqual(records[1]["html"], "2 rendered")

    def test_repairs_bad_img_from_attches_before_writing_html(self) -> None:
        page_data = {
            "result": [
                {
                    "lou": 3095,
                    "pid": 826501105,
                    "content": (
                        "24213和24215补档<br/>"
                        "[img]https://img.nga.178.com/attachments/"
                        "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png[/img]"
                        "<br/>[img]./mon_202506/06/"
                        "lsQkle-8g6uXvT3cS10o-75l.png[/img</span></div>]"
                    ),
                    "attches": [
                        {
                            "type": "img",
                            "attachurl": "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
                        },
                        {
                            "type": "img",
                            "attachurl": "mon_202506/06/lsQkle-8g6uXvT3cS10o-75l.png",
                        },
                    ],
                }
            ]
        }

        htmls = _build_post_htmls({155: page_data})
        html = htmls[0]["html"]

        self.assertEqual(html.count("<img"), 2)
        self.assertIn(
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-8g6uXvT3cS10o-75l.png",
            html,
        )
        self.assertNotIn("./mon_202506/06/lsQkle-8g6uXvT3cS10o-75l.png[/img", html)

    def test_unrepairable_bad_img_is_preserved_as_text_not_img(self) -> None:
        page_data = {
            "result": [
                {
                    "lou": 1,
                    "pid": 1001,
                    "content": "[img]./broken.png[/img</span>]",
                    "attches": [],
                }
            ]
        }

        htmls = _build_post_htmls({1: page_data})

        self.assertNotIn("<img", htmls[0]["html"])
        self.assertIn("[img]./broken.png", htmls[0]["html"])
        self.assertIn("&lt;/span&gt;", htmls[0]["html"])


class FillMissingLouTest(unittest.TestCase):
    def test_recovered_missing_post_uses_original_content_html(self) -> None:
        recovered_posts: dict[int, RecoveredMissingPost] = {
            94: {
                "original_pid": 824709822,
                "original_lou": 255,
                "content": "[b]anonymous body[/b]",
            }
        }
        htmls: list[PostHtml] = []
        floor_labels = FloorLabels(
            original_lou_by_author_lou={94: 255},
            candidate_original_lous_by_author_lou={},
            show_original=True,
        )

        recovered_html = _recovered_missing_post_htmls(recovered_posts)
        with (
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            _fill_missing_lou(htmls, [94, 95], floor_labels, recovered_html)

        html_by_lou = {item["lou"]: item["html"] for item in htmls}
        self.assertIn("anonymous body", html_by_lou[94])
        self.assertEqual(html_by_lou[95], "<p><em>本楼层内容缺失。</em></p>")


class BackupFloorMapFallbackTest(unittest.TestCase):
    def test_floor_map_failure_falls_back_to_plain_labels(self) -> None:
        htmls: list[PostHtml] = [{"lou": 1, "pid": 1001, "html": "body"}]

        with (
            patch(
                "nga_tools.backup.archive.build_and_save_floor_map",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "nga_tools.backup.archive.load_floor_labels",
                side_effect=RuntimeError("no map"),
            ),
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            result = _build_floor_map_for_backup(
                cast(NGAClient, object()),
                123,
                456,
                htmls,
                [],
            )

        self.assertFalse(result.floor_labels.show_original)
        self.assertEqual(result.recovered_missing_posts_by_author_lou, {})


class BackupThreadSubMissingLouTest(unittest.TestCase):
    def test_merge_missing_lou_sorts_and_deduplicates(self) -> None:
        self.assertEqual(_merge_missing_lou([4, 2], [2, 3], []), [2, 3, 4])

    def test_author_empty_page_is_written_and_later_pages_continue(self) -> None:
        class SparseAuthorClient:
            def __init__(self) -> None:
                self.page_calls: list[int] = []

            def get_page(
                self,
                tid: int,
                aid: int | None,
                page: int,
            ) -> dict[str, object]:
                del tid
                self.page_calls.append(page)
                if page == 2:
                    raise NGAPageError(35, "找不到内容 或 没有更多页了")
                return {
                    "code": 0,
                    "msg": "操作成功",
                    "currentPage": page,
                    "totalPage": 3,
                    "vrows": 3,
                    "result": [
                        {
                            "lou": page,
                            "pid": 1000 + page,
                            "content": f"page {page}",
                        }
                    ],
                }

        client = SparseAuthorClient()
        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch("nga_tools.backup.archive.NGAClient", return_value=client),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    return_value=FloorMapBuildResult(FloorLabels.plain(), {}),
                ),
                patch(
                    "nga_tools.backup.archive._rewrite_parsed_image_links",
                    return_value=[],
                ),
                patch(
                    "nga_tools.backup.archive._download_images",
                    return_value={"succeeded": [], "failed": []},
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                backup_thread_sub(123, 456, write_json=True)

            empty_page = json.loads(
                (temp_dir / "json" / "page_2.json").read_text(encoding="utf-8")
            )
            page_three = json.loads(
                (temp_dir / "json" / "page_3.json").read_text(encoding="utf-8")
            )

        self.assertEqual(client.page_calls, [1, 1, 2, 3])
        self.assertEqual(empty_page["currentPage"], 2)
        self.assertEqual(empty_page["msg"], "作者筛选空页")
        self.assertEqual(empty_page["result"], [])
        self.assertEqual(page_three["result"][0]["lou"], 3)

    def test_original_empty_page_error_still_fails(self) -> None:
        class OriginalClient:
            def get_page(
                self,
                tid: int,
                aid: int | None,
                page: int,
            ) -> dict[str, object]:
                del tid, aid
                if page == 2:
                    raise NGAPageError(35, "找不到内容 或 没有更多页了")
                return {
                    "code": 0,
                    "msg": "操作成功",
                    "currentPage": page,
                    "totalPage": 3,
                    "result": [
                        {
                            "lou": page,
                            "pid": 1000 + page,
                            "content": f"page {page}",
                        }
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch(
                    "nga_tools.backup.archive.NGAClient",
                    return_value=OriginalClient(),
                ),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaisesRegex(
                    NGAPageError,
                    "找不到内容 或 没有更多页了",
                ):
                    backup_thread_sub(123, None)

    def test_author_non_empty_page_error_still_fails(self) -> None:
        class ErrorClient:
            def get_page(
                self,
                tid: int,
                aid: int | None,
                page: int,
            ) -> dict[str, object]:
                del tid, aid
                if page == 2:
                    raise NGAPageError(403, "权限不足")
                return {
                    "code": 0,
                    "msg": "操作成功",
                    "currentPage": page,
                    "totalPage": 3,
                    "result": [
                        {
                            "lou": page,
                            "pid": 1000 + page,
                            "content": f"page {page}",
                        }
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch(
                    "nga_tools.backup.archive.NGAClient",
                    return_value=ErrorClient(),
                ),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                with self.assertRaisesRegex(NGAPageError, "权限不足"):
                    backup_thread_sub(123, 456)

    def test_backup_sub_retries_previous_missing_author_lous(self) -> None:
        captured_missing_lou: list[int] = []

        class FakeClient:
            def get_page_count(self, tid: int, aid: int | None) -> int:
                return 1

            def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
                return {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 3, "pid": 1003, "content": "third"},
                    ],
                }

        def fake_build_floor_map(
            client: object,
            tid: int,
            aid: int | None,
            htmls: list[PostHtml],
            missing_lou: list[int],
        ) -> FloorMapBuildResult:
            captured_missing_lou[:] = missing_lou
            return FloorMapBuildResult(FloorLabels.plain(), {})

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            (temp_dir / "json").mkdir()
            html_modified_dir = temp_dir / "html_modified"
            html_modified_dir.mkdir()
            (html_modified_dir / "post_2.html").write_text(
                MISSING_POST_HTML,
                encoding="utf-8",
            )
            (html_modified_dir / "post_4.html").write_text(
                MISSING_POST_HTML,
                encoding="utf-8",
            )
            (html_modified_dir / "post_9.html").write_text(
                "already recovered",
                encoding="utf-8",
            )

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch("nga_tools.backup.archive.NGAClient", return_value=FakeClient()),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    side_effect=fake_build_floor_map,
                ),
                patch(
                    "nga_tools.backup.archive._rewrite_parsed_image_links",
                    return_value=[],
                ),
                patch(
                    "nga_tools.backup.archive._download_images",
                    return_value={"succeeded": [], "failed": []},
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                backup_thread_sub(123, 456)

        self.assertEqual(captured_missing_lou, [2, 4])

    def test_backup_sub_does_not_retry_restored_previous_missing_lou(self) -> None:
        captured_missing_lou: list[int] = []

        class FakeClient:
            def get_page_count(self, tid: int, aid: int | None) -> int:
                return 1

            def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
                return {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 2, "pid": 1002, "content": "second restored"},
                        {"lou": 3, "pid": 1003, "content": "third"},
                    ],
                }

        def fake_build_floor_map(
            client: object,
            tid: int,
            aid: int | None,
            htmls: list[PostHtml],
            missing_lou: list[int],
        ) -> FloorMapBuildResult:
            captured_missing_lou[:] = missing_lou
            return FloorMapBuildResult(FloorLabels.plain(), {})

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            (temp_dir / "json").mkdir()
            html_modified_dir = temp_dir / "html_modified"
            html_modified_dir.mkdir()
            (html_modified_dir / "post_2.html").write_text(
                MISSING_POST_HTML,
                encoding="utf-8",
            )

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch("nga_tools.backup.archive.NGAClient", return_value=FakeClient()),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    side_effect=fake_build_floor_map,
                ),
                patch(
                    "nga_tools.backup.archive._rewrite_parsed_image_links",
                    return_value=[],
                ),
                patch(
                    "nga_tools.backup.archive._download_images",
                    return_value={"succeeded": [], "failed": []},
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                backup_thread_sub(123, 456)

            restored_html = (html_modified_dir / "post_2.html").read_text(
                encoding="utf-8"
            )

        self.assertEqual(captured_missing_lou, [])
        self.assertIn("second restored", restored_html)
        self.assertNotIn("本楼层内容缺失", restored_html)


class BackupThreadSubHtmlModifiedManifestTest(unittest.TestCase):
    class MutableFakeClient:
        def __init__(self) -> None:
            self.second_content = "second"

        def get_page_count(self, tid: int, aid: int | None) -> int:
            return 1

        def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
            return {
                "totalPage": 1,
                "result": [
                    {"lou": 1, "pid": 1001, "content": "first"},
                    {"lou": 2, "pid": 1002, "content": self.second_content},
                ],
            }

    class CountedFakeClient(MutableFakeClient):
        def __init__(self) -> None:
            super().__init__()
            self.page_calls: list[int] = []

        def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
            self.page_calls.append(page)
            page_data = super().get_page(tid, aid, page)
            page_data["vrows"] = 2
            return page_data

    def _write_current_floor_map(
        self,
        temp_dir: Path,
        author_posts: list[AuthorPostRef],
        missing_lou: list[int] | None = None,
    ) -> None:
        missing_lou = missing_lou or []
        signature = floor_map_input_signature(author_posts, missing_lou)
        entries = [
            {
                "pid": post["pid"],
                "author_lou": post["author_lou"],
                "original_lou": post["author_lou"],
            }
            for post in author_posts
        ]
        (temp_dir / "floor_map.json").write_text(
            json.dumps(
                {
                    "version": FLOOR_MAP_VERSION,
                    "floor_map_generation_version": FLOOR_MAP_GENERATION_VERSION,
                    "algorithm": FLOOR_MAP_HASH_ALGORITHM,
                    "input_signature": signature,
                    "tid": 123,
                    "aid": 456,
                    "entries": entries,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _run_backup_sub(
        self,
        temp_dir: Path,
        client: object,
        *,
        write_json: bool = False,
        rewrite_side_effect: object | None = None,
        download_return: utils.DownloadSummary | None = None,
    ) -> None:
        def fake_get_folder(
            tid: int,
            aid: int | None,
            subfolder: str | None = None,
        ) -> str:
            path = temp_dir if subfolder is None else temp_dir / subfolder
            path.mkdir(exist_ok=True)
            return str(path)

        with ExitStack() as stack:
            stack.enter_context(
                patch("nga_tools.backup.archive.NGAClient", return_value=client)
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                )
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    return_value=FloorMapBuildResult(FloorLabels.plain(), {}),
                )
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(temp_dir)})(),
                )
            )
            if rewrite_side_effect is not None:
                def rewrite_adapter(
                    parsed_htmls: list[ParsedPostHtml],
                    tid: int,
                    aid: int | None,
                    floor_labels: FloorLabels,
                    failed_image_urls: set[str] | None = None,
                    image_lookup: image_store.ImageLookupCache | None = None,
                ) -> set[int]:
                    return rewrite_side_effect(
                        [parsed_html.post_html for parsed_html in parsed_htmls],
                        tid,
                        aid,
                        floor_labels,
                        failed_image_urls,
                        image_lookup,
                    )

                stack.enter_context(
                    patch(
                        "nga_tools.backup.archive._rewrite_parsed_image_links",
                        side_effect=rewrite_adapter,
                    )
                )
            if download_return is not None:
                stack.enter_context(
                    patch(
                        "nga_tools.backup.archive._download_images",
                        return_value=download_return,
                    )
                )
            stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            backup_thread_sub(123, 456, write_json=write_json)

    def _run_backup_all(
        self,
        temp_dir: Path,
        client: object,
        *,
        write_json: bool = False,
    ) -> None:
        def fake_get_folder(
            tid: int,
            aid: int | None,
            subfolder: str | None = None,
        ) -> str:
            path = temp_dir if subfolder is None else temp_dir / subfolder
            path.mkdir(exist_ok=True)
            return str(path)

        with ExitStack() as stack:
            stack.enter_context(
                patch("nga_tools.backup.archive.NGAClient", return_value=client)
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                )
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    return_value=FloorMapBuildResult(FloorLabels.plain(), {}),
                )
            )
            stack.enter_context(
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(temp_dir)})(),
                )
            )
            stack.enter_context(patch("builtins.print"))
            stack.enter_context(patch("sys.stdout", new_callable=io.StringIO))
            backup_thread(123, 456, write_json=write_json)

    def test_backup_sub_defaults_to_archive_without_json_output(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            self._run_backup_sub(temp_dir, self.MutableFakeClient())

            self.assertTrue((temp_dir / "archive.sqlite3").is_file())
            self.assertTrue((temp_dir / "html_modified").is_dir())
            self.assertFalse((temp_dir / "json").exists())

    def test_backup_all_defaults_to_archive_without_json_output(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            self._run_backup_all(temp_dir, self.MutableFakeClient())

            self.assertTrue((temp_dir / "archive.sqlite3").is_file())
            self.assertTrue((temp_dir / "html_modified").is_dir())
            self.assertFalse((temp_dir / "json").exists())

    def test_backup_sub_writes_json_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            self._run_backup_sub(
                temp_dir,
                self.MutableFakeClient(),
                write_json=True,
            )

            self.assertTrue((temp_dir / "json" / "page_1.json").is_file())

    def test_backup_all_writes_json_when_enabled(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)

            self._run_backup_all(
                temp_dir,
                self.MutableFakeClient(),
                write_json=True,
            )

            self.assertTrue((temp_dir / "json" / "page_1.json").is_file())

    def test_backup_sub_requires_migration_for_legacy_json_without_archive(
        self,
    ) -> None:
        class FakeClient:
            def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
                del tid, aid, page
                return {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "remote"},
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            json_dir = temp_dir / "json"
            json_dir.mkdir()
            (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "正常备份不再读取旧JSON"):
                self._run_backup_sub(temp_dir, FakeClient())

            self.assertFalse((temp_dir / "archive.sqlite3").exists())

    def test_backup_sub_skips_completed_html_modified_lous(self) -> None:
        client = self.MutableFakeClient()
        captured_lous: list[int] = []

        def fake_rewrite(
            htmls: list[PostHtml],
            tid: int,
            aid: int | None,
            floor_labels: FloorLabels,
            failed_image_urls: set[str] | None = None,
            image_lookup: image_store.ImageLookupCache | None = None,
        ) -> set[int]:
            del tid, aid, floor_labels, failed_image_urls, image_lookup
            captured_lous.extend(item["lou"] for item in htmls)
            return {item["lou"] for item in htmls}

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._run_backup_sub(temp_dir, client)
            self.assertFalse((temp_dir / "html").exists())
            entries = html_modified_manifest.load_manifest(
                temp_dir / "html_modified"
            )

            self._run_backup_sub(
                temp_dir,
                client,
                rewrite_side_effect=fake_rewrite,
            )

        self.assertEqual(set(entries), {"post_1.html", "post_2.html"})
        self.assertEqual(captured_lous, [])

    def test_backup_all_manifest_makes_following_sub_skip_completed_lous(self) -> None:
        client = self.MutableFakeClient()
        captured_lous: list[int] = []

        def fake_rewrite(
            htmls: list[PostHtml],
            tid: int,
            aid: int | None,
            floor_labels: FloorLabels,
            failed_image_urls: set[str] | None = None,
            image_lookup: image_store.ImageLookupCache | None = None,
        ) -> set[int]:
            del tid, aid, floor_labels, failed_image_urls, image_lookup
            captured_lous.extend(item["lou"] for item in htmls)
            return {item["lou"] for item in htmls}

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._run_backup_all(temp_dir, client)
            self.assertFalse((temp_dir / "html").exists())
            entries = html_modified_manifest.load_manifest(
                temp_dir / "html_modified"
            )

            self._run_backup_sub(
                temp_dir,
                client,
                rewrite_side_effect=fake_rewrite,
            )

        self.assertEqual(set(entries), {"post_1.html", "post_2.html"})
        self.assertEqual(captured_lous, [])

    def test_backup_all_state_makes_following_sub_fast_skip_by_author_lou_count(
        self,
    ) -> None:
        client = self.CountedFakeClient()
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1002, "author_lou": 2},
        ]

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._write_current_floor_map(temp_dir, author_posts)
            self._run_backup_all(temp_dir, client)
            self.assertFalse((temp_dir / "html").exists())
            self.assertTrue((temp_dir / "backup_state.json").is_file())

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                path = temp_dir if subfolder is None else temp_dir / subfolder
                path.mkdir(exist_ok=True)
                return str(path)

            with (
                patch("nga_tools.backup.archive.NGAClient", return_value=client),
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.backup.archive._build_floor_map_for_post_refs",
                    side_effect=AssertionError("fast skip should not build floor map"),
                ),
                patch(
                    "nga_tools.backup.archive._download_images",
                    side_effect=AssertionError("fast skip should not check images"),
                ),
                patch(
                    "nga_tools.backup.archive._rewrite_parsed_image_links",
                    side_effect=AssertionError("fast skip should not rewrite images"),
                ),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                backup_thread_sub(123, 456)

    def test_unresolved_missing_placeholder_does_not_write_fast_skip_state(
        self,
    ) -> None:
        class MissingFloorClient:
            def get_page_count(self, tid: int, aid: int | None) -> int:
                return 1

            def get_page(
                self,
                tid: int,
                aid: int | None,
                page: int,
            ) -> dict[str, object]:
                return {
                    "totalPage": 1,
                    "vrows": 3,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "first"},
                        {"lou": 3, "pid": 1003, "content": "third"},
                    ],
                }

        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._write_current_floor_map(temp_dir, author_posts, [2])
            self._run_backup_sub(temp_dir, MissingFloorClient())

            entries = html_modified_manifest.load_manifest(
                temp_dir / "html_modified"
            )
            missing_html = (temp_dir / "html_modified" / "post_2.html").read_text(
                encoding="utf-8"
            )
            backup_state_exists = (temp_dir / "backup_state.json").exists()

        self.assertFalse(backup_state_exists)
        self.assertEqual(missing_html, MISSING_POST_HTML)
        self.assertEqual(set(entries), {"post_1.html", "post_3.html"})

    def test_backup_sub_rebuilds_only_changed_source_hash_lous(self) -> None:
        client = self.MutableFakeClient()
        captured_lous: list[int] = []

        def fake_rewrite(
            htmls: list[PostHtml],
            tid: int,
            aid: int | None,
            floor_labels: FloorLabels,
            failed_image_urls: set[str] | None = None,
            image_lookup: image_store.ImageLookupCache | None = None,
        ) -> set[int]:
            del tid, aid, floor_labels, failed_image_urls, image_lookup
            captured_lous.extend(item["lou"] for item in htmls)
            return {item["lou"] for item in htmls}

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            self._run_backup_sub(temp_dir, client)
            client.second_content = "second changed"

            self._run_backup_sub(
                temp_dir,
                client,
                rewrite_side_effect=fake_rewrite,
            )

        self.assertEqual(captured_lous, [2])

    def test_failed_placeholder_html_modified_is_not_marked_complete(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/failed.png"
        )

        class FailedImageClient:
            def get_page_count(self, tid: int, aid: int | None) -> int:
                return 1

            def get_page(
                self,
                tid: int,
                aid: int | None,
                page: int,
            ) -> dict[str, object]:
                return {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": f"[img]{image_url}[/img]",
                        }
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            download_return: utils.DownloadSummary = {
                "succeeded": [],
                "failed": [
                    {
                        "url": image_url,
                        "save_path": str(temp_dir / "images_unique"),
                        "success": False,
                    }
                ],
            }

            self._run_backup_sub(
                temp_dir,
                FailedImageClient(),
                download_return=download_return,
            )

            entries = html_modified_manifest.load_manifest(
                temp_dir / "html_modified"
            )
            html = (temp_dir / "html_modified" / "post_1.html").read_text(
                encoding="utf-8"
            )

        self.assertEqual(entries, {})
        self.assertIn(image_store.PLACEHOLDER_IMAGE_FILENAME, html)

    def test_backup_sub_keeps_historical_lou_when_refreshed_page_loses_it(
        self,
    ) -> None:
        class SwallowedPageClient:
            def __init__(self) -> None:
                self.current_lou = 1

            def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
                del tid, aid, page
                if self.current_lou == 1:
                    return {
                        "totalPage": 1,
                        "result": [
                            {"lou": 1, "pid": 1001, "content": "old visible"},
                        ],
                    }
                return {
                    "totalPage": 1,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "new visible"},
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            client = SwallowedPageClient()
            self._run_backup_sub(temp_dir, client, write_json=True)
            client.current_lou = 2

            self._run_backup_sub(temp_dir, client, write_json=True)

            old_html = (temp_dir / "html_modified" / "post_1.html").read_text(
                encoding="utf-8"
            )
            new_html = (temp_dir / "html_modified" / "post_2.html").read_text(
                encoding="utf-8"
            )
            latest_json = json.loads(
                (temp_dir / "json" / "page_1.json").read_text(encoding="utf-8")
            )

        self.assertIn("old visible", old_html)
        self.assertIn("new visible", new_html)
        self.assertEqual([post["lou"] for post in latest_json["result"]], [2])

    def test_backup_all_keeps_historical_lou_when_full_refresh_loses_it(
        self,
    ) -> None:
        class SwallowedPageClient:
            def __init__(self) -> None:
                self.current_lou = 1

            def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
                del tid, aid, page
                if self.current_lou == 1:
                    return {
                        "totalPage": 1,
                        "result": [
                            {"lou": 1, "pid": 1001, "content": "old visible"},
                        ],
                    }
                return {
                    "totalPage": 1,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "new visible"},
                    ],
                }

        with TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            client = SwallowedPageClient()
            self._run_backup_sub(temp_dir, client)
            client.current_lou = 2

            self._run_backup_all(temp_dir, client)

            old_html = (temp_dir / "html_modified" / "post_1.html").read_text(
                encoding="utf-8"
            )
            new_html = (temp_dir / "html_modified" / "post_2.html").read_text(
                encoding="utf-8"
            )

        self.assertIn("old visible", old_html)
        self.assertIn("new visible", new_html)


class DownloadImagesTest(unittest.TestCase):
    def test_reports_existing_pending_and_completion_progress(self) -> None:
        with TemporaryDirectory() as temp_dir:
            unique_dir = Path(temp_dir) / "images_unique"
            unique_dir.mkdir()
            unique_existing_path = unique_dir / "existing.png"
            unique_existing_path.write_bytes(b"already here")
            existing_url = (
                "https://img.nga.178.com/attachments/mon_202506/06/existing.png"
            )
            pending_url = (
                "https://img.nga.178.com/attachments/mon_202506/06/pending.png"
            )
            files_to_download: list[image_store.ImageDownloadTask] = [
                {"url": existing_url},
                {"url": pending_url},
            ]
            output = io.StringIO()

            def fake_download_image_tasks(
                pending_downloads: list[image_store.ImageDownloadTask],
                *,
                on_progress: utils.DownloadProgressCallback | None = None,
            ) -> utils.DownloadSummary:
                self.assertEqual(pending_downloads, [files_to_download[1]])
                result: utils.DownloadFileResult = {
                    "url": files_to_download[1]["url"],
                    "save_path": str(unique_dir / "pending.png"),
                    "success": True,
                }
                if on_progress is not None:
                    on_progress(1, 1, result)
                return {"succeeded": [result], "failed": []}

            with (
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(temp_dir)})(),
                ),
                patch(
                    "nga_tools.backup.image_pipeline.image_store.download_image_tasks",
                    side_effect=fake_download_image_tasks,
                ),
                redirect_stdout(output),
            ):
                image_store.upsert_image_mapping(existing_url, unique_existing_path)
                _download_images(123, None, files_to_download)

        output_text = output.getvalue()
        self.assertIn("共2张图片，已存在1张，本次下载1张", output_text)
        self.assertIn("本次下载1张 (0/1)", output_text)
        self.assertIn("图片下载进度 (1/1)", output_text)
        self.assertIn("图片下载完成。", output_text)
        self.assertIn("成功下载1个文件，失败0个文件。", output_text)

    def test_reports_zero_progress_when_all_images_exist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            unique_dir = Path(temp_dir) / "images_unique"
            unique_dir.mkdir()
            unique_existing_path = unique_dir / "existing.png"
            unique_existing_path.write_bytes(b"already here")
            existing_url = (
                "https://img.nga.178.com/attachments/mon_202506/06/existing.png"
            )
            files_to_download: list[image_store.ImageDownloadTask] = [
                {"url": existing_url}
            ]
            output = io.StringIO()

            with (
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=type("Config", (), {"output_dir": str(temp_dir)})(),
                ),
                patch(
                    "nga_tools.backup.image_pipeline.image_store.download_image_tasks",
                    return_value={"succeeded": [], "failed": []},
                ) as download_image_tasks,
                redirect_stdout(output),
            ):
                image_store.upsert_image_mapping(existing_url, unique_existing_path)
                _download_images(123, None, files_to_download)

            download_image_tasks.assert_not_called()

        output_text = output.getvalue()
        self.assertIn("共1张图片，已存在1张，本次下载0张", output_text)
        self.assertIn("图片下载进度 (0/0)", output_text)
        self.assertIn("成功下载0个文件，失败0个文件。", output_text)


if __name__ == "__main__":
    unittest.main()

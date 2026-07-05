from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
import unittest
from unittest.mock import patch

from nga_tools import utils
from nga_tools.backup.archive import (
    PostHtml,
    backup_thread_sub,
    _build_floor_map_for_backup,
    _download_images,
    _fill_missing_lou,
    _merge_missing_lou,
    _rewrite_image_links,
    _write_recovered_missing_post_htmls,
)
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    FloorLabels,
    FloorMapBuildResult,
    RecoveredMissingPost,
)
from nga_tools.ngaclient import NGAClient


class RewriteImageLinksTest(unittest.TestCase):
    def test_rewrites_valid_image_and_adds_download_task(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
        )
        htmls: list[PostHtml] = [
            {"lou": 1, "pid": 1001, "html": f'<img src="{image_url}" alt="" />'}
        ]

        with patch(
            "nga_tools.backup.archive.utils.get_folder",
            return_value="/tmp/images",
        ):
            tasks = _rewrite_image_links(htmls, 123, None, FloorLabels.plain())

        self.assertEqual(
            tasks,
            [
                {
                    "url": image_url,
                    "save_path": "/tmp/images/lsQkle-552eXuT3cS10p-7f7.png",
                }
            ],
        )
        self.assertIn(
            'src="../images/lsQkle-552eXuT3cS10p-7f7.png"',
            htmls[0]["html"],
        )

    def test_skips_invalid_image_download_task(self) -> None:
        invalid_url = "./mon_202506/06/lsQkle-8g6uXvT3cS10o-75l.png[/img</span></div>]"
        htmls: list[PostHtml] = [
            {"lou": 3095, "pid": 826501105, "html": f'<img src="{invalid_url}" />'}
        ]

        with (
            patch("nga_tools.backup.archive.utils.get_folder") as get_folder,
            patch("builtins.print") as print_mock,
        ):
            tasks = _rewrite_image_links(htmls, 123, None, FloorLabels.plain())

        self.assertEqual(tasks, [])
        get_folder.assert_not_called()
        print_mock.assert_called_once_with("警告：第3095楼的第1张图片链接无效")


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

        with TemporaryDirectory() as temp_dir:
            with patch(
                "nga_tools.backup.archive.utils.get_folder",
                return_value=temp_dir,
            ):
                recovered_html = _write_recovered_missing_post_htmls(
                    123,
                    456,
                    recovered_posts,
                )

            with patch("builtins.print"):
                _fill_missing_lou(htmls, [94, 95], floor_labels, recovered_html)

            recovered_file = Path(temp_dir) / "post_94.html"
            self.assertTrue(recovered_file.exists())
            self.assertIn("anonymous body", recovered_file.read_text(encoding="utf-8"))

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
                    "nga_tools.backup.archive._build_floor_map_for_backup",
                    side_effect=fake_build_floor_map,
                ),
                patch("nga_tools.backup.archive._rewrite_image_links", return_value=[]),
                patch("nga_tools.backup.archive._download_images"),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                backup_thread_sub(123, 456)

        self.assertEqual(captured_missing_lou, [2, 4])


class DownloadImagesTest(unittest.TestCase):
    def test_reports_existing_pending_and_completion_progress(self) -> None:
        with TemporaryDirectory() as temp_dir:
            existing_path = Path(temp_dir) / "existing.png"
            pending_path = Path(temp_dir) / "pending.png"
            existing_path.write_bytes(b"already here")
            files_to_download: list[utils.DownloadTask] = [
                {
                    "url": "https://img.nga.178.com/existing.png",
                    "save_path": str(existing_path),
                },
                {
                    "url": "https://img.nga.178.com/pending.png",
                    "save_path": str(pending_path),
                },
            ]
            output = io.StringIO()

            def fake_download_files(
                pending_downloads: list[utils.DownloadTask],
                *,
                on_progress: utils.DownloadProgressCallback | None = None,
            ) -> utils.DownloadSummary:
                self.assertEqual(pending_downloads, [files_to_download[1]])
                result: utils.DownloadFileResult = {
                    "url": files_to_download[1]["url"],
                    "save_path": files_to_download[1]["save_path"],
                    "success": True,
                }
                if on_progress is not None:
                    on_progress(1, 1, result)
                return {"succeeded": [result], "failed": []}

            with (
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    return_value=temp_dir,
                ),
                patch(
                    "nga_tools.backup.archive.utils.download_files",
                    side_effect=fake_download_files,
                ),
                redirect_stdout(output),
            ):
                _download_images(123, None, files_to_download)

        output_text = output.getvalue()
        self.assertIn("共2张图片，已存在1张，本次下载1张。", output_text)
        self.assertIn("下载进度：0/1", output_text)
        self.assertIn("下载进度：1/1", output_text)
        self.assertIn("图片下载完成。", output_text)
        self.assertIn("成功下载1个文件，失败0个文件。", output_text)

    def test_reports_zero_progress_when_all_images_exist(self) -> None:
        with TemporaryDirectory() as temp_dir:
            existing_path = Path(temp_dir) / "existing.png"
            existing_path.write_bytes(b"already here")
            files_to_download: list[utils.DownloadTask] = [
                {
                    "url": "https://img.nga.178.com/existing.png",
                    "save_path": str(existing_path),
                }
            ]
            output = io.StringIO()

            with (
                patch(
                    "nga_tools.backup.archive.utils.get_folder",
                    return_value=temp_dir,
                ),
                patch(
                    "nga_tools.backup.archive.utils.download_files",
                    return_value={"succeeded": [], "failed": []},
                ) as download_files,
                redirect_stdout(output),
            ):
                _download_images(123, None, files_to_download)

            download_files.assert_not_called()

        output_text = output.getvalue()
        self.assertIn("共1张图片，已存在1张，本次下载0张。", output_text)
        self.assertIn("下载进度：0/0", output_text)
        self.assertIn("成功下载0个文件，失败0个文件。", output_text)


if __name__ == "__main__":
    unittest.main()

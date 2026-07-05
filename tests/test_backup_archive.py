from __future__ import annotations

import unittest
from unittest.mock import patch

from nga_tools.backup.archive import PostHtml, _rewrite_image_links
from nga_tools.backup.floor_map import FloorLabels


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


if __name__ == "__main__":
    unittest.main()

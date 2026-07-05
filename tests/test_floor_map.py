from __future__ import annotations

import unittest
from unittest.mock import patch

from nga_tools.backup.floor_map import _page_post_refs, _scan_original_pages
from nga_tools.ngaclient.client import PageData


class FakeClient:
    def __init__(self, pages: dict[int, PageData]) -> None:
        self.pages = pages

    def get_page(self, tid: int, aid: None, page: int) -> PageData:
        return self.pages[page]


class FloorMapPagePostRefsTest(unittest.TestCase):
    def test_missing_result_remains_strict_by_default(self) -> None:
        with self.assertRaisesRegex(ValueError, "缺少帖子列表"):
            _page_post_refs({"result": None}, "作者页")

    def test_missing_result_can_be_treated_as_empty_page(self) -> None:
        with patch("builtins.print") as print_mock:
            refs = _page_post_refs(
                {"result": None},
                "原帖第2538页",
                allow_missing_posts=True,
            )

        self.assertEqual(refs, [])
        print_mock.assert_called_once()


class FloorMapOriginalScanTest(unittest.TestCase):
    def test_scan_original_pages_continues_after_null_result_page(self) -> None:
        client = FakeClient(
            {
                1: {"result": [{"pid": 1001, "lou": 1}]},
                2: {"result": None},
                3: {"result": [{"pid": 1003, "lou": 3}]},
            }
        )
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        with patch("builtins.print"):
            _scan_original_pages(
                client,
                123,
                [1, 2, 3],
                scanned_pages,
                seen_original_lous,
                {1001: [1], 1003: [2]},
                original_lou_by_author_lou,
                2,
            )

        self.assertEqual(scanned_pages, {1, 2, 3})
        self.assertEqual(seen_original_lous, {1, 3})
        self.assertEqual(original_lou_by_author_lou, {1: 1, 2: 3})


if __name__ == "__main__":
    unittest.main()

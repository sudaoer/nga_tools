from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from nga_tools.backup.floor_map import (
    _page_post_refs,
    _scan_original_pages,
    _scan_pending_original_pages,
)
from nga_tools.ngaclient.client import PageData, PidRedirectTarget


class FakeClient:
    def __init__(self, pages: dict[int, PageData]) -> None:
        self.pages = pages

    def get_page(self, tid: int, aid: None, page: int) -> PageData:
        return self.pages[page]


class FastLookupClient:
    def __init__(
        self,
        pages: dict[int, PageData],
        redirects: dict[int, PidRedirectTarget | None],
        page_count: int,
    ) -> None:
        self.pages = pages
        self.redirects = redirects
        self.page_count = page_count
        self.page_calls: list[int] = []
        self.page_count_calls = 0

    def get_pid_redirect_target(self, pid: int) -> PidRedirectTarget | None:
        return self.redirects.get(pid)

    def get_page_count(self, tid: int, aid: None) -> int:
        self.page_count_calls += 1
        return self.page_count

    def get_page(self, tid: int, aid: None, page: int) -> PageData:
        self.page_calls.append(page)
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

        with patch("builtins.print"), patch("sys.stdout", new_callable=io.StringIO):
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


class FloorMapFastLookupTest(unittest.TestCase):
    def test_pid_lookup_scans_each_located_page_once(self) -> None:
        client = FastLookupClient(
            pages={
                5: {
                    "result": [
                        {"pid": 1001, "lou": 81},
                        {"pid": 1002, "lou": 82},
                    ]
                }
            },
            redirects={
                1001: {"tid": 123, "page": 5},
                1002: {"tid": 123, "page": 5},
            },
            page_count=99,
        )
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        with patch("builtins.print"), patch("sys.stdout", new_callable=io.StringIO):
            _scan_pending_original_pages(
                client,
                123,
                [1, 2],
                {1: 1001, 2: 1002},
                scanned_pages,
                seen_original_lous,
                original_lou_by_author_lou,
                2,
            )

        self.assertEqual(client.page_calls, [5])
        self.assertEqual(client.page_count_calls, 0)
        self.assertEqual(scanned_pages, {5})
        self.assertEqual(seen_original_lous, {81, 82})
        self.assertEqual(original_lou_by_author_lou, {1: 81, 2: 82})

    def test_tid_mismatch_falls_back_to_original_page_scan(self) -> None:
        client = FastLookupClient(
            pages={1: {"result": [{"pid": 1001, "lou": 3}]}},
            redirects={1001: {"tid": 999, "page": 5}},
            page_count=1,
        )
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        with patch("builtins.print"), patch("sys.stdout", new_callable=io.StringIO):
            _scan_pending_original_pages(
                client,
                123,
                [1],
                {1: 1001},
                scanned_pages,
                seen_original_lous,
                original_lou_by_author_lou,
                1,
            )

        self.assertEqual(client.page_calls, [1])
        self.assertEqual(client.page_count_calls, 1)
        self.assertEqual(scanned_pages, {1})
        self.assertEqual(seen_original_lous, {3})
        self.assertEqual(original_lou_by_author_lou, {1: 3})


if __name__ == "__main__":
    unittest.main()

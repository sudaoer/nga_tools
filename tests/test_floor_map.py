from __future__ import annotations

import io
import json
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    build_and_save_floor_map,
    find_missing_author_lous,
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
                None,
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
                None,
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
                None,
                original_lou_by_author_lou,
                1,
            )

        self.assertEqual(client.page_calls, [1])
        self.assertEqual(client.page_count_calls, 1)
        self.assertEqual(scanned_pages, {1})
        self.assertEqual(seen_original_lous, {3})
        self.assertEqual(original_lou_by_author_lou, {1: 3})

    def test_pid_zero_falls_back_to_page_scan(self) -> None:
        client = FastLookupClient(
            pages={1: {"result": [{"pid": 0, "lou": 0}]}},
            redirects={},
            page_count=1,
        )
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        with patch("builtins.print"), patch("sys.stdout", new_callable=io.StringIO):
            _scan_pending_original_pages(
                client,
                123,
                [0],
                {0: 0},
                scanned_pages,
                seen_original_lous,
                None,
                original_lou_by_author_lou,
                1,
            )

        self.assertEqual(client.page_calls, [1])
        self.assertEqual(original_lou_by_author_lou, {0: 0})


class FloorMapMissingInferenceTest(unittest.TestCase):
    def _build_floor_map(
        self,
        page_result: list[dict[str, object]],
        missing_author_lous: list[int],
    ) -> tuple[dict[str, object], FloorMapBuildResult]:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]
        client = FastLookupClient(
            pages={1: {"result": page_result}},
            redirects={
                1001: {"tid": 123, "page": 1},
                1003: {"tid": 123, "page": 1},
            },
            page_count=1,
        )

        with TemporaryDirectory() as temp_dir:
            with (
                patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = build_and_save_floor_map(
                    client,
                    123,
                    456,
                    author_posts,
                    missing_author_lous,
                )
            with open(f"{temp_dir}/floor_map.json", encoding="utf-8") as file:
                floor_map = json.load(file)

        return floor_map, result

    def test_single_anonymous_original_post_recovers_missing_author_lou(self) -> None:
        floor_map, result = self._build_floor_map(
            [
                {
                    "pid": 1001,
                    "lou": 10,
                    "author": {"uid": 42},
                    "content": "known before",
                },
                {
                    "pid": 2002,
                    "lou": 11,
                    "author": {"uid": -1},
                    "content": "anonymous body",
                },
                {
                    "pid": 1003,
                    "lou": 12,
                    "author": {"uid": 42},
                    "content": "known after",
                },
            ],
            [2],
        )

        self.assertEqual(result.floor_labels.original_lou_by_author_lou[2], 11)
        self.assertEqual(
            result.recovered_missing_posts_by_author_lou[2],
            {"original_pid": 2002, "original_lou": 11, "content": "anonymous body"},
        )
        self.assertIn(
            {
                "pid": None,
                "author_lou": 2,
                "original_lou": 11,
                "original_pid": 2002,
            },
            floor_map["entries"],
        )

    def test_deleted_original_post_still_maps_without_recovered_content(self) -> None:
        _floor_map, result = self._build_floor_map(
            [
                {
                    "pid": 1001,
                    "lou": 10,
                    "author": {"uid": 42},
                    "content": "known before",
                },
                {
                    "pid": 1003,
                    "lou": 12,
                    "author": {"uid": 42},
                    "content": "known after",
                },
            ],
            [2],
        )

        self.assertEqual(result.floor_labels.original_lou_by_author_lou[2], 11)
        self.assertEqual(result.recovered_missing_posts_by_author_lou, {})

    def test_ambiguous_anonymous_candidates_are_not_exactly_mapped(self) -> None:
        _floor_map, result = self._build_floor_map(
            [
                {
                    "pid": 1001,
                    "lou": 10,
                    "author": {"uid": 42},
                    "content": "known before",
                },
                {
                    "pid": 2002,
                    "lou": 11,
                    "author": {"uid": -1},
                    "content": "first anonymous",
                },
                {
                    "pid": 2003,
                    "lou": 12,
                    "author": {"uid": -1},
                    "content": "second anonymous",
                },
                {
                    "pid": 1003,
                    "lou": 13,
                    "author": {"uid": 42},
                    "content": "known after",
                },
            ],
            [2],
        )

        self.assertNotIn(2, result.floor_labels.original_lou_by_author_lou)
        self.assertEqual(
            result.floor_labels.candidate_original_lous_by_author_lou[2],
            [11, 12],
        )


class FloorMapMissingAuthorLousTest(unittest.TestCase):
    def test_finds_gaps_while_accepting_zero_floor(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 0, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]

        self.assertEqual(find_missing_author_lous(author_posts), [2])


if __name__ == "__main__":
    unittest.main()

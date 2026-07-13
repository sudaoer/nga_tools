from __future__ import annotations

import pytest
import io
from collections.abc import Callable, Sequence
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    find_missing_author_lous,
    load_floor_map_build_result_if_current,
    read_author_posts_from_archive,
    read_unresolved_missing_author_lous_from_archive,
    _page_post_dicts,
    _scan_original_pages,
)
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    OriginalPostSnapshot,
    StoredFloorMap,
)
from nga_tools.config import get_config
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import use_timing_log


class FakeClient:
    def __init__(self, pages: dict[int, PageData], page_count: int | None = None) -> None:
        self.pages = pages
        self.page_count = page_count if page_count is not None else max(pages)
        self.page_calls: list[int] = []
        self.page_count_calls = 0

    def get_page_count(self, tid: int, aid: None) -> int:
        self.page_count_calls += 1
        return self.page_count

    def get_page(self, tid: int, aid: None, page: int) -> PageData:
        self.page_calls.append(page)
        return self.pages[page]

    def get_pages(
        self,
        tid: int,
        aid: None,
        pages: Sequence[int],
        *,
        on_page_complete: Callable[[int, int, int], None] | None = None,
    ) -> dict[int, PageData]:
        result: dict[int, PageData] = {}
        total = len(pages)
        for completed, page in enumerate(pages, start=1):
            result[page] = self.get_page(tid, aid, page)
            if on_page_complete is not None:
                on_page_complete(page, completed, total)
        return result


class FloorMapPagePostRefsTest:
    def test_missing_result_remains_strict_by_default(self) -> None:
        with pytest.raises(ValueError, match='缺少帖子列表'):
            _page_post_dicts({"result": None}, "作者页")

    def test_missing_result_can_be_treated_as_empty_page(self) -> None:
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            refs = _page_post_dicts(
                {"result": None},
                "原帖第2538页",
                allow_missing_posts=True,
            )

        assert refs == []
        assert '警告：原帖第2538页 缺少帖子列表' in output.getvalue()


class FloorMapBackupSourceTest:
    def test_read_author_posts_uses_archive_store(self) -> None:
        with TemporaryDirectory() as temp_dir:
            ThreadArchiveStore(Path(temp_dir)).upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"pid": 1001, "lou": 1, "content": "from archive"},
                    ],
                },
            )

            with patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir):
                author_posts = read_author_posts_from_archive(123, 456)

        assert author_posts == [{'pid': 1001, 'author_lou': 1}]

    def test_read_author_posts_requires_archive_store(self) -> None:
        with TemporaryDirectory() as temp_dir:
            json_dir = Path(temp_dir) / "json"
            json_dir.mkdir()
            (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")

            with patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir):
                with pytest.raises(RuntimeError, match='缺少archive.sqlite3'):
                    read_author_posts_from_archive(123, 456)


class FloorMapOriginalScanTest:
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

        assert scanned_pages == {1, 2, 3}
        assert seen_original_lous == {1, 3}
        assert original_lou_by_author_lou == {1: 1, 2: 3}

    def test_scan_original_pages_records_batch_fetch_timing(
        self,
        tmp_path: Path,
    ) -> None:
        client = FakeClient({1: {"result": []}})
        timing_path = tmp_path / "floor-map.timing.log"

        with use_timing_log(timing_path, task_name="floor map") as timing_log:
            assert timing_log is not None
            with patch("builtins.print"):
                _scan_original_pages(
                    client,
                    123,
                    [1],
                    set(),
                    set(),
                    None,
                    {},
                    {},
                    0,
                )

        timing_text = timing_log.path.read_text(encoding="utf-8")
        assert "阶段：原帖页面并发抓取，开始时间：" in timing_text
        assert "阶段：原帖页面并发抓取，结束时间：" in timing_text
        assert "指标：原帖页面抓取页数，值：1\n" in timing_text
        assert "指标：原帖页面抓取并发上限，值：1\n" in timing_text

    def test_scan_original_pages_applies_out_of_order_responses_in_page_order(
        self,
    ) -> None:
        class ReverseCompletionClient(FakeClient):
            def get_pages(
                self,
                tid: int,
                aid: None,
                pages: Sequence[int],
                *,
                on_page_complete: Callable[[int, int, int], None] | None = None,
            ) -> dict[int, PageData]:
                result: dict[int, PageData] = {}
                reversed_pages = list(reversed(pages))
                for completed, page in enumerate(reversed_pages, start=1):
                    self.page_calls.append(page)
                    result[page] = self.pages[page]
                    if on_page_complete is not None:
                        on_page_complete(page, completed, len(reversed_pages))
                return result

        client = ReverseCompletionClient(
            {
                1: {"result": [{"pid": 1001, "lou": 10}]},
                2: {"result": [{"pid": 1001, "lou": 20}]},
            }
        )
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        with patch("builtins.print"):
            _scan_original_pages(
                client,
                123,
                [1, 2],
                scanned_pages,
                seen_original_lous,
                None,
                {1001: [1]},
                original_lou_by_author_lou,
                1,
            )

        assert client.page_calls == [2, 1]
        assert scanned_pages == {1, 2}
        assert seen_original_lous == {10, 20}
        assert original_lou_by_author_lou == {1: 20}

    def test_scan_original_pages_does_not_apply_partial_failed_batch(self) -> None:
        class FailingBatchClient(FakeClient):
            def get_pages(
                self,
                tid: int,
                aid: None,
                pages: Sequence[int],
                *,
                on_page_complete: Callable[[int, int, int], None] | None = None,
            ) -> dict[int, PageData]:
                del tid, aid, pages, on_page_complete
                raise RuntimeError("page fetch failed")

        client = FailingBatchClient({1: {"result": []}})
        scanned_pages = {9}
        seen_original_lous = {90}
        original_posts_by_lou: dict[int, OriginalPostSnapshot] = {
            90: {
                "pid": 9000,
                "lou": 90,
                "author_uid": 9,
                "content": "existing",
                "raw_post": {"pid": 9000, "lou": 90},
            }
        }
        original_lou_by_author_lou = {9: 90}

        with patch("builtins.print"):
            with pytest.raises(RuntimeError, match="page fetch failed"):
                _scan_original_pages(
                    client,
                    123,
                    [1, 2],
                    scanned_pages,
                    seen_original_lous,
                    original_posts_by_lou,
                    {},
                    original_lou_by_author_lou,
                    0,
                )

        assert scanned_pages == {9}
        assert seen_original_lous == {90}
        assert set(original_posts_by_lou) == {90}
        assert original_lou_by_author_lou == {9: 90}


class FloorMapMissingInferenceTest:
    def _build_floor_map(
        self,
        page_result: list[dict[str, object]],
        missing_author_lous: list[int],
    ) -> tuple[StoredFloorMap, FloorMapBuildResult]:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]
        client = FakeClient(
            pages={1: {"result": page_result}},
            page_count=1,
        )

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = build_and_save_floor_map(
                    client,
                    store,
                    123,
                    456,
                    author_posts,
                    missing_author_lous,
                )
            floor_map = store.read_floor_map()
            assert floor_map is not None

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

        assert result.floor_labels.original_lou_by_author_lou[2] == 11
        assert result.recovered_missing_posts_by_author_lou[2] == {
            "original_pid": 2002,
            "original_lou": 11,
            "content": "anonymous body",
            "raw_post": {
                "pid": 2002,
                "lou": 11,
                "author": {"uid": -1},
                "content": "anonymous body",
            },
        }
        assert {
            "pid": None,
            "author_lou": 2,
            "original_lou": 11,
            "original_pid": 2002,
        } in floor_map.entries

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

        assert result.floor_labels.original_lou_by_author_lou[2] == 11
        assert result.recovered_missing_posts_by_author_lou == {}

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

        assert 2 not in result.floor_labels.original_lou_by_author_lou
        assert result.floor_labels.candidate_original_lous_by_author_lou[2] == [11, 12]


class FloorMapSignatureCacheTest:
    def test_missing_cache_check_does_not_create_thread_folder(self) -> None:
        output_dir = Path(get_config().output_dir)
        store = ThreadArchiveStore(output_dir / "123_456")

        cached = load_floor_map_build_result_if_current(store, [], [])

        assert cached is None
        assert not (output_dir / "123_456").exists()

    def test_current_input_signature_loads_cached_floor_map(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1002, "author_lou": 2},
        ]
        client = FakeClient(
            pages={
                1: {
                    "result": [
                        {"pid": 1001, "lou": 10, "author": {"uid": 42}},
                        {"pid": 1002, "lou": 11, "author": {"uid": 42}},
                    ]
                }
            },
            page_count=1,
        )

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                build_and_save_floor_map(
                    client,
                    store,
                    123,
                    456,
                    author_posts,
                    [],
                )
                cached = load_floor_map_build_result_if_current(
                    store,
                    author_posts,
                    [],
                )
                changed = load_floor_map_build_result_if_current(
                    store,
                    author_posts,
                    [3],
                )

        assert cached is not None
        assert cached.floor_labels.original_lou_by_author_lou[1] == 10
        assert changed is None

    def test_legacy_floor_map_json_is_ignored(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
        ]

        with TemporaryDirectory() as temp_dir:
            legacy_path = Path(temp_dir) / "floor_map.json"
            legacy_path.write_text("{bad", encoding="utf-8")
            store = ThreadArchiveStore(Path(temp_dir))
            cached = load_floor_map_build_result_if_current(store, author_posts, [])
            legacy_text = legacy_path.read_text(encoding="utf-8")

        assert cached is None
        assert legacy_text == "{bad"


class FloorMapMissingAuthorLousTest:
    def test_finds_gaps_while_accepting_zero_floor(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 0, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]

        assert find_missing_author_lous(author_posts) == [2]

    def test_treats_total_lou_count_as_vrows_count(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1000, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]

        assert find_missing_author_lous(author_posts, total_lou_count=4) == [2]

    def test_does_not_create_missing_lou_for_vrows_count_itself(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1000, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
            {"pid": 1002, "author_lou": 2},
            {"pid": 1003, "author_lou": 3},
        ]

        assert find_missing_author_lous(author_posts, total_lou_count=4) == []

    def test_finds_tail_gap_before_max_valid_lou(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1000, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
        ]

        assert find_missing_author_lous(author_posts, total_lou_count=4) == [2, 3]


class FloorMapStoredMissingAuthorLousTest:
    def test_reads_only_unresolved_missing_lous_from_archive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            store.replace_floor_map(
                StoredFloorMap(
                    version=FLOOR_MAP_VERSION,
                    generation_version=FLOOR_MAP_GENERATION_VERSION,
                    algorithm=FLOOR_MAP_HASH_ALGORITHM,
                    tid=123,
                    aid=456,
                    input_signature="fixture",
                    entries=[
                        {"pid": 1001, "author_lou": 1, "original_lou": 1},
                        {"pid": None, "author_lou": 2, "original_lou": 2},
                        {"pid": None, "author_lou": 4, "original_lou": 4},
                        {
                            "pid": None,
                            "author_lou": 6,
                            "original_lou": 6,
                            "original_pid": 6006,
                        },
                        {"pid": None, "author_lou": 8, "original_lou": 8},
                        {"pid": None, "author_lou": 9, "original_lou": 9},
                    ],
                )
            )

            missing_lous = read_unresolved_missing_author_lous_from_archive(
                store,
                present_lous={4},
                total_lou_count=8,
            )

        assert missing_lous == [2]

from __future__ import annotations

import pytest
import io
from collections.abc import Callable, Generator, Sequence
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
    read_unresolved_missing_author_lous_from_archive,
    recover_exact_missing_posts_from_original_pages,
    unresolved_missing_author_lous_from_stored_floor_map,
    _page_post_dicts,
    _scan_pending_author_pages,
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
from nga_tools.ngaclient.client import PageData, PidRedirectTarget
from nga_tools.timing import use_timing_log


class FakeClient:
    def __init__(
        self,
        pages: dict[int, PageData],
        page_count: int | None = None,
        pid_targets: dict[int, PidRedirectTarget | None] | None = None,
    ) -> None:
        self.pages = pages
        self.page_count = page_count if page_count is not None else max(pages)
        self.pid_targets = {} if pid_targets is None else pid_targets
        self.page_calls: list[int] = []
        self.page_count_calls = 0
        self.pid_calls: list[int] = []

    def get_page_count(self, tid: int, aid: None) -> int:
        self.page_count_calls += 1
        return self.page_count

    def get_page(self, tid: int, aid: None, page: int) -> PageData:
        self.page_calls.append(page)
        return self.pages[page]

    def get_pid_redirect_target(self, pid: int) -> PidRedirectTarget | None:
        self.pid_calls.append(pid)
        return self.pid_targets.get(pid)

    def get_pid_redirect_targets(
        self,
        pids: Sequence[int],
    ) -> dict[int, PidRedirectTarget | None]:
        return {pid: self.get_pid_redirect_target(pid) for pid in pids}

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

    def iter_pages(
        self,
        tid: int,
        aid: None,
        pages: Sequence[int],
        *,
        on_page_complete: Callable[[int, int, int], None] | None = None,
    ) -> Generator[tuple[int, PageData]]:
        page_data_by_page = self.get_pages(tid, aid, pages)
        ordered_pages = list(dict.fromkeys(pages))
        total = len(ordered_pages)
        for completed, page in enumerate(ordered_pages, start=1):
            if on_page_complete is not None:
                on_page_complete(page, completed, total)
            yield page, page_data_by_page[page]


class StreamingFakeClient(FakeClient):
    def iter_pages(
        self,
        tid: int,
        aid: None,
        pages: Sequence[int],
        *,
        on_page_complete: Callable[[int, int, int], None] | None = None,
    ) -> Generator[tuple[int, PageData]]:
        ordered_pages = list(dict.fromkeys(pages))
        total = len(ordered_pages)
        for completed, page in enumerate(ordered_pages, start=1):
            page_data = self.get_page(tid, aid, page)
            if on_page_complete is not None:
                on_page_complete(page, completed, total)
            yield page, page_data


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


class ExactMissingFloorRecoveryTest:
    def test_deduplicates_target_original_pages(self) -> None:
        client = FakeClient(
            {
                1: {
                    "result": [
                        {
                            "pid": 2011,
                            "lou": 11,
                            "author": {"uid": -1},
                            "content": "missing two",
                        },
                        {
                            "pid": 2012,
                            "lou": 12,
                            "author": {"uid": -1},
                            "content": "missing four",
                        },
                    ]
                }
            }
        )

        with (
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            recovered = recover_exact_missing_posts_from_original_pages(
                client,
                123,
                {2: 11, 4: 12},
            )

        assert client.page_calls == [1]
        assert set(recovered) == {2, 4}
        assert recovered[2]["original_pid"] == 2011
        assert recovered[4]["original_pid"] == 2012

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


class FloorMapPidTargetScanTest:
    @staticmethod
    def _scan(
        client: StreamingFakeClient,
        pid_to_author_lous: dict[int, list[int]],
        *,
        timing_path: Path | None = None,
        concurrency: int = 4,
    ) -> tuple[set[int], set[int], dict[int, int]]:
        scanned_pages: set[int] = set()
        seen_original_lous: set[int] = set()
        original_lou_by_author_lou: dict[int, int] = {}

        def run_scan() -> None:
            _scan_pending_author_pages(
                client,
                123,
                list(range(1, client.page_count + 1)),
                client.page_count,
                scanned_pages,
                seen_original_lous,
                {},
                pid_to_author_lous,
                original_lou_by_author_lou,
                sum(len(lous) for lous in pid_to_author_lous.values()),
            )

        with (
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
            patch(
                "nga_tools.backup.floor_map.get_api_concurrency",
                return_value=concurrency,
            ),
        ):
            if timing_path is None:
                run_scan()
            else:
                with use_timing_log(timing_path, task_name="pid jump"):
                    run_scan()
        return scanned_pages, seen_original_lous, original_lou_by_author_lou

    def test_request_bound_scans_range_without_pid_requests(self) -> None:
        client = StreamingFakeClient(
            {
                1: {
                    "result": [
                        {"pid": 1001, "lou": 10},
                        {"pid": 1002, "lou": 11},
                        {"pid": 1003, "lou": 12},
                    ]
                },
                2: {"result": []},
                3: {"result": []},
            }
        )

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            {1001: [1], 1002: [2], 1003: [3]},
            concurrency=1,
        )

        assert client.page_calls == [1, 2, 3]
        assert client.pid_calls == []
        assert scanned_pages == {1, 2, 3}
        assert mapped == {1: 10, 2: 11, 3: 12}

    def test_unlocatable_pid_falls_back_without_redirect_request(self) -> None:
        client = StreamingFakeClient(
            {
                1: {"result": [{"pid": 0, "lou": 0}]},
                2: {"result": []},
                3: {"result": []},
            }
        )

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            {0: [0]},
            concurrency=1,
        )

        assert client.pid_calls == []
        assert client.page_calls == [1, 2, 3]
        assert scanned_pages == {1, 2, 3}
        assert mapped == {0: 0}

    def test_zero_pid_topic_root_is_known_without_redirect(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 0, "author_lou": 0},
            {"pid": 1001, "author_lou": 1},
        ]
        client = StreamingFakeClient(
            {
                15: {
                    "result": [
                        {"pid": 1001, "lou": 280, "author": {"uid": 42}},
                    ]
                }
            },
            page_count=20,
            pid_targets={1001: PidRedirectTarget(tid=123, page_number=15)},
        )

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
                patch(
                    "nga_tools.backup.floor_map.get_api_concurrency",
                    return_value=2,
                ),
            ):
                result = build_and_save_floor_map(
                    client,
                    store,
                    123,
                    456,
                    author_posts,
                    [],
                )
            floor_map = store.floor_maps.read_floor_map()

        assert client.pid_calls == [1001]
        assert client.page_calls == [15]
        assert result.floor_labels.original_lou_by_author_lou == {
            0: 0,
            1: 280,
        }
        assert floor_map is not None
        assert floor_map.entries == [
            {"pid": 0, "author_lou": 0, "original_lou": 0},
            {"pid": 1001, "author_lou": 1, "original_lou": 280},
        ]

    def test_one_pid_target_page_recovers_every_pending_pid(
        self,
        tmp_path: Path,
    ) -> None:
        client = StreamingFakeClient(
            {
                15: {
                    "result": [
                        {"pid": 1001, "lou": 280},
                        {"pid": 1002, "lou": 281},
                    ]
                }
            },
            page_count=20,
            pid_targets={1002: PidRedirectTarget(tid=123, page_number=15)},
        )
        timing_path = tmp_path / "pid-target.timing.log"

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            {1001: [1], 1002: [2]},
            timing_path=timing_path,
            concurrency=1,
        )

        assert client.pid_calls == [1002]
        assert client.page_calls == [15]
        assert scanned_pages == {15}
        assert mapped == {1: 280, 2: 281}
        timing_logs = list(tmp_path.glob("pid-jump.timing-*.log"))
        if not timing_logs:
            timing_logs = list(tmp_path.glob("pid-target.timing-*.log"))
        assert len(timing_logs) == 1
        timing_text = timing_logs[0].read_text(encoding="utf-8")
        assert "指标：楼层映射PID定点原帖页数，值：1\n" in timing_text
        assert "指标：楼层映射回退顺扫原帖页数，值：0\n" in timing_text
        assert "指标：楼层映射PID定位请求数，值：1\n" in timing_text
        assert "指标：楼层映射PID定位命中数，值：1\n" in timing_text
        assert "指标：楼层映射本次恢复作者楼数，值：2\n" in timing_text
        assert "标签：楼层映射PID定位结果，值：none\n" in timing_text

    def test_spaced_batch_targets_distinct_pages_and_recovers_coauthors(
        self,
    ) -> None:
        pages: dict[int, PageData] = {}
        pid_targets: dict[int, PidRedirectTarget | None] = {}
        pid_to_author_lous: dict[int, list[int]] = {}
        for pair_index, page_number in enumerate((10, 20, 30, 40)):
            first_lou = pair_index * 2 + 1
            first_pid = 1000 + first_lou
            second_pid = first_pid + 1
            pages[page_number] = {
                "result": [
                    {"pid": first_pid, "lou": page_number * 20 - 2},
                    {"pid": second_pid, "lou": page_number * 20 - 1},
                ]
            }
            pid_targets[second_pid] = PidRedirectTarget(
                tid=123,
                page_number=page_number,
            )
            pid_to_author_lous[first_pid] = [first_lou]
            pid_to_author_lous[second_pid] = [first_lou + 1]
        client = StreamingFakeClient(
            pages,
            page_count=40,
            pid_targets=pid_targets,
        )

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            pid_to_author_lous,
            concurrency=4,
        )

        assert client.pid_calls == [1002, 1004, 1006, 1008]
        assert client.page_calls == [10, 20, 30, 40]
        assert scanned_pages == {10, 20, 30, 40}
        assert len(mapped) == 8

    def test_missing_redirect_falls_back_to_range(self) -> None:
        client = StreamingFakeClient(
            {
                1: {"result": [{"pid": 1001, "lou": 10}]},
                2: {"result": [{"pid": 9002, "lou": 20}]},
                3: {"result": [{"pid": 9003, "lou": 30}]},
                4: {"result": [{"pid": 1002, "lou": 60}]},
            },
        )

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            {1001: [1], 1002: [2]},
        )

        assert client.page_calls == [1, 2, 3, 4]
        assert client.pid_calls == [1001, 1002]
        assert scanned_pages == {1, 2, 3, 4}
        assert mapped == {1: 10, 2: 60}

    def test_probe_cost_falls_back_after_bounded_target_sample(self) -> None:
        pages: dict[int, PageData] = {
            page: {
                "result": (
                    [{"pid": 1000 + page, "lou": page * 20 - 1}]
                    if page <= 25
                    else []
                )
            }
            for page in range(1, 41)
        }
        client = StreamingFakeClient(
            pages,
            page_count=40,
            pid_targets={
                1000 + page: PidRedirectTarget(tid=123, page_number=page)
                for page in range(1, 26)
            },
        )
        pid_to_author_lous = {
            1000 + author_lou: [author_lou]
            for author_lou in range(1, 26)
        }

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            pid_to_author_lous,
            concurrency=1,
        )

        assert client.pid_calls == [1013, 1014]
        assert client.page_calls[:2] == [13, 14]
        assert set(client.page_calls) == set(range(1, 41))
        assert scanned_pages == set(range(1, 41))
        assert len(mapped) == 25

    def test_target_without_requested_pid_falls_back_and_preserves_other_hits(
        self,
    ) -> None:
        pages: dict[int, PageData] = {
                1: {"result": [{"pid": 1001, "lou": 10}]},
                2: {"result": [{"pid": 9002, "lou": 20}]},
                15: {"result": [{"pid": 1003, "lou": 280}]},
                16: {"result": [{"pid": 1002, "lou": 300}]},
            }
        pages.update(
            {page: {"result": []} for page in range(3, 15)}
        )
        client = StreamingFakeClient(
            pages,
            pid_targets={1002: PidRedirectTarget(tid=123, page_number=15)},
        )

        scanned_pages, _seen_lous, mapped = self._scan(
            client,
            {1001: [1], 1002: [2], 1003: [3]},
            concurrency=1,
        )

        assert client.page_calls == [15, *range(1, 15), 16]
        assert client.pid_calls == [1002]
        assert scanned_pages == set(range(1, 17))
        assert mapped == {1: 10, 2: 300, 3: 280}

    def test_failed_pid_target_scan_does_not_apply_partial_state(self) -> None:
        class FailingStreamingClient(StreamingFakeClient):
            def iter_pages(
                self,
                tid: int,
                aid: None,
                pages: Sequence[int],
                *,
                on_page_complete: (
                    Callable[[int, int, int], None] | None
                ) = None,
            ) -> Generator[tuple[int, PageData]]:
                page_number = pages[0]
                page_data = self.get_page(tid, aid, page_number)
                if on_page_complete is not None:
                    on_page_complete(page_number, 1, len(pages))
                yield page_number, page_data
                raise RuntimeError("target page fetch failed")

        client = FailingStreamingClient(
            {
                2: {"result": [{"pid": 1001, "lou": 20}]},
                3: {"result": [{"pid": 1002, "lou": 40}]},
            },
            page_count=4,
            pid_targets={
                1001: PidRedirectTarget(tid=123, page_number=2),
                1002: PidRedirectTarget(tid=123, page_number=3),
            },
        )
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

        with (
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
            patch(
                "nga_tools.backup.floor_map.get_api_concurrency",
                return_value=2,
            ),
            pytest.raises(RuntimeError, match="target page fetch failed"),
        ):
            _scan_pending_author_pages(
                client,
                123,
                [1, 2, 3, 4],
                4,
                scanned_pages,
                seen_original_lous,
                original_posts_by_lou,
                {1001: [1], 1002: [2]},
                original_lou_by_author_lou,
                3,
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
            floor_map = store.floor_maps.read_floor_map()
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

    def test_deferred_missing_floor_does_not_fetch_an_original_page(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]
        page_data: PageData = {
            "result": [
                {"pid": 1001, "lou": 10, "author": {"uid": 42}},
                {"pid": 1003, "lou": 12, "author": {"uid": 42}},
            ]
        }

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            initial_client = FakeClient({1: page_data}, page_count=1)
            deferred_client = FakeClient({1: page_data}, page_count=1)
            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                initial = build_and_save_floor_map(
                    initial_client,
                    store,
                    123,
                    456,
                    author_posts,
                    [2],
                )
                deferred = build_and_save_floor_map(
                    deferred_client,
                    store,
                    123,
                    456,
                    author_posts,
                    [2],
                    retry_missing_author_lous=(),
                )

        assert initial.floor_labels.original_lou_by_author_lou[2] == 11
        assert deferred.floor_labels.original_lou_by_author_lou[2] == 11
        assert deferred_client.page_count_calls == 0
        assert deferred_client.page_calls == []

    def test_rebuild_preserves_recovered_missing_floor_pid(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]
        page_data: PageData = {
            "result": [
                {"pid": 1001, "lou": 10, "author": {"uid": 42}},
                {
                    "pid": 2002,
                    "lou": 11,
                    "author": {"uid": -1},
                    "content": "anonymous body",
                },
                {"pid": 1003, "lou": 12, "author": {"uid": 42}},
            ]
        }

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.ensure_schema()
            initial_client = FakeClient({1: page_data}, page_count=1)
            deferred_client = FakeClient({1: page_data}, page_count=1)
            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                initial = build_and_save_floor_map(
                    initial_client,
                    store,
                    123,
                    456,
                    author_posts,
                    [2],
                )
                deferred = build_and_save_floor_map(
                    deferred_client,
                    store,
                    123,
                    456,
                    author_posts,
                    [2],
                    retry_missing_author_lous=(),
                )
            floor_map = store.floor_maps.read_floor_map()

        assert initial.recovered_missing_posts_by_author_lou[2][
            "original_pid"
        ] == 2002
        assert deferred.recovered_missing_posts_by_author_lou == {}
        assert deferred_client.page_count_calls == 0
        assert deferred_client.page_calls == []
        assert floor_map is not None
        assert floor_map.entries[1] == {
            "pid": None,
            "author_lou": 2,
            "original_lou": 11,
            "original_pid": 2002,
        }

    def test_already_fetched_page_can_recover_deferred_missing_floor(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
            {"pid": 1005, "author_lou": 5},
        ]
        client = FakeClient(
            pages={
                1: {
                    "result": [
                        {"pid": 1001, "lou": 10, "author": {"uid": 42}},
                        {
                            "pid": 2002,
                            "lou": 11,
                            "author": {"uid": -1},
                            "content": "deferred anonymous body",
                        },
                        {"pid": 1003, "lou": 12, "author": {"uid": 42}},
                        {"pid": 1005, "lou": 14, "author": {"uid": 42}},
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
                result = build_and_save_floor_map(
                    client,
                    store,
                    123,
                    456,
                    author_posts,
                    [2, 4],
                    retry_missing_author_lous=(4,),
                )

        assert result.floor_labels.original_lou_by_author_lou[2] == 11
        assert result.recovered_missing_posts_by_author_lou[2]["content"] == (
            "deferred anonymous body"
        )
        assert client.page_calls == [1]


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
            store.floor_maps.replace_floor_map(
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

    def test_filters_an_already_read_floor_map_without_store_access(self) -> None:
        stored_floor_map = StoredFloorMap(
            version=FLOOR_MAP_VERSION,
            generation_version=FLOOR_MAP_GENERATION_VERSION,
            algorithm=FLOOR_MAP_HASH_ALGORITHM,
            tid=123,
            aid=456,
            input_signature="fixture",
            entries=[
                {"pid": None, "author_lou": 2, "original_lou": 2},
                {"pid": None, "author_lou": 4, "original_lou": 4},
                {
                    "pid": None,
                    "author_lou": 6,
                    "original_lou": 6,
                    "original_pid": 6006,
                },
                {"pid": None, "author_lou": 8, "original_lou": 8},
            ],
        )

        missing_lous = unresolved_missing_author_lous_from_stored_floor_map(
            stored_floor_map,
            present_lous={4},
            total_lou_count=8,
        )

        assert missing_lous == [2]

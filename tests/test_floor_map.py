from __future__ import annotations

import pytest
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    find_missing_author_lous,
    generate_floor_map_from_backup,
    load_floor_map_build_result_if_current,
    read_author_posts_from_archive,
    read_unresolved_missing_author_lous_from_floor_map,
    _page_post_dicts,
    _scan_original_pages,
)
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.config import get_config
from nga_tools.ngaclient.client import PageData


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


class FloorMapMissingInferenceTest:
    def _build_floor_map(
        self,
        page_result: list[dict[str, object]],
        missing_author_lous: list[int],
    ) -> tuple[dict[str, object], FloorMapBuildResult]:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
            {"pid": 1003, "author_lou": 3},
        ]
        client = FakeClient(
            pages={1: {"result": page_result}},
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
        assert {'pid': None, 'author_lou': 2, 'original_lou': 11, 'original_pid': 2002} in floor_map['entries']

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

        cached = load_floor_map_build_result_if_current(123, 456, [], [])

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
            with (
                patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                build_and_save_floor_map(
                    client,
                    123,
                    456,
                    author_posts,
                    [],
                )
                cached = load_floor_map_build_result_if_current(
                    123,
                    456,
                    author_posts,
                    [],
                )
                changed = load_floor_map_build_result_if_current(
                    123,
                    456,
                    author_posts,
                    [3],
                )

        assert cached is not None
        assert cached.floor_labels.original_lou_by_author_lou[1] == 10
        assert changed is None

    def test_invalid_cached_floor_map_is_cache_miss(self) -> None:
        author_posts: list[AuthorPostRef] = [
            {"pid": 1001, "author_lou": 1},
        ]

        with TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "floor_map.json").write_text("{bad", encoding="utf-8")
            with (
                patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                cached = load_floor_map_build_result_if_current(
                    123,
                    456,
                    author_posts,
                    [],
                )

        assert cached is None


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
    def test_reads_only_unresolved_missing_lous_from_floor_map(self) -> None:
        with TemporaryDirectory() as temp_dir:
            (Path(temp_dir) / "floor_map.json").write_text(
                json.dumps(
                    {
                        "entries": [
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
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir):
                missing_lous = read_unresolved_missing_author_lous_from_floor_map(
                    123,
                    456,
                    present_lous={4},
                    total_lou_count=8,
                )

        assert missing_lous == [2]

    def test_generate_floor_map_from_backup_uses_archive_total_lou_count(
        self,
    ) -> None:
        captured_missing_lous: list[int] = []

        def fake_build_floor_map(
            client: object,
            tid: int,
            aid: int,
            author_posts: list[AuthorPostRef],
            missing_author_lous: list[int],
            *,
            strict: bool = True,
        ) -> FloorMapBuildResult:
            del client, tid, aid, author_posts, strict
            captured_missing_lous[:] = missing_author_lous
            return FloorMapBuildResult(FloorLabels.plain(), {})

        with TemporaryDirectory() as temp_dir:
            store = ThreadArchiveStore(Path(temp_dir))
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "vrows": 4,
                    "result": [
                        {"pid": 1000, "lou": 0, "content": "op"},
                        {"pid": 1001, "lou": 1, "content": "first"},
                        {"pid": 1003, "lou": 3, "content": "third"},
                    ],
                },
            )
            with (
                patch("nga_tools.backup.floor_map.utils.get_folder", return_value=temp_dir),
                patch(
                    "nga_tools.backup.floor_map.build_and_save_floor_map",
                    side_effect=fake_build_floor_map,
                ),
            ):
                generate_floor_map_from_backup(123, 456)

        assert captured_missing_lous == [2]

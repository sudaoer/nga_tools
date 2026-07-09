from __future__ import annotations

import pytest
import io
import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from typing import cast
from unittest.mock import patch

from nga_tools.cli import args_parse
from nga_tools.commands.forum import handle_forum_sync
from nga_tools.forum.export import (
    scan_postdate_forum_threads,
    sync_postdate_forum_threads_to_db,
)
from nga_tools.forum.thread_store import (
    ForumThreadStore,
    ForumThreadUpsertResult,
    forum_thread_db_path,
    forum_thread_table_name,
)
from nga_tools.forum.watch import (
    ForumWatchConfig,
    MatchedForumThread,
    build_thread_link,
    build_matched_thread,
    collect_matching_threads,
    collect_matching_threads_from_thread_source,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
    thread_matches_watch,
)
from nga_tools.ngaclient.client import ForumThread, ForumThreadPage, NGAForumPageError
from nga_tools.forum.thread_configs import ThreadConfig


def _thread(
    *,
    tid: int = 123,
    fid: int = 784,
    subject: str = "安价测试帖",
    author: str = "楼主",
    authorid: int = 456,
    postdate: int = 1000,
    lastpost: int = 2000,
    replies: int = 600,
) -> ForumThread:
    return {
        "tid": tid,
        "fid": fid,
        "subject": subject,
        "author": author,
        "authorid": authorid,
        "postdate": postdate,
        "lastpost": lastpost,
        "replies": replies,
        "forumname": "二次元跑团综合",
    }


def _watch(
    *,
    watch_name: str = "rp784",
    fid: int = 784,
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    include_tids: list[int] | None = None,
    min_replies: int = 500,
    min_author_lous: int = 20,
    pages: int = 1,
) -> ForumWatchConfig:
    return {
        "watch_name": watch_name,
        "fid": fid,
        "pages": pages,
        "min_replies": min_replies,
        "min_author_lous": min_author_lous,
        "keywords": [] if keywords is None else keywords,
        "exclude_keywords": [] if exclude_keywords is None else exclude_keywords,
        "include_tids": [] if include_tids is None else include_tids,
        "name_template": "{watch_name}-{tid}",
    }


class _FakeForumClient:
    def __init__(
        self,
        pages: dict[tuple[int, int], list[ForumThread]],
        *,
        author_pages: dict[tuple[int, int], dict[str, object]] | None = None,
        fail_on: tuple[int, int] | None = None,
    ) -> None:
        self._pages = pages
        self._author_pages = {} if author_pages is None else author_pages
        self._fail_on = fail_on
        self.base_url = "https://bbs.nga.cn"
        self.forum_fetches: list[tuple[int, int]] = []
        self.page_fetches: list[tuple[int, int | None, int]] = []

    def get_forum_threads(self, fid: int, page: int) -> list[ForumThread]:
        self.forum_fetches.append((fid, page))
        if self._fail_on == (fid, page):
            raise RuntimeError("forum fetch failed")
        return self._pages[(fid, page)]

    def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
        self.page_fetches.append((tid, aid, page))
        if aid is None:
            raise AssertionError("forum sync should fetch author-only pages")
        return self._author_pages.get((tid, aid), {"totalPage": 1, "vrows": 20})


class _FakeThreadConfigs:
    def __init__(self, thread_list: list[ThreadConfig] | None = None) -> None:
        self.ThreadList = [] if thread_list is None else thread_list
        self.saved = False

    def save_configs(self) -> None:
        self.saved = True


class _FakeForumThreadStore:
    db_path = Path("fake_forum_threads.sqlite3")

    def __init__(
        self,
        threads_by_fid: dict[int, list[ForumThread]] | None = None,
    ) -> None:
        self.upserts: list[tuple[int, list[int]]] = []
        self._threads_by_fid: dict[int, dict[int, ForumThread]] = {}
        if threads_by_fid is not None:
            for fid, threads in threads_by_fid.items():
                self._threads_by_fid[fid] = {
                    thread["tid"]: cast(ForumThread, thread.copy())
                    for thread in threads
                }

    def upsert_threads(
        self,
        fid: int,
        threads: list[ForumThread],
    ) -> ForumThreadUpsertResult:
        self.upserts.append((fid, [thread["tid"] for thread in threads]))
        stored_threads = self._threads_by_fid.setdefault(fid, {})
        inserted_count = 0
        updated_count = 0
        for thread in threads:
            if thread["tid"] in stored_threads:
                updated_count += 1
            else:
                inserted_count += 1
            stored_threads[thread["tid"]] = cast(ForumThread, thread.copy())
        return ForumThreadUpsertResult(
            inserted_count=inserted_count,
            updated_count=updated_count,
        )

    def list_threads(self, fid: int, *, forumname: str) -> list[ForumThread]:
        stored_threads = self._threads_by_fid.get(fid, {})
        return [
            cast(
                ForumThread,
                {
                    **thread,
                    "fid": fid,
                    "forumname": forumname,
                },
            )
            for thread in sorted(
                stored_threads.values(),
                key=lambda thread: (thread["lastpost"], thread["tid"]),
                reverse=True,
            )
        ]


class _FakePostdateClient:
    def __init__(
        self,
        pages: dict[tuple[int, int], ForumThreadPage],
        *,
        failures: dict[tuple[int, int], list[NGAForumPageError]] | None = None,
    ) -> None:
        self._pages = pages
        self._failures = {} if failures is None else failures
        self.thread_page_fetches: list[tuple[int, int, str | None]] = []

    def get_forum_thread_page(
        self,
        fid: int,
        page: int,
        *,
        order_by: str | None = None,
    ) -> ForumThreadPage:
        self.thread_page_fetches.append((fid, page, order_by))
        failures = self._failures.get((fid, page), [])
        if failures:
            raise failures.pop(0)
        return self._pages[(fid, page)]

    def get_page(self, tid: int, aid: int | None, page: int) -> dict[str, object]:
        raise AssertionError("postdate forum scan should not fetch thread pages")


def _forum_page(
    *,
    fid: int = 784,
    page: int = 1,
    total_page: int = 1,
    threads: list[ForumThread] | None = None,
) -> ForumThreadPage:
    page_threads = [] if threads is None else threads
    return {
        "fid": fid,
        "forumname": "二次元跑团综合",
        "current_page": page,
        "total_page": total_page,
        "per_page": 35,
        "total": len(page_threads),
        "threads": page_threads,
    }


class ForumWatchCliTest:
    def test_forum_sync_parses_full_postdate_args(self) -> None:
        args = args_parse(
            [
                "forum",
                "sync",
                "--full_postdate",
                "--fid",
                "784",
                "--refresh",
                "--start_page",
                "544",
                "--page_delay_seconds",
                "5",
            ]
        )

        assert args['full_postdate'] == True
        assert args['fid'] == 784
        assert args['refresh'] == True
        assert args['start_page'] == 544
        assert args['page_delay_seconds'] == 5

    def test_forum_sync_defaults_postdate_delay(self) -> None:
        args = args_parse(["forum", "sync", "--full_postdate", "--fid", "784"])

        assert args['page_delay_seconds'] == 3

    def test_forum_sync_rejects_non_positive_delay(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            pytest.raises(SystemExit),
        ):
            args_parse(
                [
                    "forum",
                    "sync",
                    "--full_postdate",
                    "--fid",
                    "784",
                    "--page_delay_seconds",
                    "0",
                ]
            )

    def test_forum_sync_rejects_non_positive_start_page(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            pytest.raises(SystemExit),
        ):
            args_parse(
                [
                    "forum",
                    "sync",
                    "--full_postdate",
                    "--refresh",
                    "--fid",
                    "784",
                    "--start_page",
                    "0",
                ]
            )

    def test_forum_sync_rejects_start_page_without_refresh(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            pytest.raises(SystemExit),
        ):
            args_parse(
                [
                    "forum",
                    "sync",
                    "--full_postdate",
                    "--fid",
                    "784",
                    "--start_page",
                    "2",
                ]
            )

    def test_forum_sync_rejects_removed_scan_output_arg(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            pytest.raises(SystemExit),
        ):
            args_parse(
                [
                    "forum",
                    "sync",
                    "--full_postdate",
                    "--fid",
                    "784",
                    "--scan_output",
                    "threads.jsonl",
                ]
            )

    def test_forum_sync_rejects_postdate_args_without_postdate_mode(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            pytest.raises(SystemExit),
        ):
            args_parse(["forum", "sync", "--fid", "784"])


class ForumWatchConfigTest:
    def test_loads_watch_config_with_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {"ForumWatchList": [{"watch_name": "rp784", "fid": 784}]}
                ),
                encoding="utf-8",
            )

            configs = load_forum_watch_configs(config_path)

        assert len(configs) == 1
        assert configs[0]['pages'] == 1
        assert configs[0]['min_replies'] == 500
        assert configs[0]['min_author_lous'] == 20
        assert configs[0]['keywords'] == []
        assert configs[0]['name_template'] == '{watch_name}-{tid}'

    def test_loads_custom_min_replies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ForumWatchList": [
                            {
                                "watch_name": "rp784",
                                "fid": 784,
                                "min_replies": 120,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            configs = load_forum_watch_configs(config_path)

        assert configs[0]['min_replies'] == 120

    def test_loads_custom_min_author_lous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ForumWatchList": [
                            {
                                "watch_name": "rp784",
                                "fid": 784,
                                "min_author_lous": 35,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            configs = load_forum_watch_configs(config_path)

        assert configs[0]['min_author_lous'] == 35

    def test_ignores_legacy_description_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ForumWatchList": [
                            {
                                "watch_name": "rp784",
                                "fid": 784,
                                "description_template": "{forumname}/{subject}",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            configs = load_forum_watch_configs(config_path)

        assert configs[0]['name_template'] == '{watch_name}-{tid}'

    @pytest.mark.parametrize("min_replies", [0, "500"])
    def test_rejects_invalid_min_replies(self, min_replies: object) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ForumWatchList": [
                            {
                                "watch_name": "rp784",
                                "fid": 784,
                                "min_replies": min_replies,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with pytest.raises(ValueError, match='min_replies'):
                load_forum_watch_configs(config_path)

    @pytest.mark.parametrize("min_author_lous", [0, -1, True, "20"])
    def test_rejects_invalid_min_author_lous(
        self,
        min_author_lous: object,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "forum_watch_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ForumWatchList": [
                            {
                                "watch_name": "rp784",
                                "fid": 784,
                                "min_author_lous": min_author_lous,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with pytest.raises(ValueError, match='min_author_lous'):
                load_forum_watch_configs(config_path)


class ForumWatchMatchTest:
    def test_matches_title_keyword_unless_excluded(self) -> None:
        assert thread_matches_watch(_thread(), _watch(keywords=['安价']))
        assert not thread_matches_watch(_thread(subject='[公告] 安价规则'), _watch(keywords=['安价'], exclude_keywords=['公告']))

    def test_include_tid_bypasses_exclude_keywords(self) -> None:
        matched = thread_matches_watch(
            _thread(tid=999, subject="[公告] 不应关键词匹配"),
            _watch(
                keywords=["安价"],
                exclude_keywords=["公告"],
                include_tids=[999],
            ),
        )

        assert matched

    def test_min_replies_filters_keyword_matches(self) -> None:
        watch = _watch(keywords=["安价"], min_replies=500)

        assert not thread_matches_watch(_thread(replies=499), watch)
        assert thread_matches_watch(_thread(replies=500), watch)

    def test_include_tid_bypasses_min_replies(self) -> None:
        matched = thread_matches_watch(
            _thread(tid=999, replies=1),
            _watch(keywords=["安价"], include_tids=[999], min_replies=500),
        )

        assert matched

    def test_build_matched_thread_renders_name_template(self) -> None:
        matched = build_matched_thread(
            {
                **_watch(),
                "name_template": "{watch_name}-{tid}-{authorid}",
            },
            _thread(tid=321, authorid=654, subject="安科测试帖"),
        )

        assert matched.thread_name == 'rp784-321-654'


class ForumWatchCollectTest:
    def test_collect_reports_page_progress(self) -> None:
        watch = _watch(keywords=["安价"], pages=2)
        client = _FakeForumClient(
            {
                (784, 1): [
                    _thread(tid=101, subject="安价一号"),
                    _thread(tid=102, subject="闲聊"),
                ],
                (784, 2): [_thread(tid=103, subject="安价二号")],
            }
        )
        progress_events = []

        scanned_count, matches = collect_matching_threads(
            client,
            [watch],
            progress_callback=progress_events.append,
        )

        assert scanned_count == 3
        assert [match.thread['tid'] for match in matches] == [101, 103]
        assert [(event.watch_name, event.fid, event.page, event.pages, event.scanned_count, event.matched_count) for event in progress_events] == [('rp784', 784, 1, 2, 2, 1), ('rp784', 784, 2, 2, 3, 2)]

    def test_collect_keeps_callback_optional(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})

        scanned_count, matches = collect_matching_threads(client, [watch])

        assert scanned_count == 1
        assert [match.thread['tid'] for match in matches] == [101]

    def test_collect_from_thread_source_does_not_fetch_forum_pages(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({})

        scanned_count, matches = collect_matching_threads_from_thread_source(
            client,
            [watch],
            lambda _watch_config: [_thread(tid=101, subject="安价一号")],
        )

        assert scanned_count == 1
        assert [match.thread['tid'] for match in matches] == [101]
        assert client.forum_fetches == []
        assert client.page_fetches == [(101, 456, 1)]

    def test_collect_filters_keyword_matches_by_min_author_lous(self) -> None:
        watch = _watch(keywords=["安价"], min_author_lous=20)
        client = _FakeForumClient(
            {
                (784, 1): [
                    _thread(tid=101, subject="安价一号"),
                    _thread(tid=102, subject="安价二号"),
                ]
            },
            author_pages={
                (101, 456): {"totalPage": 1, "vrows": 19},
                (102, 456): {"totalPage": 1, "vrows": 20},
            },
        )

        scanned_count, matches = collect_matching_threads(client, [watch])

        assert scanned_count == 2
        assert [match.thread['tid'] for match in matches] == [102]
        assert client.page_fetches == [(101, 456, 1), (102, 456, 1)]

    def test_collect_only_fetches_author_page_after_listing_rules_match(self) -> None:
        watch = _watch(keywords=["安价"], min_replies=500)
        client = _FakeForumClient(
            {
                (784, 1): [
                    _thread(tid=101, subject="闲聊", replies=600),
                    _thread(tid=102, subject="安价低回复", replies=499),
                ]
            }
        )

        scanned_count, matches = collect_matching_threads(client, [watch])

        assert scanned_count == 2
        assert matches == []
        assert client.page_fetches == []

    def test_include_tid_bypasses_min_author_lous(self) -> None:
        watch = _watch(keywords=["安价"], include_tids=[101], min_author_lous=20)
        client = _FakeForumClient(
            {
                (784, 1): [
                    _thread(tid=101, subject="[公告] 强制保存", replies=1),
                ]
            },
            author_pages={(101, 456): {"totalPage": 1, "vrows": 1}},
        )

        scanned_count, matches = collect_matching_threads(client, [watch])

        assert scanned_count == 1
        assert [match.thread['tid'] for match in matches] == [101]
        assert client.page_fetches == []

    @pytest.mark.parametrize(
        "page_data",
        [{"totalPage": 1}, {"totalPage": 1, "vrows": "20"}],
    )
    def test_collect_rejects_invalid_author_lou_count(
        self,
        page_data: dict[str, object],
    ) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient(
            {(784, 1): [_thread(tid=101, subject="安价一号")]},
            author_pages={(101, 456): page_data},
        )

        with pytest.raises(ValueError, match='tid=101.*vrows'):
            collect_matching_threads(client, [watch])

    def test_collect_skips_author_page_for_existing_thread_configs(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient(
            {
                (784, 1): [
                    _thread(tid=101, subject="安价已保存", authorid=456),
                    _thread(tid=102, subject="安价新主题", authorid=789),
                ]
            },
            author_pages={(102, 789): {"totalPage": 1, "vrows": 20}},
        )
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "existing",
                "tid": 101,
                "aid": 456,
                "description": "",
            }
        ]

        scanned_count, matches = collect_matching_threads(
            client,
            [watch],
            existing_thread_list=thread_list,
        )

        assert scanned_count == 2
        assert [match.thread['tid'] for match in matches] == [101, 102]
        assert client.page_fetches == [(102, 789, 1)]

    def test_collect_skips_author_page_for_name_conflicts(self) -> None:
        watch: ForumWatchConfig = {**_watch(keywords=["安价"]), "name_template": "same"}
        client = _FakeForumClient(
            {(784, 1): [_thread(tid=101, subject="安价冲突", authorid=456)]},
            author_pages={(101, 456): {"totalPage": 1, "vrows": 20}},
        )
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "same",
                "tid": 999,
                "aid": 999,
                "description": "",
            }
        ]

        scanned_count, matches = collect_matching_threads(
            client,
            [watch],
            existing_thread_list=thread_list,
        )

        assert scanned_count == 1
        assert [match.thread['tid'] for match in matches] == [101]
        assert client.page_fetches == []


class ForumWatchSyncTest:
    def test_sync_adds_new_thread_and_updates_existing_metadata(self) -> None:
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "old",
                "tid": 100,
                "aid": 200,
                "subject": "旧标题",
                "custom_note": "keep me",
            }
        ]
        matches = [
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=100, authorid=200, subject="新标题", replies=700),
                thread_name="rp784-100",
            ),
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=101, authorid=201),
                thread_name="rp784-101",
            ),
        ]

        outcomes = sync_matches_to_thread_list(
            thread_list,
            matches,
            base_url="https://bbs.nga.cn",
        )

        assert [outcome.status for outcome in outcomes] == ['updated', 'added']
        assert len(thread_list) == 2
        assert thread_list[0]['thread_name'] == 'old'
        assert thread_list[0]['subject'] == '新标题'
        assert thread_list[0]['replies'] == 700
        assert thread_list[0]['custom_note'] == 'keep me'
        assert thread_list[0]['link'] == build_thread_link('https://bbs.nga.cn', 100)
        assert thread_list[1]['thread_name'] == 'rp784-101'
        assert thread_list[1]['aid'] == 201
        assert thread_list[1]['link'] == build_thread_link('https://bbs.nga.cn', 101)

    def test_sync_skips_existing_exact_match_when_metadata_is_current(self) -> None:
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "existing",
                "tid": 100,
                "aid": 200,
                "link": build_thread_link("https://bbs.nga.cn", 100),
                "subject": "安价测试帖",
                "author": "楼主",
                "fid": 784,
                "forumname": "二次元跑团综合",
                "replies": 600,
                "postdate": 1000,
                "lastpost": 2000,
            }
        ]
        matches = [
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=100, authorid=200),
                thread_name="rp784-100",
            )
        ]

        outcomes = sync_matches_to_thread_list(
            thread_list,
            matches,
            base_url="https://bbs.nga.cn",
        )

        assert [outcome.status for outcome in outcomes] == ['skipped']
        assert thread_list[0]['thread_name'] == 'existing'

    def test_sync_reports_tid_or_name_conflicts_without_adding(self) -> None:
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "same-name",
                "tid": 100,
                "aid": 200,
                "description": "",
            }
        ]
        matches = [
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=100, authorid=201),
                thread_name="rp784-100",
            ),
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=101, authorid=201),
                thread_name="same-name",
            ),
        ]

        outcomes = sync_matches_to_thread_list(
            thread_list,
            matches,
            base_url="https://bbs.nga.cn",
        )

        assert [outcome.status for outcome in outcomes] == ['conflict', 'conflict']
        assert len(thread_list) == 1


class ForumThreadStoreTest:
    def test_default_forum_thread_db_path_is_under_output_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config = type("Config", (), {"output_dir": tmp_dir})()

            with patch("nga_tools.forum.thread_store.get_config", return_value=config):
                db_path = forum_thread_db_path()

        assert db_path == Path(tmp_dir) / 'forum_threads.sqlite3'

    def test_upsert_uses_tid_primary_key_and_updates_thread_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "forum_threads.sqlite3"
            store = ForumThreadStore(db_path)
            table_name = forum_thread_table_name(784)

            first_result = store.upsert_threads(
                784,
                [
                    _thread(
                        tid=101,
                        subject="旧标题",
                        author="旧作者",
                        authorid=201,
                        lastpost=2000,
                        replies=10,
                    )
                ],
            )
            second_result = store.upsert_threads(
                784,
                [
                    _thread(
                        tid=101,
                        subject="新标题",
                        author="新作者",
                        authorid=202,
                        lastpost=3000,
                        replies=20,
                    )
                ],
            )

            with closing(sqlite3.connect(db_path)) as connection:
                row = connection.execute(
                    f"""
                    SELECT tid, aid, author, subject, lastpost, replies
                    FROM {table_name}
                    """
                ).fetchone()
                columns = [
                    column[1]
                    for column in connection.execute(
                        f"PRAGMA table_info({table_name})"
                    ).fetchall()
                ]

        assert first_result.inserted_count == 1
        assert first_result.updated_count == 0
        assert second_result.inserted_count == 0
        assert second_result.updated_count == 1
        assert row == (101, 202, '新作者', '新标题', 3000, 20)
        assert 'fid' not in columns
        assert 'forumname' not in columns
        assert 'page' not in columns
        assert 'page_index' not in columns
        assert 'pageindex' not in columns

    def test_list_threads_returns_rows_with_injected_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ForumThreadStore(Path(tmp_dir) / "forum_threads.sqlite3")
            store.upsert_threads(
                784,
                [
                    _thread(tid=101, subject="较早回复", authorid=201, lastpost=2000),
                    _thread(tid=102, subject="较新回复", authorid=202, lastpost=3000),
                ],
            )

            threads = store.list_threads(784, forumname="rp784")

        assert [thread['tid'] for thread in threads] == [102, 101]
        assert threads[0]['fid'] == 784
        assert threads[0]['forumname'] == 'rp784'
        assert threads[0]['authorid'] == 202
        assert threads[0]['subject'] == '较新回复'


class ForumPostdateScanTest:
    def test_scan_writes_jsonl_without_thread_page_fetches(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(
                    page=1,
                    total_page=2,
                    threads=[_thread(tid=101, subject="旧帖一号", authorid=201)],
                ),
                (784, 2): _forum_page(
                    page=2,
                    total_page=2,
                    threads=[_thread(tid=102, subject="旧帖二号", authorid=202)],
                ),
            }
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "threads.jsonl"
            result = scan_postdate_forum_threads(
                client,
                fids=[784],
                output_path=output_path,
                page_delay_seconds=3,
                sleep_func=sleeps.append,
            )
            records = [
                json.loads(line)
                for line in output_path.read_text(encoding="utf-8").splitlines()
            ]

        assert result.page_count == 2
        assert result.thread_count == 2
        assert client.thread_page_fetches == [(784, 1, 'postdatedesc'), (784, 2, 'postdatedesc')]
        assert sleeps == [3]
        assert records[0]['tid'] == 101
        assert records[0]['aid'] == 201
        assert records[0]['subject'] == '旧帖一号'
        assert records[0]['page'] == 1
        assert records[0]['page_index'] == 1
        assert records[0]['postdate'] == 1000
        assert isinstance(records[0]['postdate_text'], str)

    def test_scan_starts_from_requested_page(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 2): _forum_page(
                    page=2,
                    total_page=3,
                    threads=[_thread(tid=102, subject="第二页", authorid=202)],
                ),
                (784, 3): _forum_page(
                    page=3,
                    total_page=3,
                    threads=[_thread(tid=103, subject="第三页", authorid=203)],
                ),
            }
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = scan_postdate_forum_threads(
                client,
                fids=[784],
                output_path=Path(tmp_dir) / "threads.jsonl",
                start_page=2,
                page_delay_seconds=3,
                sleep_func=sleeps.append,
            )

        assert result.page_count == 2
        assert result.thread_count == 2
        assert client.thread_page_fetches == [(784, 2, 'postdatedesc'), (784, 3, 'postdatedesc')]
        assert sleeps == [3]

    def test_scan_deduplicates_fids_in_order(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(fid=784, threads=[]),
                (785, 1): _forum_page(fid=785, threads=[]),
            }
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = scan_postdate_forum_threads(
                client,
                fids=[784, 784, 785],
                output_path=Path(tmp_dir) / "threads.jsonl",
                page_delay_seconds=3,
                sleep_func=sleeps.append,
            )

        assert result.fids == [784, 785]
        assert client.thread_page_fetches == [(784, 1, 'postdatedesc'), (785, 1, 'postdatedesc')]
        assert sleeps == [3]

    def test_scan_retries_refresh_too_fast_errors(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(threads=[]),
            },
            failures={
                (784, 1): [
                    NGAForumPageError(2048, "刷新过快 请等候数秒再行访问")
                ]
            },
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            result = scan_postdate_forum_threads(
                client,
                fids=[784],
                output_path=Path(tmp_dir) / "threads.jsonl",
                page_delay_seconds=3,
                sleep_func=sleeps.append,
            )

        assert result.page_count == 1
        assert client.thread_page_fetches == [(784, 1, 'postdatedesc'), (784, 1, 'postdatedesc')]
        assert sleeps == [10]

    def test_scan_does_not_retry_over_limit_errors(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(threads=[]),
            },
            failures={
                (784, 1): [
                    NGAForumPageError(
                        2048,
                        "超过限制,只有在使用 [单一版面主题发布时间排序] 时可翻阅超过100页",
                    )
                ]
            },
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            with pytest.raises(NGAForumPageError, match='超过限制'):
                scan_postdate_forum_threads(
                    client,
                    fids=[784],
                    output_path=Path(tmp_dir) / "threads.jsonl",
                    page_delay_seconds=3,
                    sleep_func=sleeps.append,
                )

        assert client.thread_page_fetches == [(784, 1, 'postdatedesc')]
        assert sleeps == []

    def test_scan_failure_keeps_previous_jsonl_output(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(
                    page=1,
                    total_page=2,
                    threads=[_thread(tid=101, subject="第一页", authorid=201)],
                ),
            },
            failures={
                (784, 2): [NGAForumPageError(500, "server error")],
            },
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            output_path = Path(tmp_dir) / "threads.jsonl"
            output_path.write_text("old\n", encoding="utf-8")

            with pytest.raises(NGAForumPageError, match="server error"):
                scan_postdate_forum_threads(
                    client,
                    fids=[784],
                    output_path=output_path,
                    page_delay_seconds=3,
                    sleep_func=lambda _seconds: None,
                )

            output_text = output_path.read_text(encoding="utf-8")
            temp_paths = list(Path(tmp_dir).glob(".threads.jsonl.*.tmp"))

        assert output_text == "old\n"
        assert temp_paths == []


class ForumPostdateDbSyncTest:
    def test_db_sync_stops_after_updating_existing_tid(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(
                    page=1,
                    total_page=2,
                    threads=[
                        _thread(tid=101, subject="新帖", authorid=201),
                        _thread(
                            tid=102,
                            subject="已保存的新标题",
                            authorid=202,
                            lastpost=3000,
                            replies=20,
                        ),
                    ],
                ),
                (784, 2): _forum_page(
                    page=2,
                    total_page=2,
                    threads=[_thread(tid=103, subject="不应抓取")],
                ),
            }
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "forum_threads.sqlite3"
            store = ForumThreadStore(db_path)
            store.upsert_threads(
                784,
                [
                    _thread(
                        tid=102,
                        subject="已保存的旧标题",
                        authorid=202,
                        lastpost=2000,
                        replies=10,
                    )
                ],
            )
            result = sync_postdate_forum_threads_to_db(
                client,
                fids=[784],
                store=store,
                page_delay_seconds=3,
                sleep_func=sleeps.append,
            )
            table_name = forum_thread_table_name(784)
            with closing(sqlite3.connect(db_path)) as connection:
                rows = connection.execute(
                    f"SELECT tid, subject, lastpost, replies FROM {table_name} "
                    "ORDER BY tid"
                ).fetchall()

        assert client.thread_page_fetches == [(784, 1, 'postdatedesc')]
        assert sleeps == []
        assert result.page_count == 1
        assert result.thread_count == 2
        assert result.inserted_count == 1
        assert result.updated_count == 1
        assert result.stopped_existing_count == 1
        assert rows == [(101, '新帖', 2000, 600), (102, '已保存的新标题', 3000, 20)]

    def test_db_sync_refresh_ignores_existing_tid_and_scans_to_last_page(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(
                    page=1,
                    total_page=2,
                    threads=[
                        _thread(tid=101, subject="新帖", authorid=201),
                        _thread(tid=102, subject="已保存的新标题", authorid=202),
                    ],
                ),
                (784, 2): _forum_page(
                    page=2,
                    total_page=2,
                    threads=[_thread(tid=103, subject="第二页")],
                ),
            }
        )
        sleeps: list[float] = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ForumThreadStore(Path(tmp_dir) / "forum_threads.sqlite3")
            store.upsert_threads(
                784,
                [_thread(tid=102, subject="已保存的旧标题", authorid=202)],
            )
            result = sync_postdate_forum_threads_to_db(
                client,
                fids=[784],
                store=store,
                page_delay_seconds=3,
                refresh=True,
                sleep_func=sleeps.append,
            )

        assert client.thread_page_fetches == [(784, 1, 'postdatedesc'), (784, 2, 'postdatedesc')]
        assert sleeps == [3]
        assert result.page_count == 2
        assert result.thread_count == 3
        assert result.inserted_count == 2
        assert result.updated_count == 1
        assert result.stopped_existing_count == 0


class ForumWatchCommandTest:
    def test_forum_sync_full_postdate_writes_database_only(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 1): _forum_page(
                    threads=[_thread(tid=101, subject="旧帖一号", authorid=201)]
                ),
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "forum_threads.sqlite3"
            store = ForumThreadStore(db_path)
            with (
                patch("nga_tools.commands.forum.configure_network_limits_from_args"),
                patch("nga_tools.commands.forum.NGAClient", return_value=client),
                patch("nga_tools.commands.forum.ForumThreadStore", return_value=store),
                patch(
                    "nga_tools.commands.forum.NGAThreadConfigs",
                    side_effect=AssertionError(
                        "full_postdate should not save thread configs"
                    ),
                ),
                patch("sys.stdout", new_callable=io.StringIO) as output,
            ):
                handle_forum_sync(
                    {
                        "full_postdate": True,
                        "fid": 784,
                        "page_delay_seconds": 3,
                    }
                )

            table_name = forum_thread_table_name(784)
            with closing(sqlite3.connect(db_path)) as connection:
                row = connection.execute(
                    f"SELECT tid, aid, subject FROM {table_name}"
                ).fetchone()

        assert row == (101, 201, '旧帖一号')
        assert client.thread_page_fetches == [(784, 1, 'postdatedesc')]
        output_text = output.getvalue()
        assert '发布时间扫描完成：fid=784，扫描1页，保存1个主题；新增1个，更新0个。' in output_text
        assert str(db_path) in output_text

    def test_forum_sync_full_postdate_accepts_start_page_with_refresh(self) -> None:
        client = _FakePostdateClient(
            {
                (784, 2): _forum_page(
                    page=2,
                    total_page=2,
                    threads=[_thread(tid=102, subject="第二页", authorid=202)],
                ),
            }
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            store = ForumThreadStore(Path(tmp_dir) / "forum_threads.sqlite3")
            with (
                patch("nga_tools.commands.forum.configure_network_limits_from_args"),
                patch("nga_tools.commands.forum.NGAClient", return_value=client),
                patch("nga_tools.commands.forum.ForumThreadStore", return_value=store),
                patch(
                    "nga_tools.commands.forum.NGAThreadConfigs",
                    side_effect=AssertionError(
                        "full_postdate should not save thread configs"
                    ),
                ),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                handle_forum_sync(
                    {
                        "full_postdate": True,
                        "fid": 784,
                        "refresh": True,
                        "start_page": 2,
                        "page_delay_seconds": 3,
                    }
                )

        assert client.thread_page_fetches == [(784, 2, 'postdatedesc')]

    def test_forum_sync_prints_progress_and_summary(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
        thread_configs = _FakeThreadConfigs()
        forum_store = _FakeForumThreadStore(
            {784: [_thread(tid=102, subject="安价库内主题")]}
        )

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch(
                "nga_tools.commands.forum.ForumThreadStore",
                return_value=forum_store,
            ),
            patch(
                "nga_tools.commands.forum.NGAThreadConfigs",
                return_value=thread_configs,
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            handle_forum_sync({})

        output_text = output.getvalue()
        assert '\r正在筛查数据库 rp784 fid=784，已扫描2个，匹配2个\n' in output_text
        assert '远端抓取1个主题，数据库新增1个，更新0个。' in output_text
        assert '数据库筛查2个主题，匹配2个；新增2个，更新0个，跳过0个，冲突0个。' in output_text
        assert '主题数据库：路径：fake_forum_threads.sqlite3' in output_text
        assert '[added] rp784-102 (tid=102, aid=456) - 已添加配置' in output_text
        assert '[added] rp784-101 (tid=101, aid=456) - 已添加配置' in output_text
        assert forum_store.upserts == [(784, [101])]
        assert thread_configs.saved

    def test_forum_sync_hides_skipped_result_details(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
        forum_store = _FakeForumThreadStore()
        thread_configs = _FakeThreadConfigs(
            [
                {
                    "thread_name": "existing",
                    "tid": 101,
                    "aid": 456,
                    "link": build_thread_link("https://bbs.nga.cn", 101),
                    "subject": "安价一号",
                    "author": "楼主",
                    "fid": 784,
                    "forumname": "rp784",
                    "replies": 600,
                    "postdate": 1000,
                    "lastpost": 2000,
                }
            ]
        )

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch(
                "nga_tools.commands.forum.ForumThreadStore",
                return_value=forum_store,
            ),
            patch(
                "nga_tools.commands.forum.NGAThreadConfigs",
                return_value=thread_configs,
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            handle_forum_sync({})

        output_text = output.getvalue()
        assert '远端抓取1个主题，数据库新增1个，更新0个。' in output_text
        assert '数据库筛查1个主题，匹配1个；新增0个，更新0个，跳过1个，冲突0个。' in output_text
        assert '[skipped]' not in output_text
        assert '已存在配置：existing' not in output_text
        assert forum_store.upserts == [(784, [101])]
        assert client.page_fetches == []
        assert not thread_configs.saved

    def test_forum_sync_updates_existing_metadata_and_saves(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
        forum_store = _FakeForumThreadStore()
        thread_configs = _FakeThreadConfigs(
            [
                {
                    "thread_name": "custom-name",
                    "tid": 101,
                    "aid": 456,
                    "custom_note": "keep me",
                }
            ]
        )

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch(
                "nga_tools.commands.forum.ForumThreadStore",
                return_value=forum_store,
            ),
            patch(
                "nga_tools.commands.forum.NGAThreadConfigs",
                return_value=thread_configs,
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            handle_forum_sync({})

        output_text = output.getvalue()
        assert '数据库筛查1个主题，匹配1个；新增0个，更新1个，跳过0个，冲突0个。' in output_text
        assert '已更新帖子数据：custom-name' in output_text
        assert thread_configs.ThreadList[0]['thread_name'] == 'custom-name'
        assert thread_configs.ThreadList[0]['custom_note'] == 'keep me'
        assert thread_configs.ThreadList[0]['subject'] == '安价一号'
        assert thread_configs.ThreadList[0]['link'] == build_thread_link('https://bbs.nga.cn', 101)
        assert client.page_fetches == []
        assert thread_configs.saved

    def test_forum_sync_fetches_max_pages_once_per_fid(self) -> None:
        watches = [
            _watch(watch_name="first", keywords=["甲"], pages=1),
            _watch(watch_name="second", keywords=["乙"], pages=2),
        ]
        client = _FakeForumClient(
            {
                (784, 1): [_thread(tid=101, subject="甲主题")],
                (784, 2): [_thread(tid=102, subject="乙主题")],
            }
        )
        forum_store = _FakeForumThreadStore()

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=watches,
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch(
                "nga_tools.commands.forum.ForumThreadStore",
                return_value=forum_store,
            ),
            patch(
                "nga_tools.commands.forum.NGAThreadConfigs",
                return_value=_FakeThreadConfigs(),
            ),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            handle_forum_sync({})

        assert client.forum_fetches == [(784, 1), (784, 2)]
        assert forum_store.upserts == [(784, [101]), (784, [102])]

    def test_forum_sync_finishes_progress_line_when_scan_fails(self) -> None:
        watch = _watch(keywords=["安价"], pages=2)
        client = _FakeForumClient(
            {
                (784, 1): [_thread(tid=101, subject="安价一号")],
            },
            fail_on=(784, 2),
        )
        forum_store = _FakeForumThreadStore()

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch(
                "nga_tools.commands.forum.ForumThreadStore",
                return_value=forum_store,
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            with pytest.raises(RuntimeError, match='forum fetch failed'):
                handle_forum_sync({})

        assert output.getvalue().endswith('\n')

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from nga_tools.cli import args_parse
from nga_tools.commands.forum import handle_forum_sync
from nga_tools.forum_export import (
    scan_postdate_forum_threads,
    sync_postdate_forum_threads_to_db,
)
from nga_tools.forum_threads_db import (
    ForumThreadStore,
    ForumThreadUpsertResult,
    forum_thread_table_name,
)
from nga_tools.forum_watch import (
    ForumWatchConfig,
    MatchedForumThread,
    build_matched_thread,
    collect_matching_threads,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
    thread_matches_watch,
)
from nga_tools.ngaclient.client import ForumThread, ForumThreadPage, NGAForumPageError
from nga_tools.thread_configs import ThreadConfig


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
    keywords: list[str] | None = None,
    exclude_keywords: list[str] | None = None,
    include_tids: list[int] | None = None,
    min_replies: int = 500,
    min_author_lous: int = 20,
    pages: int = 1,
) -> ForumWatchConfig:
    return {
        "watch_name": "rp784",
        "fid": 784,
        "pages": pages,
        "min_replies": min_replies,
        "min_author_lous": min_author_lous,
        "keywords": [] if keywords is None else keywords,
        "exclude_keywords": [] if exclude_keywords is None else exclude_keywords,
        "include_tids": [] if include_tids is None else include_tids,
        "name_template": "{watch_name}-{tid}",
        "description_template": "{forumname} | {author}: {subject}",
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
        self.page_fetches: list[tuple[int, int | None, int]] = []

    def get_forum_threads(self, fid: int, page: int) -> list[ForumThread]:
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

    def __init__(self) -> None:
        self.upserts: list[tuple[int, list[int]]] = []

    def upsert_threads(
        self,
        fid: int,
        threads: list[ForumThread],
    ) -> ForumThreadUpsertResult:
        self.upserts.append((fid, [thread["tid"] for thread in threads]))
        return ForumThreadUpsertResult(
            inserted_count=len(threads),
            updated_count=0,
        )


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


class ForumWatchCliTest(unittest.TestCase):
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

        self.assertEqual(args["full_postdate"], True)
        self.assertEqual(args["fid"], 784)
        self.assertEqual(args["refresh"], True)
        self.assertEqual(args["start_page"], 544)
        self.assertEqual(args["page_delay_seconds"], 5)

    def test_forum_sync_defaults_postdate_delay(self) -> None:
        args = args_parse(["forum", "sync", "--full_postdate", "--fid", "784"])

        self.assertEqual(args["page_delay_seconds"], 3)

    def test_forum_sync_rejects_non_positive_delay(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
            self.assertRaises(SystemExit),
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
            self.assertRaises(SystemExit),
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
            self.assertRaises(SystemExit),
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
            self.assertRaises(SystemExit),
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
            self.assertRaises(SystemExit),
        ):
            args_parse(["forum", "sync", "--fid", "784"])


class ForumWatchConfigTest(unittest.TestCase):
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

        self.assertEqual(len(configs), 1)
        self.assertEqual(configs[0]["pages"], 1)
        self.assertEqual(configs[0]["min_replies"], 500)
        self.assertEqual(configs[0]["min_author_lous"], 20)
        self.assertEqual(configs[0]["keywords"], [])
        self.assertEqual(configs[0]["name_template"], "{watch_name}-{tid}")

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

        self.assertEqual(configs[0]["min_replies"], 120)

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

        self.assertEqual(configs[0]["min_author_lous"], 35)

    def test_rejects_invalid_min_replies(self) -> None:
        for min_replies in [0, "500"]:
            with self.subTest(min_replies=min_replies):
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

                    with self.assertRaisesRegex(ValueError, "min_replies"):
                        load_forum_watch_configs(config_path)

    def test_rejects_invalid_min_author_lous(self) -> None:
        for min_author_lous in [0, -1, True, "20"]:
            with self.subTest(min_author_lous=min_author_lous):
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

                    with self.assertRaisesRegex(ValueError, "min_author_lous"):
                        load_forum_watch_configs(config_path)


class ForumWatchMatchTest(unittest.TestCase):
    def test_matches_title_keyword_unless_excluded(self) -> None:
        self.assertTrue(thread_matches_watch(_thread(), _watch(keywords=["安价"])))
        self.assertFalse(
            thread_matches_watch(
                _thread(subject="[公告] 安价规则"),
                _watch(keywords=["安价"], exclude_keywords=["公告"]),
            )
        )

    def test_include_tid_bypasses_exclude_keywords(self) -> None:
        matched = thread_matches_watch(
            _thread(tid=999, subject="[公告] 不应关键词匹配"),
            _watch(
                keywords=["安价"],
                exclude_keywords=["公告"],
                include_tids=[999],
            ),
        )

        self.assertTrue(matched)

    def test_min_replies_filters_keyword_matches(self) -> None:
        watch = _watch(keywords=["安价"], min_replies=500)

        self.assertFalse(thread_matches_watch(_thread(replies=499), watch))
        self.assertTrue(thread_matches_watch(_thread(replies=500), watch))

    def test_include_tid_bypasses_min_replies(self) -> None:
        matched = thread_matches_watch(
            _thread(tid=999, replies=1),
            _watch(keywords=["安价"], include_tids=[999], min_replies=500),
        )

        self.assertTrue(matched)

    def test_build_matched_thread_renders_templates(self) -> None:
        matched = build_matched_thread(
            {
                **_watch(),
                "name_template": "{watch_name}-{tid}-{authorid}",
                "description_template": "{forumname}/{author}/{subject}",
            },
            _thread(tid=321, authorid=654, subject="安科测试帖"),
        )

        self.assertEqual(matched.thread_name, "rp784-321-654")
        self.assertEqual(matched.description, "二次元跑团综合/楼主/安科测试帖")


class ForumWatchCollectTest(unittest.TestCase):
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

        self.assertEqual(scanned_count, 3)
        self.assertEqual([match.thread["tid"] for match in matches], [101, 103])
        self.assertEqual(
            [
                (
                    event.watch_name,
                    event.fid,
                    event.page,
                    event.pages,
                    event.scanned_count,
                    event.matched_count,
                )
                for event in progress_events
            ],
            [
                ("rp784", 784, 1, 2, 2, 1),
                ("rp784", 784, 2, 2, 3, 2),
            ],
        )

    def test_collect_keeps_callback_optional(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})

        scanned_count, matches = collect_matching_threads(client, [watch])

        self.assertEqual(scanned_count, 1)
        self.assertEqual([match.thread["tid"] for match in matches], [101])

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

        self.assertEqual(scanned_count, 2)
        self.assertEqual([match.thread["tid"] for match in matches], [102])
        self.assertEqual(client.page_fetches, [(101, 456, 1), (102, 456, 1)])

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

        self.assertEqual(scanned_count, 2)
        self.assertEqual(matches, [])
        self.assertEqual(client.page_fetches, [])

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

        self.assertEqual(scanned_count, 1)
        self.assertEqual([match.thread["tid"] for match in matches], [101])
        self.assertEqual(client.page_fetches, [])

    def test_collect_rejects_invalid_author_lou_count(self) -> None:
        for page_data in [{"totalPage": 1}, {"totalPage": 1, "vrows": "20"}]:
            with self.subTest(page_data=page_data):
                watch = _watch(keywords=["安价"])
                client = _FakeForumClient(
                    {(784, 1): [_thread(tid=101, subject="安价一号")]},
                    author_pages={(101, 456): page_data},
                )

                with self.assertRaisesRegex(ValueError, "tid=101.*vrows"):
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

        self.assertEqual(scanned_count, 2)
        self.assertEqual([match.thread["tid"] for match in matches], [101, 102])
        self.assertEqual(client.page_fetches, [(102, 789, 1)])

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

        self.assertEqual(scanned_count, 1)
        self.assertEqual([match.thread["tid"] for match in matches], [101])
        self.assertEqual(client.page_fetches, [])


class ForumWatchSyncTest(unittest.TestCase):
    def test_sync_adds_new_thread_and_skips_existing_exact_match(self) -> None:
        thread_list: list[ThreadConfig] = [
            {
                "thread_name": "old",
                "tid": 100,
                "aid": 200,
                "description": "",
            }
        ]
        matches = [
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=100, authorid=200),
                thread_name="rp784-100",
                description="existing",
            ),
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=101, authorid=201),
                thread_name="rp784-101",
                description="new",
            ),
        ]

        outcomes = sync_matches_to_thread_list(thread_list, matches)

        self.assertEqual([outcome.status for outcome in outcomes], ["skipped", "added"])
        self.assertEqual(len(thread_list), 2)
        self.assertEqual(thread_list[1]["thread_name"], "rp784-101")
        self.assertEqual(thread_list[1]["aid"], 201)

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
                description="tid conflict",
            ),
            MatchedForumThread(
                watch_name="rp784",
                thread=_thread(tid=101, authorid=201),
                thread_name="same-name",
                description="name conflict",
            ),
        ]

        outcomes = sync_matches_to_thread_list(thread_list, matches)

        self.assertEqual([outcome.status for outcome in outcomes], ["conflict", "conflict"])
        self.assertEqual(len(thread_list), 1)


class ForumThreadStoreTest(unittest.TestCase):
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

        self.assertEqual(first_result.inserted_count, 1)
        self.assertEqual(first_result.updated_count, 0)
        self.assertEqual(second_result.inserted_count, 0)
        self.assertEqual(second_result.updated_count, 1)
        self.assertEqual(row, (101, 202, "新作者", "新标题", 3000, 20))
        self.assertNotIn("fid", columns)
        self.assertNotIn("forumname", columns)
        self.assertNotIn("page", columns)
        self.assertNotIn("page_index", columns)
        self.assertNotIn("pageindex", columns)


class ForumPostdateScanTest(unittest.TestCase):
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

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.thread_count, 2)
        self.assertEqual(
            client.thread_page_fetches,
            [(784, 1, "postdatedesc"), (784, 2, "postdatedesc")],
        )
        self.assertEqual(sleeps, [3])
        self.assertEqual(records[0]["tid"], 101)
        self.assertEqual(records[0]["aid"], 201)
        self.assertEqual(records[0]["subject"], "旧帖一号")
        self.assertEqual(records[0]["page"], 1)
        self.assertEqual(records[0]["page_index"], 1)
        self.assertEqual(records[0]["postdate"], 1000)
        self.assertIsInstance(records[0]["postdate_text"], str)

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

        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.thread_count, 2)
        self.assertEqual(
            client.thread_page_fetches,
            [(784, 2, "postdatedesc"), (784, 3, "postdatedesc")],
        )
        self.assertEqual(sleeps, [3])

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

        self.assertEqual(result.fids, [784, 785])
        self.assertEqual(
            client.thread_page_fetches,
            [(784, 1, "postdatedesc"), (785, 1, "postdatedesc")],
        )
        self.assertEqual(sleeps, [3])

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

        self.assertEqual(result.page_count, 1)
        self.assertEqual(
            client.thread_page_fetches,
            [(784, 1, "postdatedesc"), (784, 1, "postdatedesc")],
        )
        self.assertEqual(sleeps, [10])

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
            with self.assertRaisesRegex(NGAForumPageError, "超过限制"):
                scan_postdate_forum_threads(
                    client,
                    fids=[784],
                    output_path=Path(tmp_dir) / "threads.jsonl",
                    page_delay_seconds=3,
                    sleep_func=sleeps.append,
                )

        self.assertEqual(client.thread_page_fetches, [(784, 1, "postdatedesc")])
        self.assertEqual(sleeps, [])


class ForumPostdateDbSyncTest(unittest.TestCase):
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

        self.assertEqual(client.thread_page_fetches, [(784, 1, "postdatedesc")])
        self.assertEqual(sleeps, [])
        self.assertEqual(result.page_count, 1)
        self.assertEqual(result.thread_count, 2)
        self.assertEqual(result.inserted_count, 1)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.stopped_existing_count, 1)
        self.assertEqual(
            rows,
            [
                (101, "新帖", 2000, 600),
                (102, "已保存的新标题", 3000, 20),
            ],
        )

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

        self.assertEqual(
            client.thread_page_fetches,
            [(784, 1, "postdatedesc"), (784, 2, "postdatedesc")],
        )
        self.assertEqual(sleeps, [3])
        self.assertEqual(result.page_count, 2)
        self.assertEqual(result.thread_count, 3)
        self.assertEqual(result.inserted_count, 2)
        self.assertEqual(result.updated_count, 1)
        self.assertEqual(result.stopped_existing_count, 0)


class ForumWatchCommandTest(unittest.TestCase):
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

        self.assertEqual(row, (101, 201, "旧帖一号"))
        self.assertEqual(client.thread_page_fetches, [(784, 1, "postdatedesc")])
        output_text = output.getvalue()
        self.assertIn(
            "发布时间扫描完成：fid=784，扫描1页，保存1个主题；新增1个，更新0个。",
            output_text,
        )
        self.assertIn(str(db_path), output_text)

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

        self.assertEqual(client.thread_page_fetches, [(784, 2, "postdatedesc")])

    def test_forum_sync_prints_progress_and_summary(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
        thread_configs = _FakeThreadConfigs()
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
            patch(
                "nga_tools.commands.forum.NGAThreadConfigs",
                return_value=thread_configs,
            ),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            handle_forum_sync({})

        output_text = output.getvalue()
        self.assertIn(
            "\r正在扫描 rp784 fid=784 第1/1页，已扫描1个，匹配1个\n",
            output_text,
        )
        self.assertIn("扫描1个主题，匹配1个；新增1个，跳过0个，冲突0个。", output_text)
        self.assertIn("主题数据库：新增1个，更新0个，路径：fake_forum_threads.sqlite3", output_text)
        self.assertIn("[added] rp784-101 (tid=101, aid=456) - 已添加配置", output_text)
        self.assertEqual(forum_store.upserts, [(784, [101])])
        self.assertTrue(thread_configs.saved)

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
                    "description": "",
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
        self.assertIn("扫描1个主题，匹配1个；新增0个，跳过1个，冲突0个。", output_text)
        self.assertNotIn("[skipped]", output_text)
        self.assertNotIn("已存在配置：existing", output_text)
        self.assertEqual(forum_store.upserts, [(784, [101])])
        self.assertEqual(client.page_fetches, [])
        self.assertFalse(thread_configs.saved)

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
            with self.assertRaisesRegex(RuntimeError, "forum fetch failed"):
                handle_forum_sync({})

        self.assertTrue(output.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()

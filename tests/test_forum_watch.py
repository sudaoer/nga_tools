from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from nga_tools.commands.forum import handle_forum_sync
from nga_tools.forum_watch import (
    ForumWatchConfig,
    MatchedForumThread,
    build_matched_thread,
    collect_matching_threads,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
    thread_matches_watch,
)
from nga_tools.ngaclient.client import ForumThread
from nga_tools.thread_configs import ThreadConfig


def _thread(
    *,
    tid: int = 123,
    subject: str = "安价测试帖",
    authorid: int = 456,
    replies: int = 600,
) -> ForumThread:
    return {
        "tid": tid,
        "fid": 784,
        "subject": subject,
        "author": "楼主",
        "authorid": authorid,
        "postdate": 1000,
        "lastpost": 2000,
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


class ForumWatchCommandTest(unittest.TestCase):
    def test_forum_sync_prints_progress_and_summary(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
        thread_configs = _FakeThreadConfigs()

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
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
        self.assertIn("[added] rp784-101 (tid=101, aid=456) - 已添加配置", output_text)
        self.assertTrue(thread_configs.saved)

    def test_forum_sync_hides_skipped_result_details(self) -> None:
        watch = _watch(keywords=["安价"])
        client = _FakeForumClient({(784, 1): [_thread(tid=101, subject="安价一号")]})
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

        with (
            patch(
                "nga_tools.commands.forum.load_forum_watch_configs",
                return_value=[watch],
            ),
            patch("nga_tools.commands.forum.configure_network_limits_from_args"),
            patch("nga_tools.commands.forum.NGAClient", return_value=client),
            patch("sys.stdout", new_callable=io.StringIO) as output,
        ):
            with self.assertRaisesRegex(RuntimeError, "forum fetch failed"):
                handle_forum_sync({})

        self.assertTrue(output.getvalue().endswith("\n"))


if __name__ == "__main__":
    unittest.main()

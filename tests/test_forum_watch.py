from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nga_tools.forum_watch import (
    ForumWatchConfig,
    MatchedForumThread,
    build_matched_thread,
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
) -> ForumWatchConfig:
    return {
        "watch_name": "rp784",
        "fid": 784,
        "pages": 1,
        "min_replies": min_replies,
        "keywords": [] if keywords is None else keywords,
        "exclude_keywords": [] if exclude_keywords is None else exclude_keywords,
        "include_tids": [] if include_tids is None else include_tids,
        "name_template": "{watch_name}-{tid}",
        "description_template": "{forumname} | {author}: {subject}",
    }


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


if __name__ == "__main__":
    unittest.main()

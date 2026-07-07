from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.stats.word_count import (
    clean_post_content,
    count_backup_words,
    count_chinese_text,
)


class WordCountCleaningTest(unittest.TestCase):
    def test_count_chinese_and_chinese_punctuation_separately(self) -> None:
        count = count_chinese_text("中文，test123。全角Ａ１！々")

        self.assertEqual(count.chinese_chars, 4)
        self.assertEqual(count.chinese_with_punctuation, 7)

    def test_removes_bbcode_html_images_links_mentions_and_emotes(self) -> None:
        content = (
            "[img]https://img.nga.178.com/a.jpg[/img]"
            "[url=https://example.com]链接文本[/url]"
            "<span class=\"red\"><b>正文</b></span>"
            "[s:ac:哭笑][@某人][uid=123]用户名[/uid]"
        )

        cleaned = clean_post_content(content)

        self.assertIn("链接文本", cleaned)
        self.assertIn("正文", cleaned)
        self.assertNotIn("https", cleaned)
        self.assertNotIn("哭笑", cleaned)
        self.assertNotIn("某人", cleaned)
        self.assertNotIn("用户名", cleaned)

    def test_removes_reply_quote_but_keeps_author_answer(self) -> None:
        content = (
            "[quote][pid=769626017,40811445,1]Reply[/pid] "
            "<b>Post by [uid=63074470]读者[/uid] (2024-07-09):</b><br/>"
            "被引用的问题[/quote]"
            "楼主自己的回答，应该保留。"
        )

        cleaned = clean_post_content(content)

        self.assertNotIn("被引用的问题", cleaned)
        self.assertIn("楼主自己的回答，应该保留。", cleaned)

    def test_keeps_visible_text_in_regular_quote(self) -> None:
        content = "[quote]<br/><b>[序章设定]</b><br/>这里是正文设定。[/quote]"

        cleaned = clean_post_content(content)

        self.assertIn("[序章设定]", cleaned)
        self.assertIn("这里是正文设定。", cleaned)

    def test_removes_html_reply_header_only(self) -> None:
        content = (
            "<b>Reply to [pid=769719632,40811445,2]Reply[/pid] "
            "Post by [uid=41814852]读者[/uid] (2024-07-09)</b>"
            "地点什么的不要细想，总之大家都在学校就可以了。"
        )

        cleaned = clean_post_content(content)

        self.assertNotIn("读者", cleaned)
        self.assertIn("地点什么的不要细想", cleaned)

    def test_removes_dice_expressions(self) -> None:
        content = (
            "[quote]<b> d=[1d100=49]=49 </b>[/quote]"
            ".r1d50+50=83 "
            "骰子后面的剧情正文，应该只留下中文。"
        )

        cleaned = clean_post_content(content)

        self.assertNotIn("1d100", cleaned)
        self.assertNotIn("r1d50", cleaned)
        self.assertIn("骰子后面的剧情正文", cleaned)

    def test_strips_bbcode_tags_with_library_parser(self) -> None:
        content = (
            "[size=12pt]字号正文[/size]"
            "[font=serif]字体正文[/font]"
            "[align=center]居中正文[/align]"
            "[collapse=标题]折叠正文[/collapse]"
            "[url=https://example.com]链接文本[/url]"
        )

        cleaned = clean_post_content(content)

        self.assertIn("字号正文", cleaned)
        self.assertIn("字体正文", cleaned)
        self.assertIn("居中正文", cleaned)
        self.assertIn("折叠正文", cleaned)
        self.assertIn("链接文本", cleaned)
        self.assertNotIn("[size", cleaned)
        self.assertNotIn("[font", cleaned)
        self.assertNotIn("[align", cleaned)
        self.assertNotIn("[collapse", cleaned)
        self.assertNotIn("https://example.com", cleaned)


class BackupWordCountTest(unittest.TestCase):
    def test_counts_only_posts_above_body_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_dir = Path(tmp_dir) / "123_456" / "json"
            json_dir.mkdir(parents=True)
            body_text = "正文，" * 40
            page_data = {
                "result": [
                    {"lou": 1, "content": "今日无更。"},
                    {"lou": 2, "content": body_text},
                ]
            }
            (json_dir / "page_1.json").write_text(
                json.dumps(page_data, ensure_ascii=False),
                encoding="utf-8",
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=tmp_dir),
            ):
                summary = count_backup_words(123, 456, min_body_chars=120)

        self.assertEqual(summary.page_count, 1)
        self.assertEqual(summary.total_posts, 2)
        self.assertEqual(summary.body_posts, 1)
        self.assertEqual(summary.excluded_posts, 1)
        self.assertEqual(summary.chinese_chars, 80)
        self.assertEqual(summary.chinese_with_punctuation, 120)

    def test_prefers_archive_store_over_latest_json_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_dir = Path(tmp_dir) / "123_456"
            json_dir = thread_dir / "json"
            json_dir.mkdir(parents=True)
            (json_dir / "page_1.json").write_text(
                json.dumps(
                    {
                        "result": [
                            {"lou": 2, "pid": 1002, "content": "新楼，"},
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = ThreadArchiveStore(thread_dir)
            store.upsert_page(
                1,
                {
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "旧楼，"},
                    ]
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "新楼，"},
                    ]
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=tmp_dir),
            ):
                summary = count_backup_words(123, 456, min_body_chars=1)

        self.assertEqual(summary.page_count, 1)
        self.assertEqual(summary.total_posts, 2)
        self.assertEqual(summary.body_posts, 2)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import pytest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.stats.word_count import (
    clean_post_content,
    count_backup_words,
    count_chinese_text,
)


class WordCountCleaningTest:
    def test_count_chinese_and_chinese_punctuation_separately(self) -> None:
        count = count_chinese_text("中文，test123。全角Ａ１！々")

        assert count.chinese_chars == 4
        assert count.chinese_with_punctuation == 7

    def test_removes_bbcode_html_images_links_mentions_and_emotes(self) -> None:
        content = (
            "[img]https://img.nga.178.com/a.jpg[/img]"
            "[url=https://example.com]链接文本[/url]"
            "<span class=\"red\"><b>正文</b></span>"
            "[s:ac:哭笑][@某人][uid=123]用户名[/uid]"
        )

        cleaned = clean_post_content(content)

        assert '链接文本' in cleaned
        assert '正文' in cleaned
        assert 'https' not in cleaned
        assert '哭笑' not in cleaned
        assert '某人' not in cleaned
        assert '用户名' not in cleaned

    def test_removes_reply_quote_but_keeps_author_answer(self) -> None:
        content = (
            "[quote][pid=769626017,40811445,1]Reply[/pid] "
            "<b>Post by [uid=63074470]读者[/uid] (2024-07-09):</b><br/>"
            "被引用的问题[/quote]"
            "楼主自己的回答，应该保留。"
        )

        cleaned = clean_post_content(content)

        assert '被引用的问题' not in cleaned
        assert '楼主自己的回答，应该保留。' in cleaned

    def test_keeps_visible_text_in_regular_quote(self) -> None:
        content = "[quote]<br/><b>[序章设定]</b><br/>这里是正文设定。[/quote]"

        cleaned = clean_post_content(content)

        assert '[序章设定]' in cleaned
        assert '这里是正文设定。' in cleaned

    def test_removes_html_reply_header_only(self) -> None:
        content = (
            "<b>Reply to [pid=769719632,40811445,2]Reply[/pid] "
            "Post by [uid=41814852]读者[/uid] (2024-07-09)</b>"
            "地点什么的不要细想，总之大家都在学校就可以了。"
        )

        cleaned = clean_post_content(content)

        assert '读者' not in cleaned
        assert '地点什么的不要细想' in cleaned

    def test_removes_dice_expressions(self) -> None:
        content = (
            "[quote]<b> d=[1d100=49]=49 </b>[/quote]"
            ".r1d50+50=83 "
            "骰子后面的剧情正文，应该只留下中文。"
        )

        cleaned = clean_post_content(content)

        assert '1d100' not in cleaned
        assert 'r1d50' not in cleaned
        assert '骰子后面的剧情正文' in cleaned

    def test_strips_bbcode_tags_with_library_parser(self) -> None:
        content = (
            "[size=12pt]字号正文[/size]"
            "[font=serif]字体正文[/font]"
            "[align=center]居中正文[/align]"
            "[collapse=标题]折叠正文[/collapse]"
            "[url=https://example.com]链接文本[/url]"
        )

        cleaned = clean_post_content(content)

        assert '字号正文' in cleaned
        assert '字体正文' in cleaned
        assert '居中正文' in cleaned
        assert '折叠正文' in cleaned
        assert '链接文本' in cleaned
        assert '[size' not in cleaned
        assert '[font' not in cleaned
        assert '[align' not in cleaned
        assert '[collapse' not in cleaned
        assert 'https://example.com' not in cleaned


class BackupWordCountTest:
    def test_counts_only_posts_above_body_threshold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_dir = Path(tmp_dir) / "123_456"
            body_text = "正文，" * 40
            ThreadArchiveStore(thread_dir).upsert_page(
                1,
                {
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "今日无更。"},
                        {"lou": 2, "pid": 1002, "content": body_text},
                    ]
                },
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=tmp_dir),
            ):
                summary = count_backup_words(123, 456, min_body_chars=120)

        assert summary.page_count == 1
        assert summary.archive_path == Path(tmp_dir) / '123_456' / 'archive.sqlite3'
        assert summary.total_posts == 2
        assert summary.body_posts == 1
        assert summary.excluded_posts == 1
        assert summary.chinese_chars == 80
        assert summary.chinese_with_punctuation == 120

    def test_uses_archive_store_even_when_latest_json_page_differs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            thread_dir = Path(tmp_dir) / "123_456"
            json_dir = thread_dir / "json"
            json_dir.mkdir(parents=True)
            (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")
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

        assert summary.page_count == 1
        assert summary.total_posts == 2
        assert summary.body_posts == 2

    def test_requires_archive_store_instead_of_falling_back_to_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            json_dir = Path(tmp_dir) / "123_456" / "json"
            json_dir.mkdir(parents=True)
            (json_dir / "page_1.json").write_text("{not json", encoding="utf-8")

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=tmp_dir),
            ):
                with pytest.raises(RuntimeError, match='缺少archive.sqlite3'):
                    count_backup_words(123, 456, min_body_chars=1)


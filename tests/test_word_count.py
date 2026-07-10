from __future__ import annotations

import warnings

from bs4 import MarkupResemblesLocatorWarning

from nga_tools.word_count import clean_post_content, count_chinese_text


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

    def test_plain_url_content_does_not_emit_markup_locator_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")

            cleaned = clean_post_content("https://example.com/path")

        assert cleaned == "https://example.com/path"
        assert not any(
            issubclass(warning.category, MarkupResemblesLocatorWarning)
            for warning in caught_warnings
        )

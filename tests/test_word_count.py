from __future__ import annotations

import warnings

from bs4 import MarkupResemblesLocatorWarning

from nga_tools.word_count import count_post_content


class WordCountCleaningTest:
    def test_count_chinese_and_chinese_punctuation_separately(self) -> None:
        count = count_post_content("中文，test123。全角Ａ１！々")

        assert count.chinese_chars == 4
        assert count.chinese_with_punctuation == 7

    def test_removes_bbcode_html_images_links_mentions_and_emotes(self) -> None:
        content = (
            "[img]https://img.nga.178.com/a.jpg[/img]"
            "[url=https://example.com]链接文本[/url]"
            "<span class=\"red\"><b>正文</b></span>"
            "[s:ac:哭笑][@某人][uid=123]用户名[/uid]"
        )

        assert count_post_content(content) == count_post_content("链接文本正文")

    def test_removes_reply_quote_but_keeps_author_answer(self) -> None:
        content = (
            "[quote][pid=769626017,40811445,1]Reply[/pid] "
            "<b>Post by [uid=63074470]读者[/uid] (2024-07-09):</b><br/>"
            "被引用的问题[/quote]"
            "楼主自己的回答，应该保留。"
        )

        assert count_post_content(content) == count_post_content(
            "楼主自己的回答，应该保留。"
        )

    def test_keeps_visible_text_in_regular_quote(self) -> None:
        content = "[quote]<br/><b>[序章设定]</b><br/>这里是正文设定。[/quote]"

        assert count_post_content(content) == count_post_content(
            "序章设定这里是正文设定。"
        )

    def test_removes_html_reply_header_only(self) -> None:
        content = (
            "<b>Reply to [pid=769719632,40811445,2]Reply[/pid] "
            "Post by [uid=41814852]读者[/uid] (2024-07-09)</b>"
            "地点什么的不要细想，总之大家都在学校就可以了。"
        )

        assert count_post_content(content) == count_post_content(
            "地点什么的不要细想，总之大家都在学校就可以了。"
        )

    def test_removes_dice_expressions(self) -> None:
        content = (
            "[quote]<b> d=[1d100=骰点结果] </b>[/quote]"
            ".r1d50+50=另一个骰点结果 "
            "骰子后面的剧情正文，应该只留下中文。"
        )

        assert count_post_content(content) == count_post_content(
            "骰子后面的剧情正文，应该只留下中文。"
        )

    def test_strips_bbcode_tags_with_library_parser(self) -> None:
        content = (
            "[size=12pt]字号正文[/size]"
            "[font=serif]字体正文[/font]"
            "[align=center]居中正文[/align]"
            "[collapse=标题]折叠正文[/collapse]"
            "[url=https://example.com]链接文本[/url]"
        )

        assert count_post_content(content) == count_post_content(
            "字号正文字体正文居中正文折叠正文链接文本"
        )

    def test_plain_url_content_does_not_emit_markup_locator_warning(self) -> None:
        with warnings.catch_warnings(record=True) as caught_warnings:
            warnings.simplefilter("always")

            count = count_post_content("https://example.com/path")

        assert count.chinese_chars == 0
        assert count.chinese_with_punctuation == 0
        assert not any(
            issubclass(warning.category, MarkupResemblesLocatorWarning)
            for warning in caught_warnings
        )

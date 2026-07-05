from __future__ import annotations

import unittest

from nga_tools.bbcode_convert import bbcode_to_html, strip_bbcode_tags


class BBCodeToHtmlTest(unittest.TestCase):
    def test_converts_supported_formatting_tags(self) -> None:
        content = (
            "[b]粗体[/b][i]斜体[/i][u]下划线[/u][s]删除[/s]"
            "[color=red]红色[/color][size=12pt]字号[/size]"
        )

        html = bbcode_to_html(content)

        self.assertEqual(
            html,
            '<strong>粗体</strong><em>斜体</em><u>下划线</u><del>删除</del>'
            '<span style="color:red">红色</span>'
            '<span style="font-size:12pt">字号</span>',
        )

    def test_converts_links_images_quotes_and_code(self) -> None:
        content = (
            "[url=https://example.com?a=1&b=2]链接[/url]"
            "[img]https://img.nga.178.com/a.jpg[/img]"
            "[quote]引用[/quote]"
            "[code]<x>[/code]"
        )

        html = bbcode_to_html(content)

        self.assertEqual(
            html,
            '<a href="https://example.com?a=1&b=2">链接</a>'
            '<img src="https://img.nga.178.com/a.jpg" alt="" />'
            "<blockquote>引用</blockquote>"
            "<pre><code><x></code></pre>",
        )

    def test_preserves_existing_html_and_plain_newlines(self) -> None:
        content = '<span class="red"><b>正文</b></span><br/>[b]粗体[/b]\n下一行'

        html = bbcode_to_html(content)

        self.assertEqual(
            html,
            '<span class="red"><b>正文</b></span><br/><strong>粗体</strong>\n下一行',
        )

    def test_handles_nested_tags_with_library_parser(self) -> None:
        content = "[quote]外层[b]粗体[size=12pt]字号[/size][/b][/quote]"

        html = bbcode_to_html(content)

        self.assertEqual(
            html,
            '<blockquote>外层<strong>粗体'
            '<span style="font-size:12pt">字号</span></strong></blockquote>',
        )


class BBCodeStripTest(unittest.TestCase):
    def test_strips_known_tags_but_keeps_text(self) -> None:
        content = (
            "[url=https://example.com]链接[/url]"
            "[size=12pt]字号[/size]"
            "[font=serif]字体[/font]"
            "[collapse=标题]折叠内容[/collapse]"
        )

        text = strip_bbcode_tags(content)

        self.assertEqual(text, "链接字号字体折叠内容")

    def test_keeps_non_bbcode_square_bracket_text(self) -> None:
        text = strip_bbcode_tags("[quote][序章设定]正文[/quote]")

        self.assertEqual(text, "[序章设定]正文")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from typing import Protocol, cast

import bbcode

_Formatter = Callable[[str, str, dict[str, str], object | None, dict[str, object]], str]


class _BBCodeParser(Protocol):
    def add_formatter(
        self,
        tag_name: str,
        render_func: _Formatter,
        **kwargs: object,
    ) -> None: ...

    def add_simple_formatter(
        self,
        tag_name: str,
        format_string: str,
        **kwargs: object,
    ) -> None: ...

    def format(self, data: str, **context: object) -> str: ...

    def strip(self, data: str, strip_newlines: bool = False) -> str: ...


_ParserFactory = Callable[..., _BBCodeParser]
_PARSER_FACTORY = cast(_ParserFactory, bbcode.Parser)
_INLINE_OPTIONS: dict[str, object] = {
    "escape_html": False,
    "replace_links": False,
    "replace_cosmetic": False,
}


def _first_option(options: dict[str, str], tag_name: str) -> str:
    if tag_name in options:
        return options[tag_name]
    if options:
        return next(iter(options.keys()))
    return ""


def _render_url(
    _name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    href = options.get("url", value)
    return f'<a href="{href}">{value}</a>'


def _render_img(
    _name: str,
    value: str,
    _options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    return f'<img src="{value}" alt="" />'


def _render_color(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    color = _first_option(options, name)
    return f'<span style="color:{color}">{value}</span>'


def _render_size(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    size = _first_option(options, name)
    return f'<span style="font-size:{size}">{value}</span>'


def _new_parser() -> _BBCodeParser:
    return _PARSER_FACTORY(
        newline="\n",
        install_defaults=False,
        escape_html=False,
        replace_links=False,
        replace_cosmetic=False,
    )


def _install_html_formatters(parser: _BBCodeParser) -> None:
    parser.add_simple_formatter("b", "<strong>%(value)s</strong>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("i", "<em>%(value)s</em>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("u", "<u>%(value)s</u>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("s", "<del>%(value)s</del>", **_INLINE_OPTIONS)
    parser.add_simple_formatter(
        "quote",
        "<blockquote>%(value)s</blockquote>",
        **_INLINE_OPTIONS,
    )
    parser.add_simple_formatter(
        "code",
        "<pre><code>%(value)s</code></pre>",
        **_INLINE_OPTIONS,
    )
    parser.add_formatter("url", _render_url, **_INLINE_OPTIONS)
    parser.add_formatter("img", _render_img, **_INLINE_OPTIONS)
    parser.add_formatter("color", _render_color, **_INLINE_OPTIONS)
    parser.add_formatter("size", _render_size, **_INLINE_OPTIONS)


def _install_strip_formatters(parser: _BBCodeParser) -> None:
    _install_html_formatters(parser)
    for tag_name in (
        "align",
        "center",
        "collapse",
        "del",
        "font",
        "list",
        "pid",
        "uid",
    ):
        parser.add_simple_formatter(tag_name, "%(value)s", **_INLINE_OPTIONS)
    parser.add_simple_formatter("*", "%(value)s", standalone=True, **_INLINE_OPTIONS)


@lru_cache(maxsize=1)
def _html_parser() -> _BBCodeParser:
    parser = _new_parser()
    _install_html_formatters(parser)
    return parser


@lru_cache(maxsize=1)
def _strip_parser() -> _BBCodeParser:
    parser = _new_parser()
    _install_strip_formatters(parser)
    return parser


def bbcode_to_html(text: str) -> str:
    return _html_parser().format(text)


def strip_bbcode_tags(text: str) -> str:
    return _strip_parser().strip(text)

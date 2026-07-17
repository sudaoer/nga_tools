from __future__ import annotations

from functools import lru_cache
from html import escape
from typing import cast

from nga_tools.bbcode_parser import (
    BBCodeParser,
    IMAGE_SRC_RESOLVER_CONTEXT_KEY,
    INLINE_OPTIONS,
    ImageSrcResolver,
    first_option,
    new_parser,
)


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
    context: dict[str, object],
) -> str:
    resolver = context.get(IMAGE_SRC_RESOLVER_CONTEXT_KEY)
    if callable(resolver):
        resolved_value = cast(ImageSrcResolver, resolver)(value)
        if resolved_value is None:
            return escape(f"[img]{value}[/img]", quote=False)
        value = resolved_value

    return f'<img src="{value}" alt="" />'


def _render_color(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    color = first_option(options, name)
    return f'<span style="color:{color}">{value}</span>'


def _render_size(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    size = first_option(options, name)
    return f'<span style="font-size:{size}">{value}</span>'


def _install_html_formatters(parser: BBCodeParser) -> None:
    parser.add_simple_formatter("b", "<strong>%(value)s</strong>", **INLINE_OPTIONS)
    parser.add_simple_formatter("i", "<em>%(value)s</em>", **INLINE_OPTIONS)
    parser.add_simple_formatter("u", "<u>%(value)s</u>", **INLINE_OPTIONS)
    parser.add_simple_formatter("s", "<del>%(value)s</del>", **INLINE_OPTIONS)
    parser.add_simple_formatter(
        "quote",
        "<blockquote>%(value)s</blockquote>",
        **INLINE_OPTIONS,
    )
    parser.add_simple_formatter(
        "code",
        "<pre><code>%(value)s</code></pre>",
        **INLINE_OPTIONS,
    )
    parser.add_formatter("url", _render_url, **INLINE_OPTIONS)
    parser.add_formatter("img", _render_img, **INLINE_OPTIONS)
    parser.add_formatter("color", _render_color, **INLINE_OPTIONS)
    parser.add_formatter("size", _render_size, **INLINE_OPTIONS)


def _install_strip_formatters(parser: BBCodeParser) -> None:
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
        parser.add_simple_formatter(tag_name, "%(value)s", **INLINE_OPTIONS)
    parser.add_simple_formatter("*", "%(value)s", standalone=True, **INLINE_OPTIONS)


@lru_cache(maxsize=1)
def _html_parser() -> BBCodeParser:
    parser = new_parser()
    _install_html_formatters(parser)
    return parser


@lru_cache(maxsize=1)
def _strip_parser() -> BBCodeParser:
    parser = new_parser()
    _install_strip_formatters(parser)
    return parser


def bbcode_to_html(
    text: str,
    *,
    image_src_resolver: ImageSrcResolver | None = None,
) -> str:
    if image_src_resolver is None:
        return _html_parser().format(text)
    return _html_parser().format(
        text,
        **{IMAGE_SRC_RESOLVER_CONTEXT_KEY: image_src_resolver},
    )


def strip_bbcode_tags(text: str) -> str:
    return _strip_parser().strip(text)

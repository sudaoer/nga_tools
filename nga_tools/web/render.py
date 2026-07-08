from __future__ import annotations

from collections.abc import Callable
from functools import lru_cache
from html import escape
from typing import Protocol, cast

import bbcode  # type: ignore

ImageSrcResolver = Callable[[str], str | None]

_Formatter = Callable[[str, str, dict[str, str], object | None, dict[str, object]], str]
_IMAGE_SRC_RESOLVER_CONTEXT_KEY = "image_src_resolver"
_INLINE_OPTIONS: dict[str, object] = {
    "escape_html": False,
    "replace_links": False,
    "replace_cosmetic": False,
}


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


_ParserFactory = Callable[..., _BBCodeParser]
_PARSER_FACTORY = cast(_ParserFactory, bbcode.Parser)


def _new_parser() -> _BBCodeParser:
    return _PARSER_FACTORY(
        newline="\n",
        install_defaults=False,
        escape_html=False,
        replace_links=False,
        replace_cosmetic=False,
    )


def _first_option(options: dict[str, str], tag_name: str) -> str:
    if tag_name in options:
        return options[tag_name]
    if options:
        return next(iter(options.keys()))
    return ""


def _attr(value: str) -> str:
    return escape(value, quote=True)


def _render_url(
    _name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    href = options.get("url") or _first_option(options, "url") or value
    return (
        f'<a href="{_attr(href)}" target="_blank" rel="noopener noreferrer">'
        f"{value}</a>"
    )


def _render_pid(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    raw_value = _first_option(options, name)
    pid = raw_value.split(",", 1)[0].strip()
    if not pid.isdigit():
        return value
    return (
        f'<a href="https://bbs.nga.cn/read.php?pid={pid}&amp;opt=128" '
        f'target="_blank" rel="noopener noreferrer">{value}</a>'
    )


def _render_uid(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    uid = _first_option(options, name).strip()
    if not uid.isdigit():
        return value
    return (
        f'<a href="https://bbs.nga.cn/nuke.php?func=ucp&amp;uid={uid}" '
        f'target="_blank" rel="noopener noreferrer">{value}</a>'
    )


def _render_img(
    _name: str,
    value: str,
    _options: dict[str, str],
    _parent: object | None,
    context: dict[str, object],
) -> str:
    src = value.strip()
    resolver = context.get(_IMAGE_SRC_RESOLVER_CONTEXT_KEY)
    if callable(resolver):
        resolved_src = cast(ImageSrcResolver, resolver)(src)
        if resolved_src is None:
            return escape(f"[img]{value}[/img]", quote=False)
        src = resolved_src

    return f'<img src="{_attr(src)}" alt="" loading="lazy" />'


def _render_color(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    color = _first_option(options, name)
    return f'<span style="color:{_attr(color)}">{value}</span>'


def _render_size(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    size = _first_option(options, name)
    return f'<span style="font-size:{_attr(size)}">{value}</span>'


def _render_align(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    align = _first_option(options, name).strip().lower()
    if align not in {"left", "center", "right"}:
        align = "left"
    return f'<div style="text-align:{align}">{value}</div>'


def _render_center(
    _name: str,
    value: str,
    _options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    return f'<div style="text-align:center">{value}</div>'


def _render_collapse(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    title = _first_option(options, name).strip() or "折叠内容"
    return (
        '<details class="nga-collapse">'
        f'<summary>{escape(title)}</summary>'
        f'<div class="nga-collapse-content">{value}</div>'
        "</details>"
    )


def _render_flash(
    name: str,
    value: str,
    options: dict[str, str],
    _parent: object | None,
    _context: dict[str, object],
) -> str:
    media_type = _first_option(options, name).strip() or "media"
    return (
        '<span class="nga-media-link">'
        f"{escape(media_type)}: {escape(value, quote=False)}"
        "</span>"
    )


def _install_web_formatters(parser: _BBCodeParser) -> None:
    parser.add_simple_formatter("b", "<strong>%(value)s</strong>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("i", "<em>%(value)s</em>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("u", "<u>%(value)s</u>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("s", "<del>%(value)s</del>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("del", "<del>%(value)s</del>", **_INLINE_OPTIONS)
    parser.add_simple_formatter(
        "quote",
        '<blockquote class="nga-quote">%(value)s</blockquote>',
        **_INLINE_OPTIONS,
    )
    parser.add_simple_formatter(
        "code",
        "<pre><code>%(value)s</code></pre>",
        **_INLINE_OPTIONS,
    )
    parser.add_simple_formatter(
        "h",
        '<h4 class="nga-bbcode-heading">%(value)s</h4>',
        **_INLINE_OPTIONS,
    )
    parser.add_simple_formatter("list", "<ul>%(value)s</ul>", **_INLINE_OPTIONS)
    parser.add_simple_formatter("*", "<li>%(value)s</li>", standalone=True, **_INLINE_OPTIONS)
    parser.add_simple_formatter("font", "%(value)s", **_INLINE_OPTIONS)
    parser.add_formatter("url", _render_url, **_INLINE_OPTIONS)
    parser.add_formatter("pid", _render_pid, **_INLINE_OPTIONS)
    parser.add_formatter("uid", _render_uid, **_INLINE_OPTIONS)
    parser.add_formatter("img", _render_img, **_INLINE_OPTIONS)
    parser.add_formatter("color", _render_color, **_INLINE_OPTIONS)
    parser.add_formatter("size", _render_size, **_INLINE_OPTIONS)
    parser.add_formatter("align", _render_align, **_INLINE_OPTIONS)
    parser.add_formatter("center", _render_center, **_INLINE_OPTIONS)
    parser.add_formatter("collapse", _render_collapse, **_INLINE_OPTIONS)
    parser.add_formatter("flash", _render_flash, **_INLINE_OPTIONS)


@lru_cache(maxsize=1)
def _web_parser() -> _BBCodeParser:
    parser = _new_parser()
    _install_web_formatters(parser)
    return parser


def render_web_bbcode(
    text: str,
    *,
    image_src_resolver: ImageSrcResolver | None = None,
) -> str:
    if image_src_resolver is None:
        return _web_parser().format(text)
    return _web_parser().format(
        text,
        **{_IMAGE_SRC_RESOLVER_CONTEXT_KEY: image_src_resolver},
    )

from __future__ import annotations

import re
from functools import lru_cache

import nh3

_ALLOWED_TAGS = {
    "a",
    "audio",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "details",
    "div",
    "em",
    "h4",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "summary",
    "u",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"class", "href", "target", "title"},
    "audio": {"aria-label", "class", "controls", "preload", "src"},
    "blockquote": {"class", "style"},
    "code": {"class"},
    "del": {"class"},
    "details": {"class"},
    "div": {"class", "style"},
    "em": {"class"},
    "h4": {"class"},
    "img": {"alt", "class", "data-srcorg", "loading", "src", "style", "title"},
    "li": {"class"},
    "ol": {"class"},
    "p": {"class", "style"},
    "pre": {"class"},
    "s": {"class"},
    "span": {"class", "style"},
    "strong": {"class"},
    "summary": {"class"},
    "ul": {"class"},
}
_ALLOWED_STYLE_PROPERTIES = {
    "color",
    "display",
    "font-family",
    "font-size",
    "max-height",
    "min-height",
    "text-align",
}
_ALLOWED_URL_SCHEMES = {"http", "https"}
_DROP_CONTENT_TAGS = {
    "iframe",
    "math",
    "noscript",
    "object",
    "script",
    "style",
    "svg",
    "template",
}
_DOUBLE_ENCODED_NUMERIC_ENTITY_RE = re.compile(
    r"&amp;(#(?:[0-9]+|[xX][0-9A-Fa-f]+);)"
)


@lru_cache(maxsize=1)
def _post_html_cleaner() -> nh3.Cleaner:
    return nh3.Cleaner(
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        clean_content_tags=_DROP_CONTENT_TAGS,
        filter_style_properties=_ALLOWED_STYLE_PROPERTIES,
        url_schemes=_ALLOWED_URL_SCHEMES,
        link_rel="noopener noreferrer",
    )


def sanitize_post_html(html: str) -> str:
    normalized_html = _DOUBLE_ENCODED_NUMERIC_ENTITY_RE.sub(r"&\1", html)
    return _post_html_cleaner().clean(normalized_html)

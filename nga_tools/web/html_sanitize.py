from __future__ import annotations

from functools import lru_cache

import nh3

_ALLOWED_TAGS = {
    "a",
    "b",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "i",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "span",
    "strong",
    "u",
    "ul",
}
_ALLOWED_ATTRIBUTES = {
    "a": {"href", "title"},
    "blockquote": {"style"},
    "img": {"alt", "data-srcorg", "src", "style", "title"},
    "p": {"style"},
    "span": {"style"},
}
_ALLOWED_STYLE_PROPERTIES = {
    "color",
    "display",
    "font-size",
    "max-height",
    "min-height",
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
    return _post_html_cleaner().clean(html)

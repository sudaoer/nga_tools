from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

import bbcode  # type: ignore

type Formatter = Callable[
    [str, str, dict[str, str], object | None, dict[str, object]],
    str,
]
type ImageSrcResolver = Callable[[str], str | None]

IMAGE_SRC_RESOLVER_CONTEXT_KEY = "image_src_resolver"
INLINE_OPTIONS: dict[str, object] = {
    "escape_html": False,
    "replace_links": False,
    "replace_cosmetic": False,
}


class BBCodeParser(Protocol):
    def add_formatter(
        self,
        tag_name: str,
        render_func: Formatter,
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


type ParserFactory = Callable[..., BBCodeParser]
PARSER_FACTORY = cast(ParserFactory, bbcode.Parser)


def new_parser() -> BBCodeParser:
    return PARSER_FACTORY(
        newline="\n",
        install_defaults=False,
        escape_html=False,
        replace_links=False,
        replace_cosmetic=False,
    )


def first_option(options: dict[str, str], tag_name: str) -> str:
    if tag_name in options:
        return options[tag_name]
    if options:
        return next(iter(options.keys()))
    return ""

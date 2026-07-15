from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, TypedDict

from bs4 import BeautifulSoup, Tag


class PostData(TypedDict):
    lou: int
    pid: int
    content: str


class PostHtml(TypedDict):
    lou: int
    pid: Optional[int]
    html: str


class PostRecord(TypedDict):
    lou: int
    pid: Optional[int]
    post: Optional[PostData]
    html: Optional[str]
    source_hash: str


@dataclass(frozen=True)
class ParsedPostHtml:
    post_html: PostHtml
    soup: BeautifulSoup
    images: list[Tag]

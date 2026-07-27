from __future__ import annotations

import html
import re
import warnings
from dataclasses import dataclass
from typing import cast

from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning, Tag

from nga_tools.bbcode_convert import strip_bbcode_tags

WORD_COUNT_VERSION = 1
DEFAULT_MIN_BODY_CHARS = 120

BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
IMG_BBCODE_RE = re.compile(r"\[img\].*?\[/img\]", re.IGNORECASE | re.DOTALL)
REPLY_HEADER_RE = re.compile(
    r"<b>\s*Reply to\s+\[pid=.*?</b>",
    re.IGNORECASE | re.DOTALL,
)
UID_BLOCK_RE = re.compile(
    r"\[(?:uid|pid)=[^\]]+\].*?\[/(?:uid|pid)\]",
    re.IGNORECASE | re.DOTALL,
)
MENTION_RE = re.compile(r"\[@[^\]\r\n]+\]")
EMOTE_RE = re.compile(r"\[s:[^\]\r\n]+\]", re.IGNORECASE)
DICE_BBCODE_RE = re.compile(
    r"\[(?:\d*d\d+(?:[+\-*/]\d+)*(?:=[^\]\r\n]*)?|"
    r"\.\s*r[^\]\r\n]*)\]",
    re.IGNORECASE,
)
DOT_DICE_RE = re.compile(
    r"(?<![\w.])\.\s*r\s*\d*d\d+(?:[+\-*/]\d+)*(?:\s*=\s*[^\s<，。！？；：、]*)?",
    re.IGNORECASE,
)
WHITESPACE_RE = re.compile(r"[ \t\r\f\v]+")
BLANK_LINES_RE = re.compile(r"\n{3,}")

CHINESE_PUNCTUATION = frozenset(
    "，。！？；：、、“”‘’（）《》〈〉【】「」『』〔〕〖〗〘〙〚〛［］｛｝"
    "〃〄〝〞〟〰〽—…〜～·￥"
)


@dataclass(frozen=True)
class TextWordCount:
    chinese_chars: int
    chinese_with_punctuation: int


def _is_cjk_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0x20000 <= codepoint <= 0x2A6DF
        or 0x2A700 <= codepoint <= 0x2B73F
        or 0x2B740 <= codepoint <= 0x2B81F
        or 0x2B820 <= codepoint <= 0x2CEAF
        or 0x2CEB0 <= codepoint <= 0x2EBEF
        or 0x30000 <= codepoint <= 0x3134F
    )


def _is_chinese_punctuation(char: str) -> bool:
    return char in CHINESE_PUNCTUATION


def _count_chinese_text(text: str) -> TextWordCount:
    chinese_chars = 0
    chinese_with_punctuation = 0
    for char in text:
        if _is_cjk_char(char):
            chinese_chars += 1
            chinese_with_punctuation += 1
        elif _is_chinese_punctuation(char):
            chinese_with_punctuation += 1

    return TextWordCount(
        chinese_chars=chinese_chars,
        chinese_with_punctuation=chinese_with_punctuation,
    )


def _strip_reply_quote_blocks(text: str) -> str:
    pattern = re.compile(r"\[quote\](.*?)\[/quote\]", re.IGNORECASE | re.DOTALL)
    previous = None
    current = text
    while previous != current:
        previous = current
        current = pattern.sub(_replace_reply_quote, current)
    return current


def _replace_reply_quote(match: re.Match[str]) -> str:
    quote_body = match.group(1)
    quote_start = BR_RE.sub("", quote_body).lstrip()
    if quote_start.startswith("[pid="):
        return "\n"
    return match.group(0)


def _remove_html_noise(text: str) -> str:
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=MarkupResemblesLocatorWarning)
        soup = BeautifulSoup(text, "html.parser")

    for tag in cast(list[Tag], soup.find_all(["img", "script", "style"])):
        tag.decompose()

    for tag in cast(list[Tag], soup.find_all(class_="collapse_btn")):
        tag.decompose()

    return soup.get_text("\n")


def _clean_post_content(content: str) -> str:
    text = html.unescape(content)
    text = IMG_BBCODE_RE.sub("\n", text)
    text = _strip_reply_quote_blocks(text)
    text = REPLY_HEADER_RE.sub("\n", text)
    text = UID_BLOCK_RE.sub("\n", text)
    text = MENTION_RE.sub("\n", text)
    text = EMOTE_RE.sub("\n", text)
    text = DICE_BBCODE_RE.sub("\n", text)
    text = DOT_DICE_RE.sub("\n", text)
    text = BR_RE.sub("\n", text)
    text = strip_bbcode_tags(text)
    text = _remove_html_noise(text)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def count_post_content(content: str) -> TextWordCount:
    return _count_chinese_text(_clean_post_content(content))

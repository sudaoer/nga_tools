from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.ngaclient.client import PageData

PAGE_JSON_RE = re.compile(r"^page_(\d+)\.json$")
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
IMG_BBCODE_RE = re.compile(r"\[img\].*?\[/img\]", re.IGNORECASE | re.DOTALL)
URL_BBCODE_RE = re.compile(
    r"\[url(?:=[^\]]*)?\](.*?)\[/url\]",
    re.IGNORECASE | re.DOTALL,
)
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
TECHNICAL_BBCODE_RE = re.compile(
    r"\[/?(?:b|i|u|s|quote|code|color|size|font|align|collapse|del)"
    r"(?:=[^\]]*)?\]",
    re.IGNORECASE,
)
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


@dataclass(frozen=True)
class WordCountSummary:
    tid: int
    aid: Optional[int]
    json_folder: Path
    page_count: int
    total_posts: int
    body_posts: int
    excluded_posts: int
    min_body_chars: int
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


def count_chinese_text(text: str) -> TextWordCount:
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
    soup = BeautifulSoup(text, "html.parser")
    for tag in cast(list[Tag], soup.find_all(["img", "script", "style"])):
        tag.decompose()

    for tag in cast(list[Tag], soup.find_all(class_="collapse_btn")):
        tag.decompose()

    return soup.get_text("\n")


def clean_post_content(content: str) -> str:
    text = html.unescape(content)
    text = IMG_BBCODE_RE.sub("\n", text)
    text = _strip_reply_quote_blocks(text)
    text = REPLY_HEADER_RE.sub("\n", text)
    text = URL_BBCODE_RE.sub(r"\1", text)
    text = UID_BLOCK_RE.sub("\n", text)
    text = MENTION_RE.sub("\n", text)
    text = EMOTE_RE.sub("\n", text)
    text = DICE_BBCODE_RE.sub("\n", text)
    text = DOT_DICE_RE.sub("\n", text)
    text = BR_RE.sub("\n", text)
    text = TECHNICAL_BBCODE_RE.sub("", text)
    text = _remove_html_noise(text)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    text = BLANK_LINES_RE.sub("\n\n", text)
    return text.strip()


def _page_json_sort_key(path: Path) -> int:
    match = PAGE_JSON_RE.fullmatch(path.name)
    if match is None:
        return 0
    return int(match.group(1))


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"JSON备份文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON备份文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"JSON备份文件顶层必须是对象：{path}")
    return cast(dict[str, object], raw_data)


def _page_posts(page_data: PageData, source: Path) -> list[dict[str, object]]:
    raw_posts = page_data.get("result")
    if not isinstance(raw_posts, list):
        raise ValueError(f"{source} 缺少帖子列表。")

    posts: list[dict[str, object]] = []
    for raw_post in cast(list[object], raw_posts):
        if not isinstance(raw_post, dict):
            raise ValueError(f"{source} 中的帖子不是对象：{raw_post!r}")
        post = cast(dict[str, object], raw_post)
        lou = post.get("lou")
        content = post.get("content")
        if type(lou) is not int or not isinstance(content, str):
            raise ValueError(f"{source} 中的帖子lou/content字段无效：{raw_post!r}")
        posts.append(post)

    return posts


def _page_paths(folder_json: Path) -> list[Path]:
    if not folder_json.exists():
        raise RuntimeError(f"缺少JSON备份目录：{folder_json}。请先运行 backup all。")
    if not folder_json.is_dir():
        raise RuntimeError(f"JSON备份路径不是目录：{folder_json}")

    paths = sorted(
        (
            path
            for path in folder_json.iterdir()
            if path.is_file() and PAGE_JSON_RE.fullmatch(path.name)
        ),
        key=_page_json_sort_key,
    )
    if not paths:
        raise RuntimeError(f"缺少JSON备份文件：{folder_json}/page_*.json")
    return paths


def count_backup_words(
    tid: int,
    aid: Optional[int],
    min_body_chars: int = 120,
) -> WordCountSummary:
    if min_body_chars <= 0:
        raise ValueError("--min_body_chars必须大于0。")

    folder_json = Path(utils.get_folder(tid, aid, "json", create=False))
    page_paths = _page_paths(folder_json)

    total_posts = 0
    body_posts = 0
    chinese_chars = 0
    chinese_with_punctuation = 0

    for path in page_paths:
        page_data = cast(PageData, _read_json_object(path))
        for post in sorted(_page_posts(page_data, path), key=lambda item: cast(int, item["lou"])):
            total_posts += 1
            content = cast(str, post["content"])
            cleaned_content = clean_post_content(content)
            count = count_chinese_text(cleaned_content)
            if count.chinese_with_punctuation < min_body_chars:
                continue

            body_posts += 1
            chinese_chars += count.chinese_chars
            chinese_with_punctuation += count.chinese_with_punctuation

    return WordCountSummary(
        tid=tid,
        aid=aid,
        json_folder=folder_json,
        page_count=len(page_paths),
        total_posts=total_posts,
        body_posts=body_posts,
        excluded_posts=total_posts - body_posts,
        min_body_chars=min_body_chars,
        chinese_chars=chinese_chars,
        chinese_with_punctuation=chinese_with_punctuation,
    )

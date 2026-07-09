from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nga_tools import utils
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.word_count import (
    DEFAULT_MIN_BODY_CHARS,
    TextWordCount,
    clean_post_content,
    count_chinese_text,
)

__all__ = [
    "TextWordCount",
    "WordCountSummary",
    "clean_post_content",
    "count_backup_words",
    "count_chinese_text",
]


@dataclass(frozen=True)
class WordCountSummary:
    tid: int
    aid: Optional[int]
    archive_path: Path
    page_count: int
    total_posts: int
    body_posts: int
    excluded_posts: int
    min_body_chars: int
    chinese_chars: int
    chinese_with_punctuation: int


def count_backup_words(
    tid: int,
    aid: Optional[int],
    min_body_chars: int = DEFAULT_MIN_BODY_CHARS,
) -> WordCountSummary:
    if min_body_chars <= 0:
        raise ValueError("--min_body_chars必须大于0。")

    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    totals = archive_store.read_latest_word_count_totals(min_body_chars)

    return WordCountSummary(
        tid=tid,
        aid=aid,
        archive_path=archive_store.db_path,
        page_count=archive_store.page_count(),
        total_posts=totals.total_posts,
        body_posts=totals.body_posts,
        excluded_posts=totals.total_posts - totals.body_posts,
        min_body_chars=min_body_chars,
        chinese_chars=totals.chinese_chars,
        chinese_with_punctuation=totals.chinese_with_punctuation,
    )

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, Optional, TypedDict

FLOOR_MAP_VERSION = 1
FLOOR_MAP_GENERATION_VERSION = 1
FLOOR_MAP_HASH_ALGORITHM = "sha256"
MISSING_POST_HTML = "<p><em>本楼层内容缺失。</em></p>"
ORIGINAL_POSTS_PER_PAGE = 20


class AuthorPostRef(TypedDict):
    pid: int
    author_lou: int


class FloorMapEntry(TypedDict):
    pid: Optional[int]
    author_lou: int
    original_lou: Optional[int]
    original_pid: NotRequired[int]
    candidate_original_lous: NotRequired[list[int]]


@dataclass(frozen=True)
class StoredFloorMap:
    version: int
    generation_version: int
    algorithm: str
    tid: int
    aid: int
    input_signature: str
    entries: list[FloorMapEntry]


MissingFloorIncrementalFallbackReason = Literal[
    "revision_mismatch",
    "state_mismatch",
    "candidate",
    "no_locator",
    "entry_state",
]


@dataclass(frozen=True)
class ExactMissingFloorLocatorRead:
    exact_original_lou_by_author_lou: dict[int, int]
    fallback_reason: MissingFloorIncrementalFallbackReason | None


@dataclass(frozen=True)
class PartialFloorMapUpdateResult:
    succeeded: bool
    updated_count: int


class OriginalPostSnapshot(TypedDict):
    pid: int
    lou: int
    author_uid: Optional[int]
    content: str
    raw_post: dict[str, object]


class RecoveredMissingPost(TypedDict):
    original_pid: int
    original_lou: int
    content: str
    raw_post: dict[str, object]


@dataclass(frozen=True)
class FloorLabels:
    original_lou_by_author_lou: dict[int, int]
    candidate_original_lous_by_author_lou: dict[int, list[int]]
    show_original: bool

    @classmethod
    def plain(cls) -> "FloorLabels":
        return cls(
            original_lou_by_author_lou={},
            candidate_original_lous_by_author_lou={},
            show_original=False,
        )

    def label(self, author_lou: int) -> str:
        if not self.show_original:
            return f"第{author_lou}楼"

        original_lou = self.original_lou_by_author_lou.get(author_lou)
        if original_lou is None:
            candidates = self.candidate_original_lous_by_author_lou.get(author_lou)
            if candidates:
                candidate_text = format_candidate_lous(candidates)
                return f"第{author_lou}楼（原楼层候选：{candidate_text}）"
            return f"第{author_lou}楼（原楼层未知）"

        return f"第{author_lou}楼（原{original_lou}楼）"


@dataclass(frozen=True)
class FloorMapBuildResult:
    floor_labels: FloorLabels
    recovered_missing_posts_by_author_lou: dict[int, RecoveredMissingPost]


@dataclass(frozen=True)
class MissingOriginalInference:
    exact_original_by_author_lou: dict[int, int]
    candidate_originals_by_author_lou: dict[int, list[int]]


def format_candidate_lous(candidates: Sequence[int]) -> str:
    if len(candidates) <= 5:
        return ", ".join(str(lou) for lou in candidates)

    preview = ", ".join(str(lou) for lou in candidates[:5])
    return f"{preview} 等{len(candidates)}个"

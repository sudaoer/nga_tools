from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from nga_tools.backup.archive_posts import ArchivePostMetadata
from nga_tools.backup.floor_models import AuthorPostRef, StoredFloorMap
from nga_tools.backup.models import PostData
from nga_tools.word_count import TextWordCount


@dataclass(frozen=True)
class ArchivePageUpsertResult:
    post_versions_inserted: int
    effective_processing_inputs_changed: bool
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class ArchivePagesUpsertResult:
    pages_processed: int
    post_versions_inserted: int
    effective_processing_inputs_changed: bool
    effective_changed_pages: int
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class RecoveredPostsUpsertResult:
    inserted_count: int
    effective_changed_lous: frozenset[int]
    effective_added_lous: frozenset[int]


@dataclass(frozen=True)
class ArchivePagePagination:
    page_count: int
    vrows: Optional[int]


@dataclass(frozen=True)
class ArchiveEffectivePostStats:
    post_count: int
    max_lou: Optional[int]


@dataclass(frozen=True)
class AuthorFloorRefreshInputs:
    post_refs: tuple[AuthorPostRef, ...]
    stored_floor_map: StoredFloorMap | None
    floor_map_error: str | None


@dataclass(frozen=True)
class ArchivePostVersionRow:
    version_id: int
    lou: int
    pid: int
    content: str
    source_hash: str
    author_name: Optional[str]
    author_uid: Optional[int]
    postdate_json: Optional[str]
    manual_selection: bool


@dataclass(frozen=True)
class PreparedArchivePost:
    raw_post: object
    post: PostData
    source_hash: str
    word_count: TextWordCount
    metadata: ArchivePostMetadata


@dataclass(frozen=True)
class PreparedArchivePage:
    page_number: int
    total_page: Optional[int]
    vrows: Optional[int]
    observed_at: str
    count_observation: bool
    posts: tuple[PreparedArchivePost, ...]

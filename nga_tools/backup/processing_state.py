from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from nga_tools.core.download_types import DownloadFailureKind


FLOOR_PROCESSING_STATE_VERSION = 2
IMAGE_REFERENCE_STATE_VERSION = 1
IMAGE_REFERENCE_MANIFEST_VERSION = 1
AUDIO_PROCESSING_STATE_VERSION = 1
AUDIO_REFERENCE_EXTRACTOR_VERSION = 1


@dataclass(frozen=True)
class ArchiveChangeState:
    archive_revision: int
    floor_map_revision: int


@dataclass(frozen=True)
class CurrentPaginationState:
    page_count: int
    author_total_lou_count: int | None
    source_page_number: int
    observed_at: datetime


@dataclass(frozen=True)
class FloorProcessingState:
    format_version: int
    processed_archive_revision: int
    processed_floor_map_revision: int
    page_count: int
    author_total_lou_count: int | None
    floor_map_format_version: int
    floor_map_generation_version: int
    floor_map_hash_algorithm: str
    completed_at: str


@dataclass(frozen=True)
class ImageReferenceState:
    format_version: int
    processed_archive_revision: int
    post_overlays_fingerprint: str
    post_version_selections_fingerprint: str
    image_reference_extractor_version: int
    completed_at: str


@dataclass(frozen=True)
class ImageReferenceManifestState:
    format_version: int
    processed_archive_revision: int


@dataclass(frozen=True)
class ImageReferenceManifestEntry:
    image_index: int
    url: str
    valid: bool


@dataclass(frozen=True)
class ImageReferenceManifestPost:
    lou: int
    cache_key: str
    references: tuple[ImageReferenceManifestEntry, ...]


@dataclass(frozen=True)
class ImageReferenceManifestSnapshot:
    state: ImageReferenceManifestState
    posts: tuple[ImageReferenceManifestPost, ...]
    url_reference_counts: tuple[tuple[str, int, bool], ...]


@dataclass(frozen=True)
class PendingMediaRetry:
    url: str
    last_attempt_at: datetime | None
    failure_kind: DownloadFailureKind | None
    http_status: int | None


@dataclass(frozen=True)
class PendingMissingFloorRetry:
    author_lou: int
    last_attempt_at: datetime


@dataclass(frozen=True)
class AudioProcessingState:
    format_version: int
    extractor_version: int
    processed_max_post_version_id: int
    completed_at: str


@dataclass(frozen=True)
class BackupProcessingSnapshot:
    change_state: ArchiveChangeState
    pending_image_retries: tuple[PendingMediaRetry, ...]
    current_pagination_state: CurrentPaginationState | None = None
    floor_state: FloorProcessingState | None = None
    image_state: ImageReferenceState | None = None
    audio_state: AudioProcessingState | None = None
    pending_audio_retries: tuple[PendingMediaRetry, ...] = ()
    pending_missing_floor_retries: tuple[PendingMissingFloorRetry, ...] = ()

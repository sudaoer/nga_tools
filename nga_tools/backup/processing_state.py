from __future__ import annotations

from dataclasses import dataclass


BACKUP_PROCESSING_STATE_VERSION = 1


@dataclass(frozen=True)
class ArchiveChangeState:
    archive_revision: int
    floor_map_revision: int


@dataclass(frozen=True)
class BackupProcessingState:
    format_version: int
    processed_archive_revision: int
    processed_floor_map_revision: int
    page_count: int
    author_total_lou_count: int | None
    post_overlays_fingerprint: str
    post_version_selections_fingerprint: str
    floor_map_format_version: int
    floor_map_generation_version: int
    floor_map_hash_algorithm: str
    image_reference_extractor_version: int
    completed_at: str


@dataclass(frozen=True)
class BackupProcessingSnapshot:
    change_state: ArchiveChangeState
    processing_state: BackupProcessingState | None
    pending_image_urls: tuple[str, ...]

from __future__ import annotations

from dataclasses import dataclass

from nga_tools.backup.processing_state import BackupProcessingSnapshot


@dataclass(frozen=True)


class ArchiveIncrementalChanges:
    previous_snapshot: BackupProcessingSnapshot | None
    changed_lous: frozenset[int]
    added_lous: frozenset[int]
    archive_revision_increments: int

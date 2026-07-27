from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from nga_tools.backup.media_retry import (
    MediaRetrySelection,
    pending_media_retries_after_attempt,
    select_media_retries,
)
from nga_tools.backup.processing_state import PendingMediaRetry
from nga_tools.core.download_types import DownloadFileResult


_AUDIO_RETRY_NAMESPACE = "backup-audio-retry"


type AudioRetrySelection = MediaRetrySelection


def select_audio_retries(
    retries: tuple[PendingMediaRetry, ...],
    *,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
) -> AudioRetrySelection:
    return select_media_retries(
        retries,
        namespace=_AUDIO_RETRY_NAMESPACE,
        thread_target_key=thread_target_key,
        now=now,
        max_interval=max_interval,
        force=force,
    )


def pending_audio_retries_after_attempt(
    deferred: tuple[PendingMediaRetry, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
) -> tuple[PendingMediaRetry, ...]:
    return pending_media_retries_after_attempt(
        deferred,
        failed,
        attempted_at=attempted_at,
        media_label="音频",
    )

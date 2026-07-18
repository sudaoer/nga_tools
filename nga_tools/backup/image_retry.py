from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta

from nga_tools.backup.media_retry import (
    MediaRetrySelection,
    pending_media_retries_after_attempt,
    select_media_retries,
    shared_media_retry_ticket,
    uses_probabilistic_backoff as uses_probabilistic_backoff,
)
from nga_tools.backup.processing_state import PendingImageRetry
from nga_tools.core.download_types import DownloadFileResult


_IMAGE_RETRY_NAMESPACE = "backup-image-retry"


type ImageRetrySelection = MediaRetrySelection[PendingImageRetry]


def select_image_retries(
    retries: tuple[PendingImageRetry, ...],
    *,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
) -> ImageRetrySelection:
    return select_media_retries(
        retries,
        namespace=_IMAGE_RETRY_NAMESPACE,
        thread_target_key=thread_target_key,
        now=now,
        max_interval=max_interval,
        force=force,
        shared_ticket=shared_media_retry_ticket(
            namespace=_IMAGE_RETRY_NAMESPACE,
            thread_target_key=thread_target_key,
        ),
    )


def pending_image_retries_after_attempt(
    deferred: tuple[PendingImageRetry, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
) -> tuple[PendingImageRetry, ...]:
    return pending_media_retries_after_attempt(
        deferred,
        failed,
        attempted_at=attempted_at,
        media_label="图片",
        retry_factory=lambda url, last_attempt_at, failure_kind, http_status: (
            PendingImageRetry(url, last_attempt_at, failure_kind, http_status)
        ),
    )

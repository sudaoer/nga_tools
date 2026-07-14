from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from nga_tools.backup.processing_state import PendingImageRetry
from nga_tools.core.downloads import DownloadFileResult
from nga_tools.core.retry_schedule import retry_schedule_decision


_IMAGE_RETRY_NAMESPACE = "backup-image-retry"


@dataclass(frozen=True)
class ImageRetrySelection:
    due: tuple[PendingImageRetry, ...]
    deferred: tuple[PendingImageRetry, ...]


def uses_probabilistic_backoff(retry: PendingImageRetry) -> bool:
    if retry.http_status is None:
        return False
    if retry.failure_kind == "http_3xx":
        return True
    return retry.failure_kind == "http_4xx" and retry.http_status != 429


def select_image_retries(
    retries: tuple[PendingImageRetry, ...],
    *,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
) -> ImageRetrySelection:
    due: list[PendingImageRetry] = []
    deferred: list[PendingImageRetry] = []
    for retry in retries:
        if force or not uses_probabilistic_backoff(retry):
            due.append(retry)
            continue
        decision = retry_schedule_decision(
            namespace=_IMAGE_RETRY_NAMESPACE,
            target_key=f"{thread_target_key}\0{retry.url}",
            last_event_at=retry.last_attempt_at,
            now=now,
            max_interval=max_interval,
        )
        if decision.should_run:
            due.append(retry)
        else:
            deferred.append(retry)
    return ImageRetrySelection(tuple(due), tuple(deferred))


def pending_image_retries_after_attempt(
    deferred: tuple[PendingImageRetry, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
) -> tuple[PendingImageRetry, ...]:
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError("图片重试时间必须包含时区。")
    retries_by_url = {retry.url: retry for retry in deferred}
    for item in failed:
        url = item["url"]
        if url in retries_by_url:
            raise ValueError(f"延后图片不应同时产生下载结果：{url}")
        retries_by_url[url] = PendingImageRetry(
            url=url,
            last_attempt_at=attempted_at,
            failure_kind=item.get("failure_kind", "unexpected_download"),
            http_status=item.get("http_status"),
        )
    return tuple(retries_by_url[url] for url in sorted(retries_by_url))

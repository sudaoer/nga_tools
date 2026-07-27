from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from nga_tools.backup.processing_state import PendingMediaRetry
from nga_tools.core.download_types import DownloadFileResult
from nga_tools.core.retry_schedule import (
    retry_schedule_decision,
    retry_schedule_decision_for_ticket,
    stable_retry_ticket,
)


@dataclass(frozen=True)
class MediaRetrySelection:
    due: tuple[PendingMediaRetry, ...]
    deferred: tuple[PendingMediaRetry, ...]


def uses_probabilistic_backoff(retry: PendingMediaRetry) -> bool:
    if retry.http_status is None:
        return False
    if retry.failure_kind == "http_3xx":
        return True
    return retry.failure_kind == "http_4xx" and retry.http_status != 429


def select_media_retries(
    retries: tuple[PendingMediaRetry, ...],
    *,
    namespace: str,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
    shared_ticket: float | None = None,
) -> MediaRetrySelection:
    due: list[PendingMediaRetry] = []
    deferred: list[PendingMediaRetry] = []
    for retry in retries:
        if force or not uses_probabilistic_backoff(retry):
            due.append(retry)
            continue
        if shared_ticket is None:
            decision = retry_schedule_decision(
                namespace=namespace,
                target_key=f"{thread_target_key}\0{retry.url}",
                last_event_at=retry.last_attempt_at,
                now=now,
                max_interval=max_interval,
            )
        else:
            decision = retry_schedule_decision_for_ticket(
                ticket=shared_ticket,
                last_event_at=retry.last_attempt_at,
                now=now,
                max_interval=max_interval,
            )
        if decision.should_run:
            due.append(retry)
        else:
            deferred.append(retry)
    return MediaRetrySelection(tuple(due), tuple(deferred))


def shared_media_retry_ticket(*, namespace: str, thread_target_key: str) -> float:
    return stable_retry_ticket(
        namespace=namespace,
        target_key=thread_target_key,
    )


def pending_media_retries_after_attempt(
    deferred: tuple[PendingMediaRetry, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
    media_label: str,
) -> tuple[PendingMediaRetry, ...]:
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError(f"{media_label}重试时间必须包含时区。")
    retries_by_url = {retry.url: retry for retry in deferred}
    for item in failed:
        url = item["url"]
        if url in retries_by_url:
            raise ValueError(f"延后{media_label}不应同时产生下载结果：{url}")
        retries_by_url[url] = PendingMediaRetry(
            url=url,
            last_attempt_at=attempted_at,
            failure_kind=item.get("failure_kind", "unexpected_download"),
            http_status=item.get("http_status"),
        )
    return tuple(retries_by_url[url] for url in sorted(retries_by_url))

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol, TypeVar

from nga_tools.core.download_types import (
    DownloadFailureKind,
    DownloadFileResult,
)
from nga_tools.core.retry_schedule import (
    retry_schedule_decision,
    retry_schedule_decision_for_ticket,
    stable_retry_ticket,
)


class PendingMediaRetry(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def last_attempt_at(self) -> datetime | None: ...

    @property
    def failure_kind(self) -> DownloadFailureKind | None: ...

    @property
    def http_status(self) -> int | None: ...


RetryT = TypeVar("RetryT", bound=PendingMediaRetry)


@dataclass(frozen=True)
class MediaRetrySelection[RetryT: PendingMediaRetry]:
    due: tuple[RetryT, ...]
    deferred: tuple[RetryT, ...]


def uses_probabilistic_backoff(retry: PendingMediaRetry) -> bool:
    if retry.http_status is None:
        return False
    if retry.failure_kind == "http_3xx":
        return True
    return retry.failure_kind == "http_4xx" and retry.http_status != 429


def select_media_retries(
    retries: tuple[RetryT, ...],
    *,
    namespace: str,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
    shared_ticket: float | None = None,
) -> MediaRetrySelection[RetryT]:
    due: list[RetryT] = []
    deferred: list[RetryT] = []
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
    deferred: tuple[RetryT, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
    media_label: str,
    retry_factory: Callable[
        [str, datetime, DownloadFailureKind, int | None],
        RetryT,
    ],
) -> tuple[RetryT, ...]:
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError(f"{media_label}重试时间必须包含时区。")
    retries_by_url = {retry.url: retry for retry in deferred}
    for item in failed:
        url = item["url"]
        if url in retries_by_url:
            raise ValueError(f"延后{media_label}不应同时产生下载结果：{url}")
        retries_by_url[url] = retry_factory(
            url,
            attempted_at,
            item.get("failure_kind", "unexpected_download"),
            item.get("http_status"),
        )
    return tuple(retries_by_url[url] for url in sorted(retries_by_url))

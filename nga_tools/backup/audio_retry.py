from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from nga_tools.backup.processing_state import PendingAudioRetry
from nga_tools.core.downloads import DownloadFileResult
from nga_tools.core.retry_schedule import retry_schedule_decision


_AUDIO_RETRY_NAMESPACE = "backup-audio-retry"


@dataclass(frozen=True)
class AudioRetrySelection:
    due: tuple[PendingAudioRetry, ...]
    deferred: tuple[PendingAudioRetry, ...]


def uses_probabilistic_backoff(retry: PendingAudioRetry) -> bool:
    if retry.http_status is None:
        return False
    if retry.failure_kind == "http_3xx":
        return True
    return retry.failure_kind == "http_4xx" and retry.http_status != 429


def select_audio_retries(
    retries: tuple[PendingAudioRetry, ...],
    *,
    thread_target_key: str,
    now: datetime,
    max_interval: timedelta,
    force: bool,
) -> AudioRetrySelection:
    due: list[PendingAudioRetry] = []
    deferred: list[PendingAudioRetry] = []
    for retry in retries:
        if force or not uses_probabilistic_backoff(retry):
            due.append(retry)
            continue
        decision = retry_schedule_decision(
            namespace=_AUDIO_RETRY_NAMESPACE,
            target_key=f"{thread_target_key}\0{retry.url}",
            last_event_at=retry.last_attempt_at,
            now=now,
            max_interval=max_interval,
        )
        if decision.should_run:
            due.append(retry)
        else:
            deferred.append(retry)
    return AudioRetrySelection(tuple(due), tuple(deferred))


def pending_audio_retries_after_attempt(
    deferred: tuple[PendingAudioRetry, ...],
    failed: Sequence[DownloadFileResult],
    *,
    attempted_at: datetime,
) -> tuple[PendingAudioRetry, ...]:
    if attempted_at.tzinfo is None or attempted_at.utcoffset() is None:
        raise ValueError("音频重试时间必须包含时区。")
    retries_by_url = {retry.url: retry for retry in deferred}
    for item in failed:
        url = item["url"]
        if url in retries_by_url:
            raise ValueError(f"延后音频不应同时产生下载结果：{url}")
        retries_by_url[url] = PendingAudioRetry(
            url=url,
            last_attempt_at=attempted_at,
            failure_kind=item.get("failure_kind", "unexpected_download"),
            http_status=item.get("http_status"),
        )
    return tuple(retries_by_url[url] for url in sorted(retries_by_url))

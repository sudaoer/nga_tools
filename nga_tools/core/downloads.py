from __future__ import annotations

import os
from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

from nga_tools import network_limits


class DownloadTask(TypedDict):
    url: str
    request_url: NotRequired[str]
    save_path: str


DownloadFailureKind = Literal[
    "http_3xx",
    "http_4xx",
    "http_5xx",
    "timeout",
    "connection",
    "payload",
    "unexpected_download",
    "image_store",
]


class DownloadFileResult(TypedDict):
    url: str
    save_path: str
    success: bool
    error: NotRequired[str]
    failure_kind: NotRequired[DownloadFailureKind]
    http_status: NotRequired[int]


class DownloadSummary(TypedDict):
    succeeded: list[DownloadFileResult]
    failed: list[DownloadFileResult]


DownloadProgressCallback = Callable[[int, int, DownloadFileResult], None]


def effective_download_concurrency(max_concurrency: int | None) -> int:
    image_concurrency = network_limits.get_image_concurrency()
    if max_concurrency is None:
        return image_concurrency
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be greater than 0.")
    return min(max_concurrency, image_concurrency)


def download_files(
    url_filename_lists: list[DownloadTask],
    retries: int = 5,
    backoff_factor: float = 0.5,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    max_concurrency: int | None = None,
    on_progress: DownloadProgressCallback | None = None,
) -> DownloadSummary:
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return {"succeeded": [], "failed": []}

    from nga_tools.core.image_download_runtime import (
        current_image_download_runtime,
        use_image_download_runtime,
    )

    effective_max_concurrency = effective_download_concurrency(max_concurrency)
    runtime = current_image_download_runtime()
    if runtime is not None:
        return runtime.download(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )
    with use_image_download_runtime(
        network_limits.get_image_concurrency()
    ) as temporary_runtime:
        return temporary_runtime.download(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )


def download_files_streaming(
    url_filename_lists: list[DownloadTask],
    retries: int = 5,
    backoff_factor: float = 0.5,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    max_concurrency: int | None = None,
    on_progress: DownloadProgressCallback | None = None,
) -> None:
    """Download files while delivering results only through ``on_progress``."""
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return

    from nga_tools.core.image_download_runtime import (
        current_image_download_runtime,
        use_image_download_runtime,
    )

    effective_max_concurrency = effective_download_concurrency(max_concurrency)
    runtime = current_image_download_runtime()
    if runtime is not None:
        runtime.download_streaming(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )
        return
    with use_image_download_runtime(
        network_limits.get_image_concurrency()
    ) as temporary_runtime:
        temporary_runtime.download_streaming(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )

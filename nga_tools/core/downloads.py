from __future__ import annotations

import os

from nga_tools import network_limits
from nga_tools.core.download_types import (
    DOWNLOAD_FAILURE_KINDS as DOWNLOAD_FAILURE_KINDS,
    DownloadFailureKind as DownloadFailureKind,
    DownloadFileResult as DownloadFileResult,
    DownloadProgressCallback as DownloadProgressCallback,
    DownloadResourceKind as DownloadResourceKind,
    DownloadSummary as DownloadSummary,
    DownloadTask as DownloadTask,
)


def _configured_download_concurrency(resource_kind: DownloadResourceKind) -> int:
    if resource_kind == "audio":
        return network_limits.get_audio_concurrency()
    return network_limits.get_image_concurrency()


def effective_download_concurrency(
    max_concurrency: int | None,
    *,
    resource_kind: DownloadResourceKind = "image",
) -> int:
    configured_concurrency = _configured_download_concurrency(resource_kind)
    if max_concurrency is None:
        return configured_concurrency
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be greater than 0.")
    return min(max_concurrency, configured_concurrency)


def download_files(
    url_filename_lists: list[DownloadTask],
    retries: int = 5,
    backoff_factor: float = 0.5,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    max_concurrency: int | None = None,
    on_progress: DownloadProgressCallback | None = None,
    resource_kind: DownloadResourceKind = "image",
) -> DownloadSummary:
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return {"succeeded": [], "failed": []}

    from nga_tools.core.image_download_runtime import (
        current_download_runtime,
        use_download_runtime,
    )

    effective_max_concurrency = effective_download_concurrency(
        max_concurrency,
        resource_kind=resource_kind,
    )
    runtime = current_download_runtime(resource_kind)
    if runtime is not None:
        return runtime.download(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )
    with use_download_runtime(
        resource_kind,
        _configured_download_concurrency(resource_kind),
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
    resource_kind: DownloadResourceKind = "image",
) -> None:
    """Download files while delivering results only through ``on_progress``."""
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return

    from nga_tools.core.image_download_runtime import (
        current_download_runtime,
        use_download_runtime,
    )

    effective_max_concurrency = effective_download_concurrency(
        max_concurrency,
        resource_kind=resource_kind,
    )
    runtime = current_download_runtime(resource_kind)
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
    with use_download_runtime(
        resource_kind,
        _configured_download_concurrency(resource_kind),
    ) as temporary_runtime:
        temporary_runtime.download_streaming(
            pending_downloads,
            retries=retries,
            backoff_factor=backoff_factor,
            retry_statuses=retry_statuses,
            batch_limit=effective_max_concurrency,
            on_progress=on_progress,
        )

from __future__ import annotations

import asyncio
import os
import traceback
from collections.abc import Callable
from typing import Literal, NotRequired, Optional, TypedDict

import aiohttp

from nga_tools import network_limits
from nga_tools.console import WarningCategory, report_warning


class DownloadTask(TypedDict):
    url: str
    save_path: str


DownloadFailureKind = Literal[
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
    async def fetch_and_save(
        session: aiohttp.ClientSession,
        url: str,
        save_path: str,
        semaphore: asyncio.Semaphore,
    ) -> DownloadFileResult:
        attempt = 0
        last_exc: Optional[BaseException] = None
        while attempt <= retries:
            try:
                async with semaphore:
                    async with network_limits.image_download_slot():
                        async with session.get(url) as response:
                            if response.status >= 400:
                                raise aiohttp.ClientResponseError(
                                    request_info=response.request_info,
                                    history=response.history,
                                    status=response.status,
                                    message=f"HTTP {response.status}",
                                    headers=response.headers,
                                )
                            content = await response.read()
                            dirpath = os.path.dirname(save_path)
                            if dirpath:
                                os.makedirs(dirpath, exist_ok=True)
                            with open(save_path, "wb") as file:
                                file.write(content)
                return {"url": url, "save_path": save_path, "success": True}
            except (
                aiohttp.ClientConnectorError,
                aiohttp.ClientPayloadError,
                aiohttp.ClientResponseError,
                asyncio.TimeoutError,
            ) as error:
                last_exc = error
                status = (
                    error.status
                    if isinstance(error, aiohttp.ClientResponseError)
                    else None
                )
                is_status_retry = status in retry_statuses if status is not None else True
                can_retry = attempt < retries and is_status_retry
                if not can_retry:
                    failure_kind: DownloadFailureKind
                    if isinstance(error, aiohttp.ClientResponseError):
                        failure_kind = (
                            "http_4xx" if 400 <= error.status < 500 else "http_5xx"
                        )
                    elif isinstance(error, asyncio.TimeoutError):
                        failure_kind = "timeout"
                    elif isinstance(error, aiohttp.ClientConnectorError):
                        failure_kind = "connection"
                    else:
                        failure_kind = "payload"
                    result: DownloadFileResult = {
                        "url": url,
                        "save_path": save_path,
                        "success": False,
                        "error": str(error),
                        "failure_kind": failure_kind,
                    }
                    if status is not None:
                        result["http_status"] = status
                    return result
                wait = backoff_factor * (2**attempt)
                report_warning(
                    WarningCategory.DOWNLOAD_RETRY,
                    f"Download failed ({error}), retrying {attempt + 1}/{retries} "
                    f"after {wait:.1f}s: {url}"
                )
                await asyncio.sleep(wait)
                attempt += 1
            except Exception as error:
                return {
                    "url": url,
                    "save_path": save_path,
                    "success": False,
                    "error": (
                        f"{error}\n{traceback.format_exc().rstrip()}"
                    ),
                    "failure_kind": "unexpected_download",
                }
        if last_exc:
            return {
                "url": url,
                "save_path": save_path,
                "success": False,
                "error": str(last_exc),
                "failure_kind": "unexpected_download",
            }
        return {
            "url": url,
            "save_path": save_path,
            "success": False,
            "error": "unknown",
            "failure_kind": "unexpected_download",
        }

    async def download_all(url_filename_lists: list[DownloadTask]) -> DownloadSummary:
        effective_max_concurrency = effective_download_concurrency(max_concurrency)
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(limit=effective_max_concurrency)
        semaphore = asyncio.Semaphore(effective_max_concurrency)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks: list[asyncio.Task[DownloadFileResult]] = []
            for item in url_filename_lists:
                tasks.append(
                    asyncio.create_task(
                        fetch_and_save(
                            session,
                            item["url"],
                            item["save_path"],
                            semaphore,
                        )
                    )
                )
            succeeded: list[DownloadFileResult] = []
            failed: list[DownloadFileResult] = []
            total = len(tasks)
            completed = 0
            for task in asyncio.as_completed(tasks):
                result = await task
                completed += 1
                if result["success"]:
                    succeeded.append(result)
                else:
                    failed.append(result)
                if on_progress is not None:
                    on_progress(completed, total, result)
            return {"succeeded": succeeded, "failed": failed}

    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return {"succeeded": [], "failed": []}

    return asyncio.run(download_all(pending_downloads))

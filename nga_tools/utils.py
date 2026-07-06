from __future__ import annotations

import asyncio
import datetime
import hashlib
import os
import re
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, NotRequired, Optional, TypedDict
from urllib.parse import urlsplit

import aiohttp

from nga_tools import network_limits
from nga_tools.bbcode_convert import strip_bbcode_tags
from nga_tools.config import get_config
from nga_tools.console import report_info, report_warning


class DownloadTask(TypedDict):
    url: str
    save_path: str


class DownloadFileResult(TypedDict):
    url: str
    save_path: str
    success: bool
    error: NotRequired[str]


class DownloadSummary(TypedDict):
    succeeded: list[DownloadFileResult]
    failed: list[DownloadFileResult]


DownloadProgressCallback = Callable[[int, int, DownloadFileResult], None]


_CREATED_FOLDERS: set[str] = set()
_NGA_IMAGE_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9-][A-Za-z0-9_-]*"
    r"\.(?:jpg|jpeg|png|gif|webp)"
    r"(?:\.(?:thumb|thumb_s|thumb_ss|medium)\.jpg)?$",
    re.IGNORECASE,
)
_NGA_IMAGE_PATH_RE = re.compile(
    r"^/attachments/(mon_(\d{4})(\d{2}))/(\d{2})/([^/]+)$"
)


def _effective_download_concurrency(max_concurrency: int | None) -> int:
    image_concurrency = network_limits.get_image_concurrency()
    if max_concurrency is None:
        return image_concurrency
    if max_concurrency <= 0:
        raise ValueError("max_concurrency must be greater than 0.")
    return min(max_concurrency, image_concurrency)


WARNING_LOG_FILENAME = "warnings.log"


def warning_log_path(tid: int, aid: int | None) -> Path:
    return Path(get_folder(tid, aid)) / WARNING_LOG_FILENAME


def sha256(filepath: str) -> str:
    """
    计算文件的SHA256哈希值
    """
    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # 逐块读取文件以节省内存
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def get_folder(
    tid: int | str,
    aid: Optional[int | str],
    subfolder: Optional[str] = None,
    *,
    create: bool = True,
) -> str:
    if type(tid) is int:
        tid_part = str(tid)
    elif isinstance(tid, str):
        tid_part = tid
    else:
        raise TypeError("tid must be int or str")

    if type(aid) is int:
        aid_value = str(aid)
    elif aid is None or isinstance(aid, str):
        aid_value = aid
    else:
        raise TypeError("aid must be int, str, or None")

    aid_part = aid_value if aid_value else "all"
    folder = get_config().output_dir + "/" + tid_part + "_" + aid_part
    if subfolder:
        folder += "/" + subfolder

    if create and folder not in _CREATED_FOLDERS:
        _CREATED_FOLDERS.add(folder)
        os.makedirs(folder, exist_ok=True)

    return folder


def list_files_in_folder(folder: str, ends_with: str = "") -> list[str]:
    """
    列出指定文件夹中的所有文件
    """
    if not os.path.exists(folder):
        return []
    return [
        f
        for f in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, f)) and f.endswith(ends_with)
    ]


def download_files(
    url_filename_lists: list[DownloadTask],
    retries: int = 5,
    backoff_factor: float = 0.5,
    retry_statuses: tuple[int, ...] = (429, 500, 502, 503, 504),
    max_concurrency: int | None = None,
    on_progress: DownloadProgressCallback | None = None,
) -> DownloadSummary:
    """
    并发下载多个文件，带出错重试机制，限制最大并发数
    如果某文件下载失败则跳过该文件，不会中断整个下载流程
    返回: {"succeeded": [...], "failed": [...]}
    url_filename_lists: [{"url":..., "save_path":...},...]
    retries: 每个文件的最大重试次数
    backoff_factor: 指数退避基数（等待时间 = backoff_factor * 2 ** attempt）
    retry_statuses: 针对这些HTTP状态码进行重试
    max_concurrency: 本次调用的下载并发上限；未提供时使用全局图片下载上限
    on_progress: 每个文件完成后调用，参数为(已完成数, 总数, 下载结果)
    """

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
                # only hold the semaphore during the actual network+write operation
                async with semaphore:
                    async with network_limits.image_download_slot():
                        async with session.get(url) as response:
                            # treat certain HTTP errors as exceptions to trigger retry logic
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
                            with open(save_path, "wb") as f:
                                f.write(content)
                return {"url": url, "save_path": save_path, "success": True}
            except (
                aiohttp.ClientConnectorError,
                aiohttp.ClientPayloadError,
                aiohttp.ClientResponseError,
                asyncio.TimeoutError,
            ) as e:
                last_exc = e
                status = e.status if isinstance(e, aiohttp.ClientResponseError) else None
                is_status_retry = status in retry_statuses if status is not None else True
                can_retry = attempt < retries and is_status_retry
                if not can_retry:
                    report_warning(f"Download failed, skipping {url}: {e}")
                    return {
                        "url": url,
                        "save_path": save_path,
                        "success": False,
                        "error": str(e),
                    }
                wait = backoff_factor * (2**attempt)
                report_warning(
                    f"Download failed ({e}), retrying {attempt + 1}/{retries} "
                    f"after {wait:.1f}s: {url}"
                )
                await asyncio.sleep(wait)
                attempt += 1
            except Exception as e:
                report_warning(
                    f"Unexpected error downloading {url}, skipping: {e}\n"
                    f"{traceback.format_exc().rstrip()}"
                )
                return {
                    "url": url,
                    "save_path": save_path,
                    "success": False,
                    "error": str(e),
                }
        if last_exc:
            report_warning(f"Exhausted retries, skipping {url}: {last_exc}")
            return {
                "url": url,
                "save_path": save_path,
                "success": False,
                "error": str(last_exc),
            }
        return {
            "url": url,
            "save_path": save_path,
            "success": False,
            "error": "unknown",
        }

    async def download_all(url_filename_lists: list[DownloadTask]) -> DownloadSummary:
        effective_max_concurrency = _effective_download_concurrency(max_concurrency)
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

    # 检查文件是否在本地存在，如果存在则去除该条下载任务
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    if not pending_downloads:
        return {"succeeded": [], "failed": []}

    return asyncio.run(download_all(pending_downloads))


def delete_bbcode_tags(text: str) -> str:
    """
    删除文本中的BBCode标签
    """
    return strip_bbcode_tags(text)


def TODO(message: str) -> NoReturn:
    """
    标记待办事项
    """
    report_info(f"TODO: {message}")
    sys.exit(1)


def NGA_img_link_verify(url: str) -> bool:
    """
    验证NGA图片链接是否有效
    """
    # 形如https://img.nga.178.com/attachments/mon_202601/07/lsQ0-e21K1sT3cSu3-g8.webp.medium.jpg
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "img.nga.178.com":
        return False
    if parsed_url.fragment:
        return False

    path_match = _NGA_IMAGE_PATH_RE.fullmatch(parsed_url.path)
    if path_match is None:
        return False

    year = int(path_match.group(2))
    month = int(path_match.group(3))
    day = int(path_match.group(4))
    filename = path_match.group(5)
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False

    return bool(_NGA_IMAGE_FILENAME_RE.fullmatch(filename))


if __name__ == "__main__":
    sample_text = "[b]Bold Text[/b] and [url=http://example.com]Example Link[/url]"
    cleaned_text = delete_bbcode_tags(sample_text)
    report_info(f"Original Text: {sample_text}")
    report_info(f"Cleaned Text: {cleaned_text}")
    report_info(f"Word Count: {len(cleaned_text.split())}")

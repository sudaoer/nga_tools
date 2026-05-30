from __future__ import annotations

import asyncio
import hashlib
import os
import re
import sys
import traceback
from collections.abc import Awaitable
from typing import NoReturn, NotRequired, TypedDict

import aiohttp

from nga_tools.config import get_config


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


_CREATED_FOLDERS: set[str] = set()


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


def get_folder(tid: int | str, aid: int | str | None, subfolder: str | None = None) -> str:
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

    if folder not in _CREATED_FOLDERS:
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
    max_concurrency: int = 10,
) -> DownloadSummary:
    """
    并发下载多个文件，带出错重试机制，限制最大并发数
    如果某文件下载失败则跳过该文件，不会中断整个下载流程
    返回: {"succeeded": [...], "failed": [...]}
    url_filename_lists: [{"url":..., "save_path":...},...]
    retries: 每个文件的最大重试次数
    backoff_factor: 指数退避基数（等待时间 = backoff_factor * 2 ** attempt）
    retry_statuses: 针对这些HTTP状态码进行重试
    max_concurrency: 最多同时下载的文件数
    """

    async def fetch_and_save(
        session: aiohttp.ClientSession,
        url: str,
        save_path: str,
        semaphore: asyncio.Semaphore,
    ) -> DownloadFileResult:
        attempt = 0
        last_exc: BaseException | None = None
        while attempt <= retries:
            try:
                # only hold the semaphore during the actual network+write operation
                async with semaphore:
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
                    print(f"Download failed, skipping {url}: {e}")
                    return {
                        "url": url,
                        "save_path": save_path,
                        "success": False,
                        "error": str(e),
                    }
                wait = backoff_factor * (2**attempt)
                print(
                    f"Download failed ({e}), retrying {attempt + 1}/{retries} "
                    f"after {wait:.1f}s: {url}"
                )
                await asyncio.sleep(wait)
                attempt += 1
            except Exception as e:
                print(f"Unexpected error downloading {url}, skipping: {e}")
                traceback.print_exc()
                return {
                    "url": url,
                    "save_path": save_path,
                    "success": False,
                    "error": str(e),
                }
        if last_exc:
            print(f"Exhausted retries, skipping {url}: {last_exc}")
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
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(limit=max_concurrency)
        semaphore = asyncio.Semaphore(max_concurrency)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks: list[Awaitable[DownloadFileResult]] = []
            for item in url_filename_lists:
                tasks.append(fetch_and_save(session, item["url"], item["save_path"], semaphore))
            results = await asyncio.gather(*tasks)
            succeeded = [r for r in results if r["success"]]
            failed = [r for r in results if not r["success"]]
            return {"succeeded": succeeded, "failed": failed}

    # 检查文件是否在本地存在，如果存在则去除该条下载任务
    pending_downloads = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]

    return asyncio.run(download_all(pending_downloads))


# 从bbcode统计字数
def delete_bbcode_tags(text: str) -> str:
    """
    删除文本中的BBCode标签
    """
    # 定义BBCode标签的正则表达式模式
    bbcode_pattern = re.compile(r"\[/?[a-zA-Z]+(?:=[^\]]+)?\]")
    # 使用正则表达式替换BBCode标签为空字符串
    cleaned_text = bbcode_pattern.sub("", text)
    return cleaned_text


def TODO(message: str) -> NoReturn:
    """
    标记待办事项
    """
    print(f"TODO: {message}")
    sys.exit(1)


def NGA_img_link_verify(url: str) -> bool:
    """
    验证NGA图片链接是否有效
    """
    # 形如https://img.nga.178.com/attachments/mon_202601/07/lsQ0-e21K1sT3cSu3-g8.webp.medium.jpg
    # 需要验证中间的mon_yyyymm/dd部分，无需验证文件名和后缀
    pattern = re.compile(
        r"^https://img\.nga\.178\.com/attachments/mon_\d{6}/\d{2}/.+$"
    )
    return bool(pattern.match(url))


if __name__ == "__main__":
    sample_text = "[b]Bold Text[/b] and [url=http://example.com]Example Link[/url]"
    cleaned_text = delete_bbcode_tags(sample_text)
    print("Original Text:", sample_text)
    print("Cleaned Text:", cleaned_text)
    print("Word Count:", len(cleaned_text.split()))

import config
import os

def sha256(filepath: str) -> str:
    """
    计算文件的SHA256哈希值
    """
    import hashlib

    sha256_hash = hashlib.sha256()
    with open(filepath, "rb") as f:
        # 逐块读取文件以节省内存
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()

def get_folder(tid: int | str, aid: int | str, subfolder: str | None = None) -> str:

    if not hasattr(get_folder, "created_folders"):
        get_folder.created_folders = set()

    if type(tid) == int:
        tid = str(tid)
    if type(aid) == int:
        aid = str(aid)
    folder = config.OUTPUT_DIR + "/" + tid + "_" + (aid if aid else "all")
    if subfolder:
        folder += "/" + subfolder

    if folder not in get_folder.created_folders:
        get_folder.created_folders.add(folder)
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


import aiohttp



def download_files(
    url_filename_lists,
    retries: int = 5,
    backoff_factor: float = 0.5,
    retry_statuses: tuple = (429, 500, 502, 503, 504),
    max_concurrency: int = 10,
):
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
    import asyncio
    import traceback

    async def fetch_and_save(session, url, save_path, semaphore: asyncio.Semaphore):
        attempt = 0
        last_exc = None
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
            except (aiohttp.ClientConnectorError, aiohttp.ClientPayloadError, aiohttp.ClientResponseError, asyncio.TimeoutError) as e:
                last_exc = e
                status = getattr(e, "status", None)
                # decide whether to retry
                is_status_retry = status in retry_statuses if status is not None else True
                can_retry = attempt < retries and is_status_retry
                if not can_retry:
                    # exhausted retries or non-retryable status -> skip this file
                    print(f"Download failed, skipping {url}: {e}")
                    return {"url": url, "save_path": save_path, "success": False, "error": str(e)}
                wait = backoff_factor * (2 ** attempt)
                print(f"Download failed ({e}), retrying {attempt+1}/{retries} after {wait:.1f}s: {url}")
                await asyncio.sleep(wait)
                attempt += 1
            except Exception as e:
                # non-retryable unexpected error -> skip this file
                print(f"Unexpected error downloading {url}, skipping: {e}")
                traceback.print_exc()
                return {"url": url, "save_path": save_path, "success": False, "error": str(e)}
        # exhausted retries
        if last_exc:
            print(f"Exhausted retries, skipping {url}: {last_exc}")
            return {"url": url, "save_path": save_path, "success": False, "error": str(last_exc)}
        return {"url": url, "save_path": save_path, "success": False, "error": "unknown"}

    async def download_all(url_filename_lists):
        timeout = aiohttp.ClientTimeout(total=60)
        connector = aiohttp.TCPConnector(limit=max_concurrency)
        semaphore = asyncio.Semaphore(max_concurrency)
        async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
            tasks = []
            for item in url_filename_lists:
                url = item["url"]
                save_path = item["save_path"]
                tasks.append(fetch_and_save(session, url, save_path, semaphore))
            results = await asyncio.gather(*tasks)
            succeeded = [r for r in results if r.get("success")]
            failed = [r for r in results if not r.get("success")]
            return {"succeeded": succeeded, "failed": failed}
        

    #检查文件是否在本地存在，如果存在则去除该条下载任务
    url_filename_lists = [
        item for item in url_filename_lists if not os.path.exists(item["save_path"])
    ]
    

    return asyncio.run(download_all(url_filename_lists))

# 从bbcode统计字数
import re


def delete_bbcode_tags(text: str) -> str:
    """
    删除文本中的BBCode标签
    """
    # 定义BBCode标签的正则表达式模式
    bbcode_pattern = re.compile(r"\[/?[a-zA-Z]+(?:=[^\]]+)?\]")
    # 使用正则表达式替换BBCode标签为空字符串
    cleaned_text = bbcode_pattern.sub("", text)
    return cleaned_text


import sys


def TODO(message: str):
    """
    标记待办事项
    """
    print(f"TODO: {message}")
    sys.exit(1)


if __name__ == "__main__":
    sample_text = "[b]Bold Text[/b] and [url=http://example.com]Example Link[/url]"
    cleaned_text = delete_bbcode_tags(sample_text)
    print("Original Text:", sample_text)
    print("Cleaned Text:", cleaned_text)
    print("Word Count:", len(cleaned_text.split()))

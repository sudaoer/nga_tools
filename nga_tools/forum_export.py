from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from nga_tools.config import get_config
from nga_tools.ngaclient.client import ForumThread, ForumThreadPage, NGAForumPageError

POSTDATE_ORDER = "postdatedesc"
DEFAULT_PAGE_DELAY_SECONDS = 3
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_CODE = 2048


class ForumThreadPageClient(Protocol):
    def get_forum_thread_page(
        self,
        fid: int,
        page: int,
        *,
        order_by: str | None = None,
    ) -> ForumThreadPage: ...


class TextWriter(Protocol):
    def write(self, text: str, /) -> object: ...

    def flush(self) -> object: ...


class ForumPostdateThreadRecord(TypedDict):
    fid: int
    forumname: str
    page: int
    page_index: int
    tid: int
    aid: int
    author: str
    subject: str
    postdate: int
    postdate_text: str
    lastpost: int
    lastpost_text: str
    replies: int


ProgressStatus = Literal["fetching", "waiting", "retrying"]
SleepFunc = Callable[[float], None]
ForumPostdateScanProgressCallback = Callable[["ForumPostdateScanProgress"], None]


@dataclass(frozen=True)
class ForumPostdateScanProgress:
    fid: int
    page: int
    total_pages: int | None
    written_count: int
    status: ProgressStatus
    message: str


@dataclass(frozen=True)
class ForumPostdateScanResult:
    output_path: Path
    fids: list[int]
    page_count: int
    thread_count: int


def unique_fids(fids: Iterable[int]) -> list[int]:
    seen: set[int] = set()
    ordered_fids: list[int] = []
    for fid in fids:
        if fid in seen:
            continue
        seen.add(fid)
        ordered_fids.append(fid)
    return ordered_fids


def default_postdate_scan_output_path(now: datetime | None = None) -> Path:
    timestamp = (datetime.now() if now is None else now).strftime("%Y%m%d_%H%M%S")
    return (
        Path(get_config().output_dir)
        / "forum_sync"
        / f"postdatedesc_threads_{timestamp}.jsonl"
    )


def _timestamp_text(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def _thread_record(
    thread: ForumThread,
    *,
    page: int,
    page_index: int,
    forumname: str,
) -> ForumPostdateThreadRecord:
    return {
        "fid": thread["fid"],
        "forumname": forumname,
        "page": page,
        "page_index": page_index,
        "tid": thread["tid"],
        "aid": thread["authorid"],
        "author": thread["author"],
        "subject": thread["subject"],
        "postdate": thread["postdate"],
        "postdate_text": _timestamp_text(thread["postdate"]),
        "lastpost": thread["lastpost"],
        "lastpost_text": _timestamp_text(thread["lastpost"]),
        "replies": thread["replies"],
    }


def _is_rate_limited(error: NGAForumPageError) -> bool:
    return error.code == RATE_LIMIT_CODE and "刷新过快" in error.message


def _report_progress(
    progress_callback: ForumPostdateScanProgressCallback | None,
    *,
    fid: int,
    page: int,
    total_pages: int | None,
    written_count: int,
    status: ProgressStatus,
    message: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        ForumPostdateScanProgress(
            fid=fid,
            page=page,
            total_pages=total_pages,
            written_count=written_count,
            status=status,
            message=message,
        )
    )


def _fetch_postdate_page_with_retry(
    client: ForumThreadPageClient,
    *,
    fid: int,
    page: int,
    total_pages: int | None,
    written_count: int,
    page_delay_seconds: int,
    sleep_func: SleepFunc,
    progress_callback: ForumPostdateScanProgressCallback | None,
) -> ForumThreadPage:
    attempt = 0
    while True:
        _report_progress(
            progress_callback,
            fid=fid,
            page=page,
            total_pages=total_pages,
            written_count=written_count,
            status="fetching",
            message="请求版面主题",
        )
        try:
            return client.get_forum_thread_page(
                fid,
                page,
                order_by=POSTDATE_ORDER,
            )
        except NGAForumPageError as error:
            if not _is_rate_limited(error) or attempt >= MAX_RATE_LIMIT_RETRIES:
                raise
            attempt += 1
            wait_seconds = max(page_delay_seconds * 3, 10)
            _report_progress(
                progress_callback,
                fid=fid,
                page=page,
                total_pages=total_pages,
                written_count=written_count,
                status="retrying",
                message=(
                    f"刷新过快，等待{wait_seconds}秒后重试"
                    f"{attempt}/{MAX_RATE_LIMIT_RETRIES}"
                ),
            )
            sleep_func(wait_seconds)


def _write_page_records(
    output_file: TextWriter,
    page_data: ForumThreadPage,
) -> int:
    written_count = 0
    for page_index, thread in enumerate(page_data["threads"], start=1):
        record = _thread_record(
            thread,
            page=page_data["current_page"],
            page_index=page_index,
            forumname=page_data["forumname"],
        )
        output_file.write(json.dumps(record, ensure_ascii=False))
        output_file.write("\n")
        written_count += 1
    output_file.flush()
    return written_count


def scan_postdate_forum_threads(
    client: ForumThreadPageClient,
    *,
    fids: Sequence[int],
    output_path: Path,
    start_page: int = 1,
    page_delay_seconds: int = DEFAULT_PAGE_DELAY_SECONDS,
    sleep_func: SleepFunc = time.sleep,
    progress_callback: ForumPostdateScanProgressCallback | None = None,
) -> ForumPostdateScanResult:
    if start_page <= 0:
        raise ValueError("start_page必须大于0。")
    if page_delay_seconds <= 0:
        raise ValueError("page_delay_seconds必须大于0。")

    scan_fids = unique_fids(fids)
    if not scan_fids:
        raise ValueError("至少需要一个fid。")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    total_written = 0
    total_pages_scanned = 0

    with output_path.open("w", encoding="utf-8") as output_file:
        for fid_index, fid in enumerate(scan_fids, start=1):
            total_pages: int | None = None
            page = start_page
            while total_pages is None or page <= total_pages:
                page_data = _fetch_postdate_page_with_retry(
                    client,
                    fid=fid,
                    page=page,
                    total_pages=total_pages,
                    written_count=total_written,
                    page_delay_seconds=page_delay_seconds,
                    sleep_func=sleep_func,
                    progress_callback=progress_callback,
                )
                total_pages = page_data["total_page"]
                total_written += _write_page_records(output_file, page_data)
                total_pages_scanned += 1

                has_more_pages = page < total_pages
                has_more_fids = fid_index < len(scan_fids)
                if has_more_pages or has_more_fids:
                    _report_progress(
                        progress_callback,
                        fid=fid,
                        page=page,
                        total_pages=total_pages,
                        written_count=total_written,
                        status="waiting",
                        message=f"等待{page_delay_seconds}秒",
                    )
                    sleep_func(page_delay_seconds)
                page += 1

    return ForumPostdateScanResult(
        output_path=output_path,
        fids=scan_fids,
        page_count=total_pages_scanned,
        thread_count=total_written,
    )

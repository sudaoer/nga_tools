from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol, TypedDict

from nga_tools.config import get_config
from nga_tools.core.atomic import open_text_atomically
from nga_tools.forum.thread_store import ForumThreadStore, timestamp_text
from nga_tools.forum.timing import ForumSyncTimingCollector
from nga_tools.ngaclient.client import (
    FORUM_MIRROR_TYPE_BIT,
    ForumThread,
    ForumThreadPage,
    NGAForumPageError,
)

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


@dataclass(frozen=True)
class ForumPostdateDbSyncResult:
    db_path: Path
    fids: list[int]
    page_count: int
    thread_count: int
    inserted_count: int
    updated_count: int
    stopped_existing_count: int


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
    return timestamp_text(timestamp)


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

    with open_text_atomically(output_path) as output_file:
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


def sync_postdate_forum_threads_to_db(
    client: ForumThreadPageClient,
    *,
    fids: Sequence[int],
    store: ForumThreadStore | None = None,
    start_page: int = 1,
    page_delay_seconds: int = DEFAULT_PAGE_DELAY_SECONDS,
    refresh: bool = False,
    sleep_func: SleepFunc = time.sleep,
    progress_callback: ForumPostdateScanProgressCallback | None = None,
) -> ForumPostdateDbSyncResult:
    if start_page <= 0:
        raise ValueError("start_page必须大于0。")
    if page_delay_seconds <= 0:
        raise ValueError("page_delay_seconds必须大于0。")
    if start_page != 1 and not refresh:
        raise ValueError("start_page仅支持与refresh一起使用。")

    scan_fids = unique_fids(fids)
    if not scan_fids:
        raise ValueError("至少需要一个fid。")

    thread_store = ForumThreadStore() if store is None else store
    total_threads = 0
    total_inserted = 0
    total_updated = 0
    total_pages_scanned = 0
    stopped_existing_count = 0

    for fid_index, fid in enumerate(scan_fids, start=1):
        known_tids: set[int] = set() if refresh else thread_store.existing_tids(fid)
        total_pages: int | None = None
        page = start_page
        while total_pages is None or page <= total_pages:
            page_data = _fetch_postdate_page_with_retry(
                client,
                fid=fid,
                page=page,
                total_pages=total_pages,
                written_count=total_threads,
                page_delay_seconds=page_delay_seconds,
                sleep_func=sleep_func,
                progress_callback=progress_callback,
            )
            total_pages = page_data["total_page"]
            page_tids = {thread["tid"] for thread in page_data["threads"]}
            found_existing_tids = page_tids & known_tids
            upsert_result = thread_store.upsert_threads(fid, page_data["threads"])
            total_threads += upsert_result.total_count
            total_inserted += upsert_result.inserted_count
            total_updated += upsert_result.updated_count
            total_pages_scanned += 1

            stop_after_page = not refresh and bool(found_existing_tids)
            if stop_after_page:
                stopped_existing_count += len(found_existing_tids)

            has_more_pages = page < total_pages and not stop_after_page
            has_more_fids = fid_index < len(scan_fids)
            if has_more_pages or has_more_fids:
                _report_progress(
                    progress_callback,
                    fid=fid,
                    page=page,
                    total_pages=total_pages,
                    written_count=total_threads,
                    status="waiting",
                    message=f"等待{page_delay_seconds}秒",
                )
                sleep_func(page_delay_seconds)
            if stop_after_page:
                break
            page += 1

    return ForumPostdateDbSyncResult(
        db_path=thread_store.db_path,
        fids=scan_fids,
        page_count=total_pages_scanned,
        thread_count=total_threads,
        inserted_count=total_inserted,
        updated_count=total_updated,
        stopped_existing_count=stopped_existing_count,
    )


@dataclass(frozen=True)
class ForumDefaultScanProgress:
    fid: int
    page: int
    total_pages: int | None
    pages_cap: int
    fetched_count: int
    status: ProgressStatus
    message: str


@dataclass(frozen=True)
class ForumDefaultDbSyncResult:
    db_path: Path
    fids: list[int]
    page_count: int
    thread_count: int
    inserted_count: int
    updated_count: int
    stopped_at_watermark: bool
    watermark: int | None
    fresh_threads: tuple[ForumThread, ...]


ForumDefaultScanProgressCallback = Callable[[ForumDefaultScanProgress], None]


def _is_mirror_thread(thread: ForumThread) -> bool:
    return thread["is_forum"] or bool(thread["topic_type"] & FORUM_MIRROR_TYPE_BIT)


def _normal_lastposts_for_watermark(
    threads: Sequence[ForumThread],
    *,
    page_number: int,
) -> list[int]:
    """Return lastpost values of threads that participate in the watermark check.

    Forum-mirror slots are always excluded. On page 1 the leading pinned prefix
    (threads forced above their lastpost rank) is also excluded, because NGA
    pins sticky/announcement threads to the top regardless of lastpost.
    """
    non_mirror = [thread for thread in threads if not _is_mirror_thread(thread)]
    if not non_mirror:
        return []
    if page_number != 1:
        return [thread["lastpost"] for thread in non_mirror]

    page_max = max(thread["lastpost"] for thread in non_mirror)
    normal_lastposts: list[int] = []
    reached_normal = False
    for thread in non_mirror:
        if not reached_normal:
            if thread["lastpost"] >= page_max:
                reached_normal = True
                normal_lastposts.append(thread["lastpost"])
        else:
            normal_lastposts.append(thread["lastpost"])
    return normal_lastposts


def _fetch_default_page_with_retry(
    client: ForumThreadPageClient,
    *,
    fid: int,
    page: int,
    total_pages: int | None,
    fetched_count: int,
    pages_cap: int,
    page_delay_seconds: int,
    sleep_func: SleepFunc,
    progress_callback: ForumDefaultScanProgressCallback | None,
    timing_collector: ForumSyncTimingCollector | None,
) -> ForumThreadPage:
    attempt = 0
    while True:
        _report_default_progress(
            progress_callback,
            fid=fid,
            page=page,
            total_pages=total_pages,
            pages_cap=pages_cap,
            fetched_count=fetched_count,
            status="fetching",
            message="请求版面主题",
        )
        if timing_collector is not None:
            timing_collector.record_forum_page_request_attempt()
        try:
            if timing_collector is None:
                page_data = client.get_forum_thread_page(fid, page)
            else:
                with timing_collector.measure("forum_page_request"):
                    page_data = client.get_forum_thread_page(fid, page)
        except NGAForumPageError as error:
            if not _is_rate_limited(error) or attempt >= MAX_RATE_LIMIT_RETRIES:
                raise
            attempt += 1
            if timing_collector is not None:
                timing_collector.record_rate_limit_retry()
            wait_seconds = max(page_delay_seconds * 3, 10)
            _report_default_progress(
                progress_callback,
                fid=fid,
                page=page,
                total_pages=total_pages,
                pages_cap=pages_cap,
                fetched_count=fetched_count,
                status="retrying",
                message=(
                    f"刷新过快，等待{wait_seconds}秒后重试"
                    f"{attempt}/{MAX_RATE_LIMIT_RETRIES}"
                ),
            )
            if timing_collector is None:
                sleep_func(wait_seconds)
            else:
                with timing_collector.measure("rate_limit_wait"):
                    sleep_func(wait_seconds)
            continue

        if timing_collector is not None:
            timing_collector.record_successful_forum_page(
                len(page_data["threads"])
            )
        return page_data


def _report_default_progress(
    progress_callback: ForumDefaultScanProgressCallback | None,
    *,
    fid: int,
    page: int,
    total_pages: int | None,
    pages_cap: int,
    fetched_count: int,
    status: ProgressStatus,
    message: str,
) -> None:
    if progress_callback is None:
        return
    progress_callback(
        ForumDefaultScanProgress(
            fid=fid,
            page=page,
            total_pages=total_pages,
            pages_cap=pages_cap,
            fetched_count=fetched_count,
            status=status,
            message=message,
        )
    )


def sync_default_forum_threads_to_db(
    client: ForumThreadPageClient,
    *,
    fids_pages: dict[int, int],
    store: ForumThreadStore,
    page_delay_seconds: int = 0,
    sleep_func: SleepFunc = time.sleep,
    progress_callback: ForumDefaultScanProgressCallback | None = None,
    timing_collector: ForumSyncTimingCollector | None = None,
) -> ForumDefaultDbSyncResult:
    if page_delay_seconds < 0:
        raise ValueError("page_delay_seconds不能为负数。")

    if not fids_pages:
        raise ValueError("至少需要一个fid。")

    ordered_fids = list(fids_pages.keys())
    total_threads = 0
    total_inserted = 0
    total_updated = 0
    total_pages_scanned = 0
    any_stopped = False
    last_watermark: int | None = None
    fresh_threads_by_tid: dict[int, ForumThread] = {}

    for fid in ordered_fids:
        pages_cap = fids_pages[fid]
        if timing_collector is None:
            cutoff = store.max_normal_lastpost(fid)
        else:
            with timing_collector.measure("watermark_read"):
                cutoff = store.max_normal_lastpost(fid)
        if cutoff is not None:
            last_watermark = cutoff

        page_buffers: list[list[ForumThread]] = []
        fid_threads = 0
        total_pages: int | None = None
        page = 1
        crossed = False

        while True:
            page_data = _fetch_default_page_with_retry(
                client,
                fid=fid,
                page=page,
                total_pages=total_pages,
                fetched_count=total_threads + fid_threads,
                pages_cap=pages_cap,
                page_delay_seconds=page_delay_seconds,
                sleep_func=sleep_func,
                progress_callback=progress_callback,
                timing_collector=timing_collector,
            )
            total_pages = page_data["total_page"]
            threads = page_data["threads"]
            page_buffers.append(threads)
            fid_threads += len(threads)
            for thread in threads:
                fresh_threads_by_tid[thread["tid"]] = thread

            normal_lastposts = _normal_lastposts_for_watermark(
                threads,
                page_number=page,
            )
            if (
                cutoff is not None
                and not crossed
                and normal_lastposts
                and min(normal_lastposts) <= cutoff
            ):
                crossed = True

            max_page = min(pages_cap, total_pages)

            if crossed:
                if page < max_page:
                    if page_delay_seconds > 0:
                        _report_default_progress(
                            progress_callback,
                            fid=fid,
                            page=page,
                            total_pages=total_pages,
                            pages_cap=pages_cap,
                            fetched_count=total_threads + fid_threads,
                            status="waiting",
                            message=f"等待{page_delay_seconds}秒（重叠页）",
                        )
                        sleep_func(page_delay_seconds)
                    overlap_page = page + 1
                    overlap_data = _fetch_default_page_with_retry(
                        client,
                        fid=fid,
                        page=overlap_page,
                        total_pages=total_pages,
                        fetched_count=total_threads + fid_threads,
                        pages_cap=pages_cap,
                        page_delay_seconds=page_delay_seconds,
                        sleep_func=sleep_func,
                        progress_callback=progress_callback,
                        timing_collector=timing_collector,
                    )
                    overlap_threads = overlap_data["threads"]
                    page_buffers.append(overlap_threads)
                    fid_threads += len(overlap_threads)
                    for thread in overlap_threads:
                        fresh_threads_by_tid[thread["tid"]] = thread
                any_stopped = True
                break

            if page >= max_page:
                break

            if page_delay_seconds > 0:
                _report_default_progress(
                    progress_callback,
                    fid=fid,
                    page=page,
                    total_pages=total_pages,
                    pages_cap=pages_cap,
                    fetched_count=total_threads + fid_threads,
                    status="waiting",
                    message=f"等待{page_delay_seconds}秒",
                )
                sleep_func(page_delay_seconds)
            page += 1

        if timing_collector is None:
            upsert_results = store.upsert_fid_pages_atomically(fid, page_buffers)
        else:
            with timing_collector.measure("database_upsert"):
                upsert_results = store.upsert_fid_pages_atomically(
                    fid,
                    page_buffers,
                )
        fid_inserted = 0
        fid_updated = 0
        for upsert_result in upsert_results:
            fid_inserted += upsert_result.inserted_count
            fid_updated += upsert_result.updated_count
        total_threads += fid_threads
        total_inserted += fid_inserted
        total_updated += fid_updated
        total_pages_scanned += len(page_buffers)

    return ForumDefaultDbSyncResult(
        db_path=store.db_path,
        fids=ordered_fids,
        page_count=total_pages_scanned,
        thread_count=total_threads,
        inserted_count=total_inserted,
        updated_count=total_updated,
        stopped_at_watermark=any_stopped,
        watermark=last_watermark,
        fresh_threads=tuple(fresh_threads_by_tid.values()),
    )

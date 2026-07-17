from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag

from nga_tools.core.download_types import DownloadFileResult
from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.backup import image_store
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.html_images import effective_image_src
from nga_tools.backup.models import ParsedPostHtml, PostHtml
from nga_tools.console import (
    WarningCategory,
    report_info,
    report_progress,
    report_warning,
)
from nga_tools.timing import record_timing_metric, time_section


@dataclass(frozen=True)
class PostImageReference:
    image_index: int
    url: str
    valid: bool


@dataclass(frozen=True)
class PostImageReferenceScan:
    lou: int
    references: tuple[PostImageReference, ...]


@dataclass(frozen=True)
class ImageDownloadOutcome:
    succeeded_count: int
    failed: list[DownloadFileResult]


def parse_post_htmls_for_images(htmls: Sequence[PostHtml]) -> list[ParsedPostHtml]:
    parsed_htmls: list[ParsedPostHtml] = []
    for item in htmls:
        soup = BeautifulSoup(item["html"], "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        parsed_htmls.append(ParsedPostHtml(item, soup, images))
    return parsed_htmls


def collect_image_download_tasks_from_parsed(
    parsed_htmls: Sequence[ParsedPostHtml],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    return collect_image_download_tasks_from_scans(
        [scan_post_image_references(item) for item in parsed_htmls],
        floor_labels,
    )


def scan_post_image_references(
    parsed_html: ParsedPostHtml,
) -> PostImageReferenceScan:
    references: list[PostImageReference] = []
    for index, image in enumerate(parsed_html.images, start=1):
        normalized_image_url = effective_image_src(image)
        if normalized_image_url is None:
            continue
        references.append(
            PostImageReference(
                image_index=index,
                url=normalized_image_url,
                valid=NGA_img_link_verify(normalized_image_url),
            )
        )
    return PostImageReferenceScan(
        lou=parsed_html.post_html["lou"],
        references=tuple(references),
    )


def collect_image_download_tasks_from_scans(
    scans: Sequence[PostImageReferenceScan],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    seen_urls: set[str] = set()
    files_to_download: list[image_store.ImageDownloadTask] = []

    for scan in scans:
        for reference in scan.references:
            if not reference.valid:
                report_warning(
                    WarningCategory.IMAGE_DOWNLOAD,
                    f"{floor_labels.label(scan.lou)}的"
                    f"第{reference.image_index}张图片链接无效：{reference.url}"
                )
                continue

            if reference.url not in seen_urls:
                seen_urls.add(reference.url)
                files_to_download.append({"url": reference.url})

    return files_to_download


def _record_image_preparation_metrics(
    stats: image_store.ImagePreparationStats,
) -> None:
    record_timing_metric("图片任务URL数", stats.task_url_count)
    record_timing_metric("图片索引命中URL数", stats.mapping_hit_url_count)
    record_timing_metric("图片唯一物理路径数", stats.unique_physical_path_count)
    record_timing_metric("图片线程内路径去重数", stats.intra_thread_path_dedup_count)
    record_timing_metric(
        "图片内存缓存命中路径数",
        stats.memory_cache_hit_path_count,
    )
    record_timing_metric("图片深度校验路径数", stats.deep_validation_path_count)
    record_timing_metric(
        "图片持久化缓存命中路径数",
        stats.persistent_cache_hit_path_count,
    )
    record_timing_metric(
        "图片持久化缓存查询路径数",
        stats.persistent_cache_query_path_count,
    )
    record_timing_metric(
        "图片校验文件缺失路径数",
        stats.missing_validation_path_count,
    )
    record_timing_metric("图片无效映射数", stats.invalid_mapping_count)
    record_timing_metric("图片待下载URL数", stats.pending_download_url_count)


def _run_download_images(
    tid: int,
    aid: Optional[int],
    files_to_download: list[image_store.ImageDownloadTask],
    *,
    collect_successes: bool,
) -> tuple[
    int,
    list[DownloadFileResult],
    list[DownloadFileResult],
]:
    del tid, aid
    with time_section("图片下载准备"):
        preparation = image_store.prepare_image_download_tasks(files_to_download)
        pending_downloads = preparation.pending_tasks
        _record_image_preparation_metrics(preparation.stats)
        total_count = len(files_to_download)
        pending_count = len(pending_downloads)
        existing_count = total_count - pending_count
    report_progress(
        f"共{total_count}张图片，已存在{existing_count}张，"
        f"本次下载{pending_count}张",
        completed=0,
        total=pending_count,
    )

    succeeded_count = 0
    succeeded: list[DownloadFileResult] = []
    failed_results: list[DownloadFileResult] = []

    def update_progress(
        completed: int,
        total: int,
        _result: DownloadFileResult,
    ) -> None:
        report_progress(
            "图片下载进度",
            completed=completed,
            total=total,
        )

    with time_section("图片下载"):
        if pending_count > 0:
            if collect_successes:
                download_result = image_store.download_image_tasks(
                    pending_downloads,
                    on_progress=update_progress,
                )
                succeeded = download_result["succeeded"]
                succeeded_count = len(succeeded)
                failed_results = download_result["failed"]
            else:
                compact_result = image_store.download_image_tasks_compact(
                    pending_downloads,
                    on_progress=update_progress,
                )
                succeeded_count = compact_result["succeeded_count"]
                failed_results = compact_result["failed"]
        else:
            report_progress("图片下载进度", completed=0, total=0)

    report_info("图片下载完成。")
    report_info(
        f"成功下载{succeeded_count}个文件，"
        f"失败{len(failed_results)}个文件。"
    )
    for failed in failed_results:
        failed_url = failed["url"]
        failure_kind = failed.get("failure_kind", "unexpected_download")
        record_timing_metric(f"图片下载失败/{failure_kind}", 1)
        status_text = (
            f"，HTTP {failed['http_status']}" if "http_status" in failed else ""
        )
        error_text = _clean_repeated_url(failed.get("error", "unknown"), failed_url)
        report_warning(
            WarningCategory.IMAGE_DOWNLOAD,
            f"下载失败：{failed_url}（类别：{failure_kind}{status_text}，"
            f"详情：{error_text}）"
        )
    return succeeded_count, succeeded, failed_results


def download_images_compact(
    tid: int,
    aid: Optional[int],
    files_to_download: list[image_store.ImageDownloadTask],
) -> ImageDownloadOutcome:
    succeeded_count, _succeeded, failed = _run_download_images(
        tid,
        aid,
        files_to_download,
        collect_successes=False,
    )
    return ImageDownloadOutcome(succeeded_count, failed)


def _clean_repeated_url(error_text: str, url: str) -> str:
    """从错误详情中移除警告开头已经展示的 URL。"""
    escaped_url = re.escape(url)
    cleaned = re.sub(
        rf"(?:,\s*)?\burl\s*=\s*(?P<quote>['\"]?){escaped_url}(?P=quote)",
        "",
        error_text,
        flags=re.IGNORECASE,
    )
    cleaned = cleaned.replace(url, "").strip(" ,;")
    return cleaned or "unknown"

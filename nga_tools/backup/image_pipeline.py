from __future__ import annotations

from collections.abc import Sequence
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.html_images import effective_image_src
from nga_tools.backup.models import ParsedPostHtml, PostHtml
from nga_tools.console import report_info, report_progress, report_warning
from nga_tools.timing import time_section


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
    seen_urls: set[str] = set()
    files_to_download: list[image_store.ImageDownloadTask] = []

    for parsed_html in parsed_htmls:
        for index, image in enumerate(parsed_html.images):
            normalized_image_url = effective_image_src(image)
            if normalized_image_url is None:
                continue

            if not utils.NGA_img_link_verify(normalized_image_url):
                report_warning(
                    f"{floor_labels.label(parsed_html.post_html['lou'])}的"
                    f"第{index + 1}张图片链接无效：{normalized_image_url}"
                )
                continue

            if normalized_image_url not in seen_urls:
                seen_urls.add(normalized_image_url)
                files_to_download.append({"url": normalized_image_url})

    return files_to_download


def collect_image_download_tasks(
    htmls: list[PostHtml],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    return collect_image_download_tasks_from_parsed(
        parse_post_htmls_for_images(htmls),
        floor_labels,
    )


def pending_download_tasks(
    files_to_download: list[image_store.ImageDownloadTask],
) -> list[image_store.ImageDownloadTask]:
    return image_store.pending_image_download_tasks(files_to_download)


def download_images(
    tid: int,
    aid: Optional[int],
    files_to_download: list[image_store.ImageDownloadTask],
) -> utils.DownloadSummary:
    del tid, aid
    with time_section("图片下载准备"):
        pending_downloads = pending_download_tasks(files_to_download)
        total_count = len(files_to_download)
        pending_count = len(pending_downloads)
        existing_count = total_count - pending_count
    report_progress(
        f"共{total_count}张图片，已存在{existing_count}张，"
        f"本次下载{pending_count}张",
        completed=0,
        total=pending_count,
    )

    download_result: utils.DownloadSummary = {"succeeded": [], "failed": []}

    def update_progress(
        completed: int,
        total: int,
        _result: utils.DownloadFileResult,
    ) -> None:
        report_progress(
            "图片下载进度",
            completed=completed,
            total=total,
        )

    with time_section("图片下载"):
        if pending_count > 0:
            download_result = image_store.download_image_tasks(
                pending_downloads,
                on_progress=update_progress,
            )
        else:
            report_progress("图片下载进度", completed=0, total=0)

    report_info("图片下载完成。")
    report_info(
        f"成功下载{len(download_result['succeeded'])}个文件，"
        f"失败{len(download_result['failed'])}个文件。"
    )
    for failed in download_result["failed"]:
        report_warning(f"下载失败：{failed['url']}")
    return download_result

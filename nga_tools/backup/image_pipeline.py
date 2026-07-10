from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import html_modified_manifest, image_store
from nga_tools.backup.files import write_text_atomically
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.html_images import effective_image_src
from nga_tools.backup.models import ParsedPostHtml, PostHtml, PostRecord
from nga_tools.backup.post_html import source_hashes_by_lou
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


def rewrite_parsed_image_links(
    parsed_htmls: Sequence[ParsedPostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
    failed_image_urls: set[str] | None = None,
    image_lookup: image_store.ImageLookupCache | None = None,
    folder_html_modified: Path | None = None,
) -> set[int]:
    if folder_html_modified is None:
        folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    failed_image_urls = failed_image_urls or set()
    placeholder_image_src: str | None = None
    completed_lous: set[int] = set()

    for parsed_html in parsed_htmls:
        item = parsed_html.post_html
        item_complete = True
        for index, image in enumerate(parsed_html.images):
            normalized_image_url = effective_image_src(image)
            if normalized_image_url is None:
                continue

            if not utils.NGA_img_link_verify(normalized_image_url):
                item_complete = False
                continue

            if image_lookup is None:
                image_src = image_store.unique_image_src_from_html_dir(
                    normalized_image_url,
                    folder_html_modified,
                )
            else:
                image_src = image_lookup.unique_image_src_from_html_dir(
                    normalized_image_url,
                    folder_html_modified,
                )
            if image_src is None and normalized_image_url in failed_image_urls:
                if placeholder_image_src is None:
                    placeholder_image_src = image_store.placeholder_image_src_from_html_dir(
                        folder_html_modified,
                    )
                image_src = placeholder_image_src
                item_complete = False
            if image_src is None:
                item_complete = False
                report_warning(
                    f"{floor_labels.label(item['lou'])}的"
                    f"第{index + 1}张图片未找到已下载文件"
                )
                continue

            image["src"] = image_src

        item["html"] = str(parsed_html.soup)
        if item_complete:
            completed_lous.add(item["lou"])

    return completed_lous


def rewrite_image_links(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
    failed_image_urls: set[str] | None = None,
    image_lookup: image_store.ImageLookupCache | None = None,
    folder_html_modified: Path | None = None,
) -> set[int]:
    return rewrite_parsed_image_links(
        parse_post_htmls_for_images(htmls),
        tid,
        aid,
        floor_labels,
        failed_image_urls,
        image_lookup,
        folder_html_modified,
    )


def write_modified_htmls(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
    folder_html_modified: Path | None = None,
) -> dict[int, str]:
    if folder_html_modified is None:
        folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    output_hash_by_lou: dict[int, str] = {}
    for item in htmls:
        html = item["html"]
        path = folder_html_modified / html_modified_manifest.post_html_filename(
            item["lou"]
        )
        write_text_atomically(path, html)
        output_hash_by_lou[item["lou"]] = html_modified_manifest.hash_text(html)
    return output_hash_by_lou


def html_source_hashes_by_lou(htmls: list[PostHtml]) -> dict[int, str]:
    return {
        item["lou"]: html_modified_manifest.hash_text(item["html"])
        for item in htmls
    }


def completed_html_modified_lous(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
) -> tuple[
    Path,
    dict[int, str],
    dict[str, html_modified_manifest.HtmlModifiedManifestEntry],
    set[int],
]:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    source_hash_by_lou = html_source_hashes_by_lou(htmls)
    manifest_entries = html_modified_manifest.load_manifest(folder_html_modified)
    skipped_lous = html_modified_manifest.completed_post_lous(
        folder_html_modified,
        source_hash_by_lou,
        manifest_entries,
    )
    return folder_html_modified, source_hash_by_lou, manifest_entries, skipped_lous


def completed_html_modified_lous_for_records(
    records: Sequence[PostRecord],
    tid: int,
    aid: Optional[int],
) -> tuple[
    Path,
    dict[int, str],
    dict[str, html_modified_manifest.HtmlModifiedManifestEntry],
    set[int],
]:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    source_hash_by_lou = source_hashes_by_lou(records)
    manifest_entries = html_modified_manifest.load_manifest(folder_html_modified)
    skipped_lous = html_modified_manifest.completed_post_lous(
        folder_html_modified,
        source_hash_by_lou,
        manifest_entries,
    )
    return folder_html_modified, source_hash_by_lou, manifest_entries, skipped_lous


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


def failed_image_urls(download_result: utils.DownloadSummary) -> set[str]:
    return {
        image_store.normalize_nga_image_url(failed["url"])
        for failed in download_result["failed"]
        if utils.NGA_img_link_verify(image_store.normalize_nga_image_url(failed["url"]))
    }

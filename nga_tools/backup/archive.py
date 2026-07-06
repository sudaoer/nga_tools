from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TypedDict, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import backup_state, html_manifest, html_modified_manifest
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    PAGE_JSON_RE,
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    RecoveredMissingPost,
    build_and_save_floor_map,
    load_floor_map_build_result_if_current,
    load_floor_labels,
    read_missing_author_lous_from_html_modified,
)
from nga_tools.backup import image_store
from nga_tools.bbcode_convert import ImageSrcResolver, bbcode_to_html
from nga_tools.console import report_info, report_progress, report_warning
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData

_NGA_IMAGE_BASE_URL = "https://img.nga.178.com/attachments/"
_IMAGE_PATH_IN_TEXT_RE = re.compile(
    r"mon_\d{6}/\d{2}/[A-Za-z0-9-][A-Za-z0-9_-]*"
    r"\.(?:jpg|jpeg|png|gif|webp)"
    r"(?:\.(?:thumb|thumb_s|thumb_ss|medium)\.jpg)?",
    re.IGNORECASE,
)


class ImageAttachment(TypedDict):
    url: str
    path: str
    name: str


class PostData(TypedDict):
    lou: int
    pid: int
    content: str
    image_attachments: list[ImageAttachment]


class PostHtml(TypedDict):
    lou: int
    pid: Optional[int]
    html: str


class PostRecord(TypedDict):
    lou: int
    pid: Optional[int]
    html: Optional[str]
    source_hash: str
    html_hash: str


@dataclass(frozen=True)
class ParsedPostHtml:
    post_html: PostHtml
    soup: BeautifulSoup
    images: list[Tag]


def _page_posts(page_data: PageData) -> list[PostData]:
    raw_posts = page_data.get("result")
    if not isinstance(raw_posts, list):
        raise ValueError("NGA响应中缺少帖子列表。")

    posts: list[PostData] = []
    for raw_post in cast(list[object], raw_posts):
        if not isinstance(raw_post, dict):
            raise ValueError(f"NGA响应中的帖子不是对象：{raw_post!r}")
        post = cast(dict[str, object], raw_post)
        lou = post.get("lou")
        pid = post.get("pid")
        content = post.get("content")
        if type(lou) is not int or type(pid) is not int or not isinstance(content, str):
            raise ValueError(f"NGA响应中的帖子字段无效：{raw_post!r}")
        posts.append(
            {
                "lou": lou,
                "pid": pid,
                "content": content,
                "image_attachments": _post_image_attachments(post),
            }
        )

    return posts


def _attachment_url_from_value(value: str) -> Optional[str]:
    normalized_value = image_store.normalize_nga_image_url(value.strip())
    if utils.NGA_img_link_verify(normalized_value):
        return normalized_value

    if normalized_value.startswith("/attachments/"):
        candidate_url = "https://img.nga.178.com" + normalized_value
    else:
        while normalized_value.startswith("./"):
            normalized_value = normalized_value[2:]
        if normalized_value.startswith("attachments/"):
            normalized_value = normalized_value[len("attachments/") :]
        candidate_url = _NGA_IMAGE_BASE_URL + normalized_value.lstrip("/")

    candidate_url = image_store.normalize_nga_image_url(candidate_url)
    if utils.NGA_img_link_verify(candidate_url):
        return candidate_url
    return None


def _image_attachment_from_raw(raw_attachment: object) -> Optional[ImageAttachment]:
    if not isinstance(raw_attachment, dict):
        return None
    attachment = cast(dict[str, object], raw_attachment)
    if attachment.get("type") != "img":
        return None

    attachurl = attachment.get("attachurl")
    if not isinstance(attachurl, str):
        return None

    url = _attachment_url_from_value(attachurl)
    if url is None:
        return None

    path = urlsplit(url).path
    if not path.startswith("/attachments/"):
        return None

    image_path = path.removeprefix("/attachments/")
    return {
        "url": url,
        "path": image_path,
        "name": image_path.rsplit("/", 1)[-1],
    }


def _post_image_attachments(post: dict[str, object]) -> list[ImageAttachment]:
    raw_attachments = post.get("attches")
    if not isinstance(raw_attachments, list):
        return []

    image_attachments: list[ImageAttachment] = []
    for raw_attachment in cast(list[object], raw_attachments):
        image_attachment = _image_attachment_from_raw(raw_attachment)
        if image_attachment is not None:
            image_attachments.append(image_attachment)

    return image_attachments


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _attachment_index_for_image_src(
    image_src: str,
    attachments: list[ImageAttachment],
    start_index: int,
) -> Optional[int]:
    normalized_src = image_store.normalize_nga_image_url(image_src.strip())
    if utils.NGA_img_link_verify(normalized_src):
        for index, attachment in enumerate(attachments):
            if attachment["url"] == normalized_src:
                return index

    match = _IMAGE_PATH_IN_TEXT_RE.search(normalized_src)
    image_path = match.group(0).lower() if match is not None else ""
    src_lower = normalized_src.lower()
    index_order = [*range(start_index, len(attachments)), *range(0, start_index)]
    for index in index_order:
        attachment = attachments[index]
        attachment_path = attachment["path"].lower()
        attachment_name = attachment["name"].lower()
        if image_path and attachment_path == image_path:
            return index
        if attachment_path in src_lower or attachment_name in src_lower:
            return index

    return None


def _looks_like_relative_nga_image_src(image_src: str) -> bool:
    return _IMAGE_PATH_IN_TEXT_RE.search(image_src) is not None


def _make_image_src_resolver(
    attachments: list[ImageAttachment],
) -> ImageSrcResolver:
    next_attachment_index = 0

    def resolve_image_src(image_src: str) -> Optional[str]:
        nonlocal next_attachment_index

        normalized_src = image_store.normalize_nga_image_url(image_src.strip())
        if utils.NGA_img_link_verify(normalized_src):
            attachment_index = _attachment_index_for_image_src(
                normalized_src,
                attachments,
                next_attachment_index,
            )
            if attachment_index is not None:
                next_attachment_index = max(next_attachment_index, attachment_index + 1)
            return normalized_src

        attachment_index = _attachment_index_for_image_src(
            normalized_src,
            attachments,
            next_attachment_index,
        )
        if attachment_index is not None:
            next_attachment_index = max(next_attachment_index, attachment_index + 1)
            return attachments[attachment_index]["url"]

        if (
            _looks_like_relative_nga_image_src(normalized_src)
            and next_attachment_index < len(attachments)
        ):
            attachment = attachments[next_attachment_index]
            next_attachment_index += 1
            return attachment["url"]

        return None

    return resolve_image_src


def _post_html_from_content(post: PostData) -> str:
    return bbcode_to_html(
        post["content"],
        image_src_resolver=_make_image_src_resolver(post["image_attachments"]),
    )


def _html_has_invalid_image_src(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    images = cast(list[Tag], soup.find_all("img"))
    for image in images:
        image_src = _tag_attr_str(image, "src")
        if not image_src:
            continue
        normalized_image_src = image_store.normalize_nga_image_url(image_src)
        if not utils.NGA_img_link_verify(normalized_image_src):
            return True
    return False


def _read_page_json(path: Path) -> PageData:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"JSON备份文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"JSON备份文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"JSON备份文件顶层必须是对象：{path}")
    return cast(PageData, raw_data)


def _page_json_sort_key(path: Path) -> int:
    match = PAGE_JSON_RE.fullmatch(path.name)
    if not match:
        return 0
    return int(match.group(1))


def _existing_page_numbers(folder_json: Path) -> set[int]:
    page_numbers: set[int] = set()
    for path in folder_json.iterdir():
        if not path.is_file():
            continue
        match = PAGE_JSON_RE.fullmatch(path.name)
        if match:
            page_numbers.add(int(match.group(1)))
    return page_numbers


def _read_pages_json(folder_json: Path) -> dict[int, PageData]:
    page_data_by_page: dict[int, PageData] = {}
    page_paths = sorted(
        (
            path
            for path in folder_json.iterdir()
            if path.is_file() and PAGE_JSON_RE.fullmatch(path.name)
        ),
        key=_page_json_sort_key,
    )
    for path in page_paths:
        match = PAGE_JSON_RE.fullmatch(path.name)
        if match is None:
            continue
        page_data_by_page[int(match.group(1))] = _read_page_json(path)
    return page_data_by_page


def _write_page_json(folder_json: Path, page_number: int, page_data: PageData) -> None:
    path = folder_json / f"page_{page_number}.json"
    path.write_text(
        json.dumps(page_data, ensure_ascii=False, indent=4),
        encoding="utf-8",
    )


def _write_pages_json(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    page_count: int,
) -> dict[int, PageData]:
    folder_json = Path(utils.get_folder(tid, aid, "json"))
    page_data_by_page: dict[int, PageData] = {}
    for page_number in range(1, page_count + 1):
        report_progress(
            f"正在获取第{page_number}页",
            completed=page_number - 1,
            total=page_count,
        )
        page_data = client.get_page(tid, aid, page_number)
        _write_page_json(folder_json, page_number, page_data)
        page_data_by_page[page_number] = page_data
    report_progress(
        "页面获取完成",
        completed=page_count,
        total=page_count,
    )
    return page_data_by_page


def _post_source_hash(post: PostData) -> str:
    return html_manifest.hash_object(
        {
            "content": post["content"],
            "image_attachments": post["image_attachments"],
        }
    )


def _prepare_post_records(
    page_data_by_page: dict[int, PageData],
    tid: int,
    aid: Optional[int],
    refresh_pages: Optional[set[int]],
) -> list[PostRecord]:
    folder_html = Path(utils.get_folder(tid, aid, "html"))
    manifest_entries = html_manifest.load_manifest(folder_html)
    records: list[PostRecord] = []

    for page_number, page_data in sorted(page_data_by_page.items()):
        page_posts = _page_posts(page_data)
        for post in page_posts:
            filename = html_manifest.post_html_filename(post["lou"])
            html_path = folder_html / filename
            should_refresh = refresh_pages is None or page_number in refresh_pages
            source_hash = _post_source_hash(post)
            manifest_entry = manifest_entries.get(filename)
            post_html: str | None = None
            html_hash: str

            if (
                not should_refresh
                and manifest_entry is not None
                and manifest_entry["source_hash"] == source_hash
                and html_path.is_file()
            ):
                html_hash = manifest_entry["output_hash"]
            elif not should_refresh and html_path.is_file():
                cached_html = html_path.read_text(encoding="utf-8")
                if _html_has_invalid_image_src(cached_html):
                    post_html = _post_html_from_content(post)
                    _write_text_atomically(html_path, post_html)
                else:
                    post_html = cached_html
                html_hash = html_manifest.hash_text(post_html)
            else:
                post_html = _post_html_from_content(post)
                _write_text_atomically(html_path, post_html)
                html_hash = html_manifest.hash_text(post_html)

            records.append(
                {
                    "lou": post["lou"],
                    "pid": post["pid"],
                    "html": post_html,
                    "source_hash": source_hash,
                    "html_hash": html_hash,
                }
            )

    return records


def _write_html_manifest_for_records(
    records: Sequence[PostRecord],
    tid: int,
    aid: Optional[int],
) -> dict[str, html_manifest.HtmlManifestEntry]:
    folder_html = Path(utils.get_folder(tid, aid, "html"))
    entries: dict[str, html_manifest.HtmlManifestEntry] = {}
    for record in records:
        filename = html_manifest.post_html_filename(record["lou"])
        if not (folder_html / filename).is_file():
            continue
        entries[filename] = {
            "source_hash": record["source_hash"],
            "output_hash": record["html_hash"],
        }
    html_manifest.write_manifest(folder_html, entries)
    return entries


def _load_post_htmls_for_records(
    records: Sequence[PostRecord],
    tid: int,
    aid: Optional[int],
) -> list[PostHtml]:
    folder_html = Path(utils.get_folder(tid, aid, "html"))
    htmls: list[PostHtml] = []
    for record in records:
        post_html = record["html"]
        if post_html is None:
            post_html = (
                folder_html / html_manifest.post_html_filename(record["lou"])
            ).read_text(encoding="utf-8")
        htmls.append(
            {
                "lou": record["lou"],
                "pid": record["pid"],
                "html": post_html,
            }
        )
    return htmls


def _write_post_htmls(  # pyright: ignore[reportUnusedFunction]
    page_data_by_page: dict[int, PageData],
    tid: int,
    aid: Optional[int],
    refresh_pages: Optional[set[int]],
) -> list[PostHtml]:
    records = _prepare_post_records(page_data_by_page, tid, aid, refresh_pages)
    _write_html_manifest_for_records(records, tid, aid)
    return _load_post_htmls_for_records(records, tid, aid)


def _find_missing_lou(
    posts: Sequence[PostHtml] | Sequence[PostRecord],
) -> list[int]:
    expected_lou = 1
    missing_lou: list[int] = []
    for item in sorted(posts, key=lambda post: post["lou"]):
        if item["lou"] != expected_lou:
            for lou in range(expected_lou, item["lou"]):
                missing_lou.append(lou)
            expected_lou = item["lou"]
        expected_lou += 1

    return missing_lou


def _merge_missing_lou(*missing_lou_groups: list[int]) -> list[int]:
    return sorted(
        {
            lou
            for missing_lou_group in missing_lou_groups
            for lou in missing_lou_group
        }
    )


def _fill_missing_lou(  # pyright: ignore[reportUnusedFunction]
    htmls: list[PostHtml],
    missing_lou: list[int],
    floor_labels: FloorLabels,
    recovered_missing_html_by_lou: dict[int, str],
) -> None:
    for lou in missing_lou:
        if lou in recovered_missing_html_by_lou:
            report_info(f"已恢复缺失{floor_labels.label(lou)}。")
        else:
            report_warning(f"缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        htmls.append(
            {
                "lou": lou,
                "pid": None,
                "html": recovered_missing_html_by_lou.get(lou, MISSING_POST_HTML),
            }
        )

    htmls.sort(key=lambda item: item["lou"])


def _fill_missing_post_records(
    records: list[PostRecord],
    missing_lou: list[int],
    floor_labels: FloorLabels,
    recovered_missing_html_by_lou: dict[int, str],
) -> None:
    for lou in missing_lou:
        if lou in recovered_missing_html_by_lou:
            report_info(f"已恢复缺失{floor_labels.label(lou)}。")
        else:
            report_warning(f"缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        post_html = recovered_missing_html_by_lou.get(lou, MISSING_POST_HTML)
        html_hash = html_manifest.hash_text(post_html)
        records.append(
            {
                "lou": lou,
                "pid": None,
                "html": post_html,
                "source_hash": html_manifest.hash_text(f"missing:{html_hash}"),
                "html_hash": html_hash,
            }
        )

    records.sort(key=lambda item: item["lou"])


def _write_recovered_missing_post_htmls(
    tid: int,
    aid: Optional[int],
    recovered_missing_posts: dict[int, RecoveredMissingPost],
) -> dict[int, str]:
    if not recovered_missing_posts:
        return {}

    folder_html = Path(utils.get_folder(tid, aid, "html"))
    recovered_html_by_lou: dict[int, str] = {}
    for author_lou, recovered_post in sorted(recovered_missing_posts.items()):
        post_html = bbcode_to_html(recovered_post["content"])
        html_path = folder_html / f"post_{author_lou}.html"
        html_path.write_text(post_html, encoding="utf-8")
        recovered_html_by_lou[author_lou] = post_html
    return recovered_html_by_lou


def _parse_post_htmls_for_images(htmls: Sequence[PostHtml]) -> list[ParsedPostHtml]:
    parsed_htmls: list[ParsedPostHtml] = []
    for item in htmls:
        soup = BeautifulSoup(item["html"], "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        parsed_htmls.append(ParsedPostHtml(item, soup, images))
    return parsed_htmls


def _collect_image_download_tasks_from_parsed(
    parsed_htmls: Sequence[ParsedPostHtml],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    seen_urls: set[str] = set()
    files_to_download: list[image_store.ImageDownloadTask] = []

    for parsed_html in parsed_htmls:
        for index, image in enumerate(parsed_html.images):
            image_url = _tag_attr_str(image, "src")
            if not image_url:
                continue

            normalized_image_url = image_store.normalize_nga_image_url(image_url)
            if not utils.NGA_img_link_verify(normalized_image_url):
                report_warning(
                    f"{floor_labels.label(parsed_html.post_html['lou'])}的"
                    f"第{index + 1}张图片链接无效"
                )
                continue

            if normalized_image_url not in seen_urls:
                seen_urls.add(normalized_image_url)
                files_to_download.append({"url": normalized_image_url})

    return files_to_download


def _collect_image_download_tasks(  # pyright: ignore[reportUnusedFunction]
    htmls: list[PostHtml],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    return _collect_image_download_tasks_from_parsed(
        _parse_post_htmls_for_images(htmls),
        floor_labels,
    )


def _rewrite_parsed_image_links(
    parsed_htmls: Sequence[ParsedPostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
    failed_image_urls: set[str] | None = None,
    image_lookup: image_store.ImageLookupCache | None = None,
) -> set[int]:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    failed_image_urls = failed_image_urls or set()
    placeholder_image_src: str | None = None
    completed_lous: set[int] = set()

    for parsed_html in parsed_htmls:
        item = parsed_html.post_html
        item_complete = True
        for index, image in enumerate(parsed_html.images):
            image_url = _tag_attr_str(image, "src")
            if not image_url:
                continue

            normalized_image_url = image_store.normalize_nga_image_url(image_url)
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


def _rewrite_image_links(  # pyright: ignore[reportUnusedFunction]
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
    failed_image_urls: set[str] | None = None,
    image_lookup: image_store.ImageLookupCache | None = None,
) -> set[int]:
    return _rewrite_parsed_image_links(
        _parse_post_htmls_for_images(htmls),
        tid,
        aid,
        floor_labels,
        failed_image_urls,
        image_lookup,
    )


def _write_text_atomically(path: Path, text: str) -> None:
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def _write_modified_htmls(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
) -> dict[int, str]:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    output_hash_by_lou: dict[int, str] = {}
    for item in htmls:
        html = item["html"]
        path = folder_html_modified / html_modified_manifest.post_html_filename(
            item["lou"]
        )
        _write_text_atomically(path, html)
        output_hash_by_lou[item["lou"]] = html_modified_manifest.hash_text(html)
    return output_hash_by_lou


def _html_source_hashes_by_lou(htmls: list[PostHtml]) -> dict[int, str]:
    return {
        item["lou"]: html_modified_manifest.hash_text(item["html"])
        for item in htmls
    }


def _html_hashes_by_lou(records: Sequence[PostRecord]) -> dict[int, str]:
    return {record["lou"]: record["html_hash"] for record in records}


def _unresolved_missing_placeholder_lous(records: Sequence[PostRecord]) -> set[int]:
    missing_html_hash = html_manifest.hash_text(MISSING_POST_HTML)
    return {
        record["lou"]
        for record in records
        if record["pid"] is None and record["html_hash"] == missing_html_hash
    }


def _completed_html_modified_lous(  # pyright: ignore[reportUnusedFunction]
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
    source_hash_by_lou = _html_source_hashes_by_lou(htmls)
    manifest_entries = html_modified_manifest.load_manifest(folder_html_modified)
    skipped_lous = html_modified_manifest.completed_post_lous(
        folder_html_modified,
        source_hash_by_lou,
        manifest_entries,
    )
    return folder_html_modified, source_hash_by_lou, manifest_entries, skipped_lous


def _completed_html_modified_lous_for_records(
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
    source_hash_by_lou = _html_hashes_by_lou(records)
    manifest_entries = html_modified_manifest.load_manifest(folder_html_modified)
    skipped_lous = html_modified_manifest.completed_post_lous(
        folder_html_modified,
        source_hash_by_lou,
        manifest_entries,
    )
    return folder_html_modified, source_hash_by_lou, manifest_entries, skipped_lous


def _post_refs_from_posts(
    posts: Sequence[PostHtml] | Sequence[PostRecord],
) -> list[AuthorPostRef]:
    post_refs: list[AuthorPostRef] = []
    for item in posts:
        pid = item["pid"]
        if pid is None:
            continue
        post_refs.append({"pid": pid, "author_lou": item["lou"]})
    return post_refs


def _post_refs_from_htmls(htmls: list[PostHtml]) -> list[AuthorPostRef]:
    return _post_refs_from_posts(htmls)


def _page_count_from_page_data(page_data: PageData) -> int:
    total_pages = page_data.get("totalPage", 1)
    if not isinstance(total_pages, int):
        raise ValueError(f"Invalid totalPage value: {total_pages!r}")
    return total_pages


def _author_total_lou_count_from_page_data(
    page_data: PageData,
    aid: Optional[int],
) -> int | None:
    if aid is None:
        return None
    total_lous = page_data.get("vrows")
    if type(total_lous) is int:
        return total_lous
    return None


def _can_fast_skip_author_backup(
    tid: int,
    aid: Optional[int],
    author_total_lou_count: int | None,
) -> bool:
    if aid is None or author_total_lou_count is None:
        return False

    thread_folder = Path(utils.get_folder(tid, aid))
    state = backup_state.load_state(thread_folder)
    if state is None:
        return False
    if state["author_total_lou_count"] != author_total_lou_count:
        return False

    folder_json = Path(utils.get_folder(tid, aid, "json"))
    expected_pages = set(range(1, state["page_count"] + 1))
    if not expected_pages <= _existing_page_numbers(folder_json):
        return False

    folder_html = Path(utils.get_folder(tid, aid, "html"))
    html_entries = html_manifest.load_manifest(folder_html)
    if len(html_entries) != state["html_manifest_entry_count"]:
        return False
    if not html_manifest.manifest_files_exist(folder_html, html_entries):
        return False

    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    html_modified_entries = html_modified_manifest.load_manifest(folder_html_modified)
    if len(html_modified_entries) != state["html_modified_manifest_entry_count"]:
        return False
    if not html_modified_manifest.manifest_files_exist(
        folder_html_modified,
        html_modified_entries,
    ):
        return False

    return (thread_folder / "floor_map.json").is_file()


def _write_backup_state_if_complete(
    tid: int,
    aid: Optional[int],
    page_count: int,
    author_total_lou_count: int | None,
    records: Sequence[PostRecord],
    missing_lou: Sequence[int],
    html_entries: dict[str, html_manifest.HtmlManifestEntry],
    source_hash_by_lou: dict[int, str],
    skipped_lous: set[int],
    completed_lous: set[int],
) -> None:
    if aid is None or author_total_lou_count is None:
        return
    unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
    if unresolved_missing_lous:
        return
    if set(source_hash_by_lou) - (skipped_lous | completed_lous):
        return
    if (
        load_floor_map_build_result_if_current(
            tid,
            aid,
            _post_refs_from_posts(records),
            missing_lou,
        )
        is None
    ):
        return

    folder_html = Path(utils.get_folder(tid, aid, "html"))
    if not html_manifest.manifest_files_exist(folder_html, html_entries):
        return

    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    html_modified_entries = html_modified_manifest.load_manifest(folder_html_modified)
    if len(html_modified_entries) != len(source_hash_by_lou):
        return
    if not html_modified_manifest.manifest_files_exist(
        folder_html_modified,
        html_modified_entries,
    ):
        return

    backup_state.write_state(
        Path(utils.get_folder(tid, aid)),
        author_total_lou_count=author_total_lou_count,
        page_count=page_count,
        html_manifest_entry_count=len(html_entries),
        html_modified_manifest_entry_count=len(html_modified_entries),
        unresolved_missing_count=0,
    )


def _pending_download_tasks(
    files_to_download: list[image_store.ImageDownloadTask],
) -> list[image_store.ImageDownloadTask]:
    return image_store.pending_image_download_tasks(files_to_download)


def _download_images(
    tid: int,
    aid: Optional[int],
    files_to_download: list[image_store.ImageDownloadTask],
) -> utils.DownloadSummary:
    del tid, aid
    pending_downloads = _pending_download_tasks(files_to_download)
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


def _failed_image_urls(download_result: utils.DownloadSummary) -> set[str]:
    return {
        image_store.normalize_nga_image_url(failed["url"])
        for failed in download_result["failed"]
        if utils.NGA_img_link_verify(image_store.normalize_nga_image_url(failed["url"]))
    }


def _build_floor_map_for_backup(  # pyright: ignore[reportUnusedFunction]
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    htmls: list[PostHtml],
    missing_lou: list[int],
) -> FloorMapBuildResult:
    return _build_floor_map_for_post_refs(
        client,
        tid,
        aid,
        _post_refs_from_htmls(htmls),
        missing_lou,
    )


def _build_floor_map_for_post_refs(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    post_refs: list[AuthorPostRef],
    missing_lou: list[int],
) -> FloorMapBuildResult:
    if aid is None:
        return FloorMapBuildResult(FloorLabels.plain(), {})

    try:
        if not missing_lou:
            current_result = load_floor_map_build_result_if_current(
                tid,
                aid,
                post_refs,
                missing_lou,
            )
            if current_result is not None:
                report_info("楼层映射输入未变化，复用已有floor_map.json。")
                return current_result
        return build_and_save_floor_map(
            client,
            tid,
            aid,
            post_refs,
            missing_lou,
            strict=False,
        )
    except Exception as error:
        report_warning(f"楼层映射生成失败，继续生成备份：{error}")
        try:
            floor_labels = load_floor_labels(tid, aid)
        except Exception as load_error:
            report_warning(f"无法加载已有楼层映射，使用普通楼层标签：{load_error}")
            floor_labels = FloorLabels.plain()
        return FloorMapBuildResult(floor_labels, {})


def backup_thread(tid: int, aid: Optional[int]) -> None:
    client = NGAClient()
    first_page_data = client.get_page(tid, aid, 1)
    page_count = _page_count_from_page_data(first_page_data)
    author_total_lou_count = _author_total_lou_count_from_page_data(
        first_page_data,
        aid,
    )

    page_data_by_page = _write_pages_json(client, tid, aid, page_count)

    report_info("开始处理")

    records = _prepare_post_records(page_data_by_page, tid, aid, None)
    missing_lou = _find_missing_lou(records)
    floor_map_result = _build_floor_map_for_post_refs(
        client,
        tid,
        aid,
        _post_refs_from_posts(records),
        missing_lou,
    )
    floor_labels = floor_map_result.floor_labels
    recovered_missing_html_by_lou = _write_recovered_missing_post_htmls(
        tid,
        aid,
        floor_map_result.recovered_missing_posts_by_author_lou,
    )

    _fill_missing_post_records(
        records,
        missing_lou,
        floor_labels,
        recovered_missing_html_by_lou,
    )
    html_entries = _write_html_manifest_for_records(records, tid, aid)
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    source_hash_by_lou = _html_hashes_by_lou(records)
    htmls = _load_post_htmls_for_records(records, tid, aid)
    parsed_htmls = _parse_post_htmls_for_images(htmls)
    files_to_download = _collect_image_download_tasks_from_parsed(
        parsed_htmls,
        floor_labels,
    )
    download_result = _download_images(tid, aid, files_to_download)
    image_lookup = image_store.ImageLookupCache.for_tasks(files_to_download)
    completed_lous = set(
        _rewrite_parsed_image_links(
            parsed_htmls,
            tid,
            aid,
            floor_labels,
            _failed_image_urls(download_result),
            image_lookup,
        )
    )
    unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
    completed_lous -= unresolved_missing_lous
    output_hash_by_lou = _write_modified_htmls(htmls, tid, aid)
    html_modified_manifest.write_updated_manifest(
        folder_html_modified,
        previous_entries={},
        source_hash_by_lou=source_hash_by_lou,
        skipped_lous=set(),
        completed_lous=completed_lous,
        output_hash_by_lou=output_hash_by_lou,
    )
    _write_backup_state_if_complete(
        tid,
        aid,
        page_count,
        author_total_lou_count,
        records,
        missing_lou,
        html_entries,
        source_hash_by_lou,
        skipped_lous=set(),
        completed_lous=completed_lous,
    )


def backup_thread_sub(tid: int, aid: Optional[int]) -> None:
    client = NGAClient()
    first_page_data = client.get_page(tid, aid, 1)
    page_count = _page_count_from_page_data(first_page_data)
    author_total_lou_count = _author_total_lou_count_from_page_data(
        first_page_data,
        aid,
    )
    if _can_fast_skip_author_backup(tid, aid, author_total_lou_count):
        report_info("只看楼主总楼数未变化，跳过增量处理。")
        return

    folder_json = Path(utils.get_folder(tid, aid, "json"))
    existing_page_numbers = _existing_page_numbers(folder_json)

    if existing_page_numbers:
        tail_start = min(max(existing_page_numbers), page_count)
    else:
        tail_start = 1
    missing_page_numbers = set(range(1, page_count + 1)) - existing_page_numbers
    refresh_page_numbers = set(range(tail_start, page_count + 1)) | missing_page_numbers

    report_progress(
        f"准备增量备份：远端{page_count}页，本地{len(existing_page_numbers)}页，"
        f"需获取{len(refresh_page_numbers)}页",
        completed=0,
        total=len(refresh_page_numbers),
    )
    sorted_refresh_page_numbers = sorted(refresh_page_numbers)
    for index, page_number in enumerate(sorted_refresh_page_numbers, start=1):
        report_progress(
            f"正在获取第{page_number}页",
            completed=index - 1,
            total=len(sorted_refresh_page_numbers),
        )
        page_data = client.get_page(tid, aid, page_number)
        _write_page_json(folder_json, page_number, page_data)
    report_progress(
        "页面获取完成",
        completed=len(sorted_refresh_page_numbers),
        total=len(sorted_refresh_page_numbers),
    )

    page_data_by_page = _read_pages_json(folder_json)
    if not page_data_by_page:
        raise RuntimeError("没有可处理的JSON备份。")

    report_info("开始处理")

    records = _prepare_post_records(
        page_data_by_page,
        tid,
        aid,
        refresh_page_numbers,
    )
    missing_lou = _find_missing_lou(records)
    if aid is not None:
        present_lou = {item["lou"] for item in records}
        previous_missing_lou = [
            lou
            for lou in read_missing_author_lous_from_html_modified(tid, aid)
            if lou not in present_lou
        ]
        missing_lou = _merge_missing_lou(missing_lou, previous_missing_lou)
    floor_map_result = _build_floor_map_for_post_refs(
        client,
        tid,
        aid,
        _post_refs_from_posts(records),
        missing_lou,
    )
    floor_labels = floor_map_result.floor_labels
    recovered_missing_html_by_lou = _write_recovered_missing_post_htmls(
        tid,
        aid,
        floor_map_result.recovered_missing_posts_by_author_lou,
    )

    _fill_missing_post_records(
        records,
        missing_lou,
        floor_labels,
        recovered_missing_html_by_lou,
    )
    html_entries = _write_html_manifest_for_records(records, tid, aid)
    (
        folder_html_modified,
        source_hash_by_lou,
        manifest_entries,
        skipped_lous,
    ) = _completed_html_modified_lous_for_records(records, tid, aid)
    unresolved_missing_lous = _unresolved_missing_placeholder_lous(records)
    skipped_lous -= unresolved_missing_lous
    active_records = [item for item in records if item["lou"] not in skipped_lous]
    active_htmls = _load_post_htmls_for_records(active_records, tid, aid)

    parsed_htmls = _parse_post_htmls_for_images(active_htmls)
    files_to_download = _collect_image_download_tasks_from_parsed(
        parsed_htmls,
        floor_labels,
    )
    download_result = _download_images(tid, aid, files_to_download)
    image_lookup = image_store.ImageLookupCache.for_tasks(files_to_download)
    completed_lous = set(
        _rewrite_parsed_image_links(
            parsed_htmls,
            tid,
            aid,
            floor_labels,
            _failed_image_urls(download_result),
            image_lookup,
        )
    )
    completed_lous -= unresolved_missing_lous
    output_hash_by_lou = _write_modified_htmls(active_htmls, tid, aid)
    html_modified_manifest.write_updated_manifest(
        folder_html_modified,
        previous_entries=manifest_entries,
        source_hash_by_lou=source_hash_by_lou,
        skipped_lous=skipped_lous,
        completed_lous=completed_lous,
        output_hash_by_lou=output_hash_by_lou,
    )
    _write_backup_state_if_complete(
        tid,
        aid,
        page_count,
        author_total_lou_count,
        records,
        missing_lou,
        html_entries,
        source_hash_by_lou,
        skipped_lous=skipped_lous,
        completed_lous=completed_lous,
    )

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import html_manifest, image_store
from nga_tools.backup.files import write_text_atomically
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    AuthorPostRef,
    FloorLabels,
    RecoveredMissingPost,
)
from nga_tools.backup.models import ImageAttachment, PostData, PostHtml, PostRecord
from nga_tools.bbcode_convert import ImageSrcResolver, bbcode_to_html
from nga_tools.console import report_info, report_warning
from nga_tools.ngaclient.client import PageData

_NGA_IMAGE_BASE_URL = "https://img.nga.178.com/attachments/"
_IMAGE_PATH_IN_TEXT_RE = re.compile(
    r"mon_\d{6}/\d{2}/[A-Za-z0-9-][A-Za-z0-9_-]*"
    r"\.(?:jpg|jpeg|png|gif|webp)"
    r"(?:\.(?:thumb|thumb_s|thumb_ss|medium)\.jpg)?",
    re.IGNORECASE,
)


def page_posts(page_data: PageData) -> list[PostData]:
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
                "image_attachments": post_image_attachments(post),
            }
        )

    return posts


def attachment_url_from_value(value: str) -> Optional[str]:
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


def image_attachment_from_raw(raw_attachment: object) -> Optional[ImageAttachment]:
    if not isinstance(raw_attachment, dict):
        return None
    attachment = cast(dict[str, object], raw_attachment)
    if attachment.get("type") != "img":
        return None

    attachurl = attachment.get("attachurl")
    if not isinstance(attachurl, str):
        return None

    url = attachment_url_from_value(attachurl)
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


def post_image_attachments(post: dict[str, object]) -> list[ImageAttachment]:
    raw_attachments = post.get("attches")
    if not isinstance(raw_attachments, list):
        return []

    image_attachments: list[ImageAttachment] = []
    for raw_attachment in cast(list[object], raw_attachments):
        image_attachment = image_attachment_from_raw(raw_attachment)
        if image_attachment is not None:
            image_attachments.append(image_attachment)

    return image_attachments


def tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def attachment_index_for_image_src(
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


def looks_like_relative_nga_image_src(image_src: str) -> bool:
    return _IMAGE_PATH_IN_TEXT_RE.search(image_src) is not None


def make_image_src_resolver(
    attachments: list[ImageAttachment],
) -> ImageSrcResolver:
    next_attachment_index = 0

    def resolve_image_src(image_src: str) -> Optional[str]:
        nonlocal next_attachment_index

        normalized_src = image_store.normalize_nga_image_url(image_src.strip())
        if utils.NGA_img_link_verify(normalized_src):
            attachment_index = attachment_index_for_image_src(
                normalized_src,
                attachments,
                next_attachment_index,
            )
            if attachment_index is not None:
                next_attachment_index = max(next_attachment_index, attachment_index + 1)
            return normalized_src

        attachment_index = attachment_index_for_image_src(
            normalized_src,
            attachments,
            next_attachment_index,
        )
        if attachment_index is not None:
            next_attachment_index = max(next_attachment_index, attachment_index + 1)
            return attachments[attachment_index]["url"]

        if (
            looks_like_relative_nga_image_src(normalized_src)
            and next_attachment_index < len(attachments)
        ):
            attachment = attachments[next_attachment_index]
            next_attachment_index += 1
            return attachment["url"]

        return None

    return resolve_image_src


def post_html_from_content(post: PostData) -> str:
    return bbcode_to_html(
        post["content"],
        image_src_resolver=make_image_src_resolver(post["image_attachments"]),
    )


def html_has_invalid_image_src(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    images = cast(list[Tag], soup.find_all("img"))
    for image in images:
        image_src = tag_attr_str(image, "src")
        if not image_src:
            continue
        normalized_image_src = image_store.normalize_nga_image_url(image_src)
        if not utils.NGA_img_link_verify(normalized_image_src):
            return True
    return False


def post_source_hash(post: PostData) -> str:
    return html_manifest.hash_object(
        {
            "content": post["content"],
            "image_attachments": post["image_attachments"],
        }
    )


def prepare_post_records(
    page_data_by_page: dict[int, PageData],
    tid: int,
    aid: Optional[int],
    refresh_pages: Optional[set[int]],
) -> list[PostRecord]:
    folder_html = Path(utils.get_folder(tid, aid, "html"))
    manifest_entries = html_manifest.load_manifest(folder_html)
    records: list[PostRecord] = []

    for page_number, page_data in sorted(page_data_by_page.items()):
        for post in page_posts(page_data):
            filename = html_manifest.post_html_filename(post["lou"])
            html_path = folder_html / filename
            should_refresh = refresh_pages is None or page_number in refresh_pages
            source_hash = post_source_hash(post)
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
                if html_has_invalid_image_src(cached_html):
                    post_html = post_html_from_content(post)
                    write_text_atomically(html_path, post_html)
                else:
                    post_html = cached_html
                html_hash = html_manifest.hash_text(post_html)
            else:
                post_html = post_html_from_content(post)
                write_text_atomically(html_path, post_html)
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


def write_html_manifest_for_records(
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


def load_post_htmls_for_records(
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


def write_post_htmls(
    page_data_by_page: dict[int, PageData],
    tid: int,
    aid: Optional[int],
    refresh_pages: Optional[set[int]],
) -> list[PostHtml]:
    records = prepare_post_records(page_data_by_page, tid, aid, refresh_pages)
    write_html_manifest_for_records(records, tid, aid)
    return load_post_htmls_for_records(records, tid, aid)


def find_missing_lou(
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


def merge_missing_lou(*missing_lou_groups: list[int]) -> list[int]:
    return sorted(
        {
            lou
            for missing_lou_group in missing_lou_groups
            for lou in missing_lou_group
        }
    )


def fill_missing_lou(
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


def fill_missing_post_records(
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


def write_recovered_missing_post_htmls(
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


def html_hashes_by_lou(records: Sequence[PostRecord]) -> dict[int, str]:
    return {record["lou"]: record["html_hash"] for record in records}


def unresolved_missing_placeholder_lous(records: Sequence[PostRecord]) -> set[int]:
    missing_html_hash = html_manifest.hash_text(MISSING_POST_HTML)
    return {
        record["lou"]
        for record in records
        if record["pid"] is None and record["html_hash"] == missing_html_hash
    }


def post_refs_from_posts(
    posts: Sequence[PostHtml] | Sequence[PostRecord],
) -> list[AuthorPostRef]:
    post_refs: list[AuthorPostRef] = []
    for item in posts:
        pid = item["pid"]
        if pid is None:
            continue
        post_refs.append({"pid": pid, "author_lou": item["lou"]})
    return post_refs


def post_refs_from_htmls(htmls: list[PostHtml]) -> list[AuthorPostRef]:
    return post_refs_from_posts(htmls)


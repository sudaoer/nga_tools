from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional, TypedDict, cast
from urllib.parse import urlsplit

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    PAGE_JSON_RE,
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    RecoveredMissingPost,
    build_and_save_floor_map,
    load_floor_labels,
    read_missing_author_lous_from_html_modified,
)
from nga_tools.backup import image_store
from nga_tools.bbcode_convert import ImageSrcResolver, bbcode_to_html
from nga_tools.console import InlineProgress
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
    progress = InlineProgress()
    try:
        for page_number in range(1, page_count + 1):
            progress.update(f"正在获取第{page_number}页...")
            page_data = client.get_page(tid, aid, page_number)
            _write_page_json(folder_json, page_number, page_data)
            page_data_by_page[page_number] = page_data
    finally:
        progress.finish()
    return page_data_by_page


def _write_post_htmls(
    page_data_by_page: dict[int, PageData],
    tid: int,
    aid: Optional[int],
    refresh_pages: Optional[set[int]],
) -> list[PostHtml]:
    folder_html = Path(utils.get_folder(tid, aid, "html"))
    htmls: list[PostHtml] = []

    for page_number, page_data in sorted(page_data_by_page.items()):
        page_posts = _page_posts(page_data)
        for post in page_posts:
            html_path = folder_html / f"post_{post['lou']}.html"
            should_refresh = refresh_pages is None or page_number in refresh_pages
            if should_refresh or not html_path.exists():
                post_html = _post_html_from_content(post)
                html_path.write_text(post_html, encoding="utf-8")
            else:
                post_html = html_path.read_text(encoding="utf-8")
                if _html_has_invalid_image_src(post_html):
                    post_html = _post_html_from_content(post)
                    html_path.write_text(post_html, encoding="utf-8")
            htmls.append(
                {"lou": post["lou"], "pid": post["pid"], "html": post_html}
            )

    return htmls


def _find_missing_lou(htmls: list[PostHtml]) -> list[int]:
    htmls.sort(key=lambda item: item["lou"])

    expected_lou = 1
    missing_lou: list[int] = []
    for item in htmls:
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


def _fill_missing_lou(
    htmls: list[PostHtml],
    missing_lou: list[int],
    floor_labels: FloorLabels,
    recovered_missing_html_by_lou: dict[int, str],
) -> None:
    for lou in missing_lou:
        if lou in recovered_missing_html_by_lou:
            print(f"已恢复缺失{floor_labels.label(lou)}。")
        else:
            print(f"警告：缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        htmls.append(
            {
                "lou": lou,
                "pid": None,
                "html": recovered_missing_html_by_lou.get(lou, MISSING_POST_HTML),
            }
        )

    htmls.sort(key=lambda item: item["lou"])


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


def _collect_image_download_tasks(
    htmls: list[PostHtml],
    floor_labels: FloorLabels,
) -> list[image_store.ImageDownloadTask]:
    seen_urls: set[str] = set()
    files_to_download: list[image_store.ImageDownloadTask] = []

    for item in htmls:
        soup = BeautifulSoup(item["html"], "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        for index, image in enumerate(images):
            image_url = _tag_attr_str(image, "src")
            if not image_url:
                continue

            normalized_image_url = image_store.normalize_nga_image_url(image_url)
            if not utils.NGA_img_link_verify(normalized_image_url):
                print(
                    f"警告：{floor_labels.label(item['lou'])}的"
                    f"第{index + 1}张图片链接无效"
                )
                continue

            if normalized_image_url not in seen_urls:
                seen_urls.add(normalized_image_url)
                files_to_download.append({"url": normalized_image_url})

    return files_to_download


def _rewrite_image_links(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
    failed_image_urls: set[str] | None = None,
) -> None:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    failed_image_urls = failed_image_urls or set()
    placeholder_image_src: str | None = None

    for item in htmls:
        soup = BeautifulSoup(item["html"], "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        for index, image in enumerate(images):
            image_url = _tag_attr_str(image, "src")
            if not image_url:
                continue

            normalized_image_url = image_store.normalize_nga_image_url(image_url)
            if not utils.NGA_img_link_verify(normalized_image_url):
                continue

            image_src = image_store.unique_image_src_from_html_dir(
                normalized_image_url,
                folder_html_modified,
            )
            if image_src is None and normalized_image_url in failed_image_urls:
                if placeholder_image_src is None:
                    placeholder_image_src = image_store.placeholder_image_src_from_html_dir(
                        folder_html_modified,
                    )
                image_src = placeholder_image_src
            if image_src is None:
                print(
                    f"警告：{floor_labels.label(item['lou'])}的"
                    f"第{index + 1}张图片未找到已下载文件"
                )
                continue

            image["src"] = image_src

        item["html"] = str(soup)


def _write_modified_htmls(htmls: list[PostHtml], tid: int, aid: Optional[int]) -> None:
    folder_html_modified = utils.get_folder(tid, aid, "html_modified")
    for item in htmls:
        with open(
            f"{folder_html_modified}/post_{item['lou']}.html",
            "w",
            encoding="utf-8",
        ) as file:
            file.write(item["html"])


def _post_refs_from_htmls(htmls: list[PostHtml]) -> list[AuthorPostRef]:
    post_refs: list[AuthorPostRef] = []
    for item in htmls:
        pid = item["pid"]
        if pid is None:
            continue
        post_refs.append({"pid": pid, "author_lou": item["lou"]})
    return post_refs


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
    print(
        f"共{total_count}张图片，已存在{existing_count}张，"
        f"本次下载{pending_count}张。"
    )

    progress = InlineProgress()
    download_result: utils.DownloadSummary = {"succeeded": [], "failed": []}

    def update_progress(
        completed: int,
        total: int,
        _result: utils.DownloadFileResult,
    ) -> None:
        progress.update(f"下载进度：{completed}/{total}")

    progress.update(f"下载进度：0/{pending_count}")
    try:
        if pending_count > 0:
            download_result = image_store.download_image_tasks(
                pending_downloads,
                on_progress=update_progress,
            )
    finally:
        progress.finish()

    print("图片下载完成。")
    print(
        f"成功下载{len(download_result['succeeded'])}个文件，"
        f"失败{len(download_result['failed'])}个文件。"
    )
    for failed in download_result["failed"]:
        print(f"下载失败：{failed['url']}，保存为：{failed['save_path']}")
    return download_result


def _failed_image_urls(download_result: utils.DownloadSummary) -> set[str]:
    return {
        image_store.normalize_nga_image_url(failed["url"])
        for failed in download_result["failed"]
        if utils.NGA_img_link_verify(image_store.normalize_nga_image_url(failed["url"]))
    }


def _build_floor_map_for_backup(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    htmls: list[PostHtml],
    missing_lou: list[int],
) -> FloorMapBuildResult:
    if aid is None:
        return FloorMapBuildResult(FloorLabels.plain(), {})

    try:
        return build_and_save_floor_map(
            client,
            tid,
            aid,
            _post_refs_from_htmls(htmls),
            missing_lou,
            strict=False,
        )
    except Exception as error:
        print(f"警告：楼层映射生成失败，继续生成备份：{error}")
        try:
            floor_labels = load_floor_labels(tid, aid)
        except Exception as load_error:
            print(f"警告：无法加载已有楼层映射，使用普通楼层标签：{load_error}")
            floor_labels = FloorLabels.plain()
        return FloorMapBuildResult(floor_labels, {})


def backup_thread(tid: int, aid: Optional[int]) -> None:
    client = NGAClient()
    page_count = client.get_page_count(tid, aid)

    page_data_by_page = _write_pages_json(client, tid, aid, page_count)

    print("开始处理")

    htmls = _write_post_htmls(page_data_by_page, tid, aid, None)
    missing_lou = _find_missing_lou(htmls)
    floor_map_result = _build_floor_map_for_backup(
        client,
        tid,
        aid,
        htmls,
        missing_lou,
    )
    floor_labels = floor_map_result.floor_labels
    recovered_missing_html_by_lou = _write_recovered_missing_post_htmls(
        tid,
        aid,
        floor_map_result.recovered_missing_posts_by_author_lou,
    )

    _fill_missing_lou(htmls, missing_lou, floor_labels, recovered_missing_html_by_lou)
    files_to_download = _collect_image_download_tasks(htmls, floor_labels)
    download_result = _download_images(tid, aid, files_to_download)
    _rewrite_image_links(
        htmls,
        tid,
        aid,
        floor_labels,
        _failed_image_urls(download_result),
    )
    _write_modified_htmls(htmls, tid, aid)


def backup_thread_sub(tid: int, aid: Optional[int]) -> None:
    client = NGAClient()
    page_count = client.get_page_count(tid, aid)
    folder_json = Path(utils.get_folder(tid, aid, "json"))
    existing_page_numbers = _existing_page_numbers(folder_json)

    if existing_page_numbers:
        tail_start = min(max(existing_page_numbers), page_count)
    else:
        tail_start = 1
    missing_page_numbers = set(range(1, page_count + 1)) - existing_page_numbers
    refresh_page_numbers = set(range(tail_start, page_count + 1)) | missing_page_numbers

    print(
        f"准备增量备份：远端{page_count}页，本地{len(existing_page_numbers)}页，"
        f"需获取{len(refresh_page_numbers)}页。"
    )
    progress = InlineProgress()
    try:
        for page_number in sorted(refresh_page_numbers):
            progress.update(f"正在获取第{page_number}页...")
            page_data = client.get_page(tid, aid, page_number)
            _write_page_json(folder_json, page_number, page_data)
    finally:
        progress.finish()

    page_data_by_page = _read_pages_json(folder_json)
    if not page_data_by_page:
        raise RuntimeError("没有可处理的JSON备份。")

    print("开始处理")

    htmls = _write_post_htmls(
        page_data_by_page,
        tid,
        aid,
        refresh_page_numbers,
    )
    missing_lou = _find_missing_lou(htmls)
    if aid is not None:
        present_lou = {item["lou"] for item in htmls}
        previous_missing_lou = [
            lou
            for lou in read_missing_author_lous_from_html_modified(tid, aid)
            if lou not in present_lou
        ]
        missing_lou = _merge_missing_lou(missing_lou, previous_missing_lou)
    floor_map_result = _build_floor_map_for_backup(
        client,
        tid,
        aid,
        htmls,
        missing_lou,
    )
    floor_labels = floor_map_result.floor_labels
    recovered_missing_html_by_lou = _write_recovered_missing_post_htmls(
        tid,
        aid,
        floor_map_result.recovered_missing_posts_by_author_lou,
    )

    _fill_missing_lou(htmls, missing_lou, floor_labels, recovered_missing_html_by_lou)
    files_to_download = _collect_image_download_tasks(htmls, floor_labels)
    download_result = _download_images(tid, aid, files_to_download)
    _rewrite_image_links(
        htmls,
        tid,
        aid,
        floor_labels,
        _failed_image_urls(download_result),
    )
    _write_modified_htmls(htmls, tid, aid)

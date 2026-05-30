from __future__ import annotations

import json
from typing import Optional, TypedDict, cast

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup.floor_map import (
    MISSING_POST_HTML,
    AuthorPostRef,
    FloorLabels,
    build_and_save_floor_map,
)
from nga_tools.bbcode_convert import bbcode_to_html
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData


class PostData(TypedDict):
    lou: int
    pid: int
    content: str


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
        posts.append({"lou": lou, "pid": pid, "content": content})

    return posts


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _image_filename_from_url(image_url: str) -> str:
    return image_url.split("/")[-1].split("?")[0]


def _write_pages_json(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    page_count: int,
) -> None:
    folder_json = utils.get_folder(tid, aid, "json")
    for page_number in range(1, page_count + 1):
        print(f"正在获取第{page_number}页...")
        page_data = client.get_page(tid, aid, page_number)
        with open(
            f"{folder_json}/page_{page_number}.json",
            "w",
            encoding="utf-8",
        ) as file:
            json.dump(page_data, file, ensure_ascii=False, indent=4)


def _write_post_htmls(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    page_count: int,
) -> list[PostHtml]:
    folder_html = utils.get_folder(tid, aid, "html")
    htmls: list[PostHtml] = []

    for page_number in range(1, page_count + 1):
        page_posts = _page_posts(client.get_page(tid, aid, page_number))
        for post in page_posts:
            post_html = bbcode_to_html(post["content"])
            with open(
                f"{folder_html}/post_{post['lou']}.html",
                "w",
                encoding="utf-8",
            ) as file:
                file.write(post_html)
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


def _fill_missing_lou(
    htmls: list[PostHtml],
    missing_lou: list[int],
    floor_labels: FloorLabels,
) -> None:
    for lou in missing_lou:
        print(f"警告：缺失{floor_labels.label(lou)}！")

    for lou in missing_lou:
        htmls.append({"lou": lou, "pid": None, "html": MISSING_POST_HTML})

    htmls.sort(key=lambda item: item["lou"])


def _rewrite_image_links(
    htmls: list[PostHtml],
    tid: int,
    aid: Optional[int],
    floor_labels: FloorLabels,
) -> list[utils.DownloadTask]:
    seen_urls: set[str] = set()
    files_to_download: list[utils.DownloadTask] = []

    for item in htmls:
        soup = BeautifulSoup(item["html"], "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        for index, image in enumerate(images):
            image_url = _tag_attr_str(image, "src")
            if not image_url:
                continue

            image_filename = _image_filename_from_url(image_url)
            image["src"] = f"../images/{image_filename}"

            if not utils.NGA_img_link_verify(image_url):
                print(
                    f"警告：{floor_labels.label(item['lou'])}的"
                    f"第{index + 1}张图片链接无效"
                )

            if image_url not in seen_urls:
                seen_urls.add(image_url)
                save_path = (
                    utils.get_folder(tid, aid, "images") + f"/{image_filename}"
                )
                files_to_download.append({"url": image_url, "save_path": save_path})

        item["html"] = str(soup)

    return files_to_download


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


def backup_thread(tid: int, aid: Optional[int]) -> None:
    client = NGAClient()
    page_count = client.get_page_count(tid, aid)

    _write_pages_json(client, tid, aid, page_count)

    print("开始处理")

    htmls = _write_post_htmls(client, tid, aid, page_count)
    missing_lou = _find_missing_lou(htmls)
    floor_labels = FloorLabels.plain()
    if aid is not None:
        floor_labels = build_and_save_floor_map(
            client,
            tid,
            aid,
            _post_refs_from_htmls(htmls),
            missing_lou,
        )

    _fill_missing_lou(htmls, missing_lou, floor_labels)
    files_to_download = _rewrite_image_links(htmls, tid, aid, floor_labels)
    _write_modified_htmls(htmls, tid, aid)

    print(f"准备下载{len(files_to_download)}个图片文件...")
    utils.get_folder(tid, aid, "images")
    download_result = utils.download_files(files_to_download)
    print("图片下载完成。")
    print(
        f"成功下载{len(download_result['succeeded'])}个文件，"
        f"失败{len(download_result['failed'])}个文件。"
    )
    for failed in download_result["failed"]:
        print(f"下载失败：{failed['url']}，保存为：{failed['save_path']}")

from __future__ import annotations

from typing import Optional, cast
from urllib.parse import urlsplit

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.backup.models import ImageAttachment, PostData
from nga_tools.core.hashing import hash_text
from nga_tools.ngaclient.client import PageData

_NGA_IMAGE_BASE_URL = "https://img.nga.178.com/attachments/"


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


def post_data_from_raw(raw_post: object, source: str = "NGA响应") -> PostData:
    if not isinstance(raw_post, dict):
        raise ValueError(f"{source}中的帖子不是对象：{raw_post!r}")

    post = cast(dict[str, object], raw_post)
    lou = post.get("lou")
    pid = post.get("pid")
    content = post.get("content")
    if type(lou) is not int or type(pid) is not int or not isinstance(content, str):
        raise ValueError(f"{source}中的帖子字段无效：{raw_post!r}")

    return {
        "lou": lou,
        "pid": pid,
        "content": content,
        "image_attachments": post_image_attachments(post),
    }


def page_posts(page_data: PageData) -> list[PostData]:
    raw_posts = page_data.get("result")
    if not isinstance(raw_posts, list):
        raise ValueError("NGA响应中缺少帖子列表。")

    return [post_data_from_raw(raw_post) for raw_post in cast(list[object], raw_posts)]


def post_source_hash(post: PostData) -> str:
    return hash_text(post["content"])

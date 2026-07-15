from __future__ import annotations

from typing import cast

from nga_tools.backup.models import PostData
from nga_tools.core.hashing import hash_text
from nga_tools.ngaclient.client import PageData

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
    }


def page_posts(page_data: PageData) -> list[PostData]:
    raw_posts = page_data.get("result")
    if not isinstance(raw_posts, list):
        raise ValueError("NGA响应中缺少帖子列表。")

    return [post_data_from_raw(raw_post) for raw_post in cast(list[object], raw_posts)]


def post_source_hash(post: PostData) -> str:
    return hash_text(post["content"])

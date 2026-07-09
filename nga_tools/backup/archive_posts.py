from __future__ import annotations

import json
from typing import Optional, TypedDict, cast

from nga_tools.backup.models import ImageAttachment
from nga_tools.backup.post_data import post_image_attachments

PostDate = int | str


class ArchivePostMetadata(TypedDict):
    author_name: Optional[str]
    author_uid: Optional[int]
    postdate_json: Optional[str]
    image_attachments_json: str


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _post_author(post: dict[str, object]) -> tuple[Optional[str], Optional[int]]:
    raw_author = post.get("author")
    if not isinstance(raw_author, dict):
        return None, None
    author = cast(dict[str, object], raw_author)
    raw_name = author.get("username") or author.get("nickname")
    author_name = raw_name if isinstance(raw_name, str) and raw_name else None
    raw_uid = author.get("uid")
    author_uid = raw_uid if type(raw_uid) is int else None
    return author_name, author_uid


def _optional_postdate(post: dict[str, object]) -> Optional[PostDate]:
    value = post.get("postdate")
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped_value = value.strip()
        return stripped_value if stripped_value else None
    return None


def postdate_to_json(value: Optional[PostDate]) -> Optional[str]:
    if value is None:
        return None
    return _json_text(value)


def postdate_from_json(value: Optional[str]) -> Optional[PostDate]:
    if value is None:
        return None
    try:
        raw_value: object = json.loads(value)
    except json.JSONDecodeError:
        return None
    if type(raw_value) is int:
        return raw_value
    if isinstance(raw_value, str):
        stripped_value = raw_value.strip()
        return stripped_value if stripped_value else None
    return None


def image_attachments_to_json(attachments: list[ImageAttachment]) -> str:
    return _json_text(attachments)


def image_attachments_from_json(value: Optional[str]) -> list[ImageAttachment]:
    if value is None:
        return []
    try:
        raw_attachments: object = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw_attachments, list):
        return []

    attachments: list[ImageAttachment] = []
    for raw_attachment in cast(list[object], raw_attachments):
        if not isinstance(raw_attachment, dict):
            continue
        attachment = cast(dict[str, object], raw_attachment)
        url = attachment.get("url")
        path = attachment.get("path")
        name = attachment.get("name")
        if isinstance(url, str) and isinstance(path, str) and isinstance(name, str):
            attachments.append({"url": url, "path": path, "name": name})
    return attachments


def metadata_from_raw_post(raw_post: object) -> ArchivePostMetadata:
    post_dict: dict[str, object] = (
        cast(dict[str, object], raw_post) if isinstance(raw_post, dict) else {}
    )
    author_name, author_uid = _post_author(post_dict)
    return {
        "author_name": author_name,
        "author_uid": author_uid,
        "postdate_json": postdate_to_json(_optional_postdate(post_dict)),
        "image_attachments_json": image_attachments_to_json(
            post_image_attachments(post_dict)
        ),
    }

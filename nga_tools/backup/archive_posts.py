from __future__ import annotations

import json
from typing import Optional, TypedDict, cast

PostDate = int | str


class ArchivePostMetadata(TypedDict):
    author_name: Optional[str]
    author_uid: Optional[int]
    postdate_json: Optional[str]


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


def metadata_from_raw_post(raw_post: object) -> ArchivePostMetadata:
    post_dict: dict[str, object] = (
        cast(dict[str, object], raw_post) if isinstance(raw_post, dict) else {}
    )
    author_name, author_uid = _post_author(post_dict)
    return {
        "author_name": author_name,
        "author_uid": author_uid,
        "postdate_json": postdate_to_json(_optional_postdate(post_dict)),
    }

from __future__ import annotations

import datetime
import re
from collections.abc import Mapping, Sequence
from typing import Literal, TypedDict

from bs4 import BeautifulSoup

from nga_tools.backup.models import PostRecord
from nga_tools.bbcode_render import render_web_bbcode
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.html_sanitize import sanitize_post_html

POST_OVERLAYS_VERSION = 1
POST_OVERLAY_MODE_REPLACE: Literal["replace"] = "replace"

_BANNED_BBCODE_TAG_RE = re.compile(
    r"\[/?(?:img|flash)(?:\s|=|\])",
    re.IGNORECASE,
)


class PostOverlay(TypedDict):
    mode: Literal["replace"]
    bbcode: str
    content_hash: str
    updated_at: str


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def post_overlay_from_storage(
    *,
    mode: object,
    bbcode: object,
    content_hash: object,
    updated_at: object,
) -> PostOverlay:
    if mode != POST_OVERLAY_MODE_REPLACE:
        raise ValueError(f"overlay mode无效：{mode!r}")
    if not isinstance(bbcode, str) or not bbcode.strip():
        raise ValueError("overlay BBCode不能为空。")
    if not isinstance(content_hash, str) or content_hash != hash_text(bbcode):
        raise ValueError("overlay content_hash与BBCode不匹配。")
    if not isinstance(updated_at, str) or not updated_at:
        raise ValueError("overlay updated_at无效。")
    try:
        parsed_updated_at = datetime.datetime.fromisoformat(updated_at)
    except ValueError as error:
        raise ValueError("overlay updated_at不是有效ISO时间。") from error
    if parsed_updated_at.tzinfo is None:
        raise ValueError("overlay updated_at必须包含时区。")

    return {
        "mode": POST_OVERLAY_MODE_REPLACE,
        "bbcode": bbcode,
        "content_hash": content_hash,
        "updated_at": updated_at,
    }


def validate_overlay_bbcode(bbcode: str) -> None:
    if not bbcode.strip():
        raise ValueError("overlay BBCode不能为空。")
    if _BANNED_BBCODE_TAG_RE.search(bbcode):
        raise ValueError("overlay暂不支持图片或媒体外链。")


def render_overlay_html(bbcode: str) -> str:
    validate_overlay_bbcode(bbcode)
    html = sanitize_post_html(render_web_bbcode(bbcode))
    if BeautifulSoup(html, "html.parser").find("img") is not None:
        raise ValueError("overlay暂不支持图片或媒体外链。")
    return html


def make_post_overlay(bbcode: str) -> PostOverlay:
    render_overlay_html(bbcode)
    return {
        "mode": POST_OVERLAY_MODE_REPLACE,
        "bbcode": bbcode,
        "content_hash": hash_text(bbcode),
        "updated_at": _now_utc_iso(),
    }


def post_overlays_fingerprint(overlays: Mapping[int, PostOverlay]) -> str:
    return hash_object(
        {
            "version": POST_OVERLAYS_VERSION,
            "overlays": {
                str(lou): {
                    "mode": overlays[lou]["mode"],
                    "content_hash": overlays[lou]["content_hash"],
                }
                for lou in sorted(overlays)
            },
        }
    )


def source_hash_with_overlay(source_hash: str, overlay: PostOverlay) -> str:
    return hash_object(
        {
            "post_source_hash": source_hash,
            "post_overlay": {
                "version": POST_OVERLAYS_VERSION,
                "mode": overlay["mode"],
                "content_hash": overlay["content_hash"],
            },
        }
    )


def apply_post_overlays_to_records(
    overlays: Mapping[int, PostOverlay],
    records: Sequence[PostRecord],
) -> list[PostRecord]:
    if not overlays:
        return list(records)

    applied_records: list[PostRecord] = []
    for record in records:
        overlay = overlays.get(record["lou"])
        if overlay is None:
            applied_records.append(record)
            continue
        applied_records.append(
            {
                "lou": record["lou"],
                "pid": record["pid"],
                "post": None,
                "html": render_overlay_html(overlay["bbcode"]),
                "source_hash": source_hash_with_overlay(
                    record["source_hash"],
                    overlay,
                ),
            }
        )
    return applied_records


def source_hashes_by_lou_with_post_overlays(
    overlays: Mapping[int, PostOverlay],
    records: Sequence[PostRecord],
) -> dict[int, str]:
    source_hash_by_lou: dict[int, str] = {}
    for record in records:
        overlay = overlays.get(record["lou"])
        source_hash = record["source_hash"]
        if overlay is not None:
            source_hash = source_hash_with_overlay(source_hash, overlay)
        source_hash_by_lou[record["lou"]] = source_hash
    return source_hash_by_lou

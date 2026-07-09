from __future__ import annotations

import datetime
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, TypedDict, cast

from bs4 import BeautifulSoup

from nga_tools.backup.models import PostRecord
from nga_tools.bbcode_render import render_web_bbcode
from nga_tools.console import report_warning
from nga_tools.core.atomic import write_json_atomically
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.html_sanitize import sanitize_post_html

POST_OVERLAYS_FILENAME = "post_overlays.json"
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


class PostOverlayFile(TypedDict):
    version: int
    overlays: dict[str, PostOverlay]


def post_overlays_path(thread_folder: Path) -> Path:
    return thread_folder / POST_OVERLAYS_FILENAME


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _normalize_raw_overlay(raw_overlay: object) -> PostOverlay | None:
    if not isinstance(raw_overlay, dict):
        return None
    overlay = cast(dict[object, object], raw_overlay)
    if overlay.get("mode") != POST_OVERLAY_MODE_REPLACE:
        return None

    bbcode = overlay.get("bbcode")
    content_hash = overlay.get("content_hash")
    updated_at = overlay.get("updated_at")
    if not isinstance(bbcode, str) or not bbcode.strip():
        return None
    if not isinstance(content_hash, str) or content_hash != hash_text(bbcode):
        return None
    if not isinstance(updated_at, str) or not updated_at:
        return None

    return {
        "mode": POST_OVERLAY_MODE_REPLACE,
        "bbcode": bbcode,
        "content_hash": content_hash,
        "updated_at": updated_at,
    }


def load_post_overlays(thread_folder: Path) -> dict[int, PostOverlay]:
    path = post_overlays_path(thread_folder)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        report_warning(f"post overlay文件无效，按空overlay处理：{path}: {error}")
        return {}

    if not isinstance(raw_data, dict):
        report_warning(f"post overlay文件格式无效，按空overlay处理：{path}")
        return {}
    data = cast(dict[object, object], raw_data)
    if data.get("version") != POST_OVERLAYS_VERSION:
        return {}
    raw_overlays = data.get("overlays")
    if not isinstance(raw_overlays, dict):
        report_warning(f"post overlay文件缺少overlays对象，按空overlay处理：{path}")
        return {}

    overlays: dict[int, PostOverlay] = {}
    for raw_lou, raw_overlay in cast(dict[object, object], raw_overlays).items():
        if not isinstance(raw_lou, str) or not raw_lou.isdigit():
            continue
        lou = int(raw_lou)
        normalized_overlay = _normalize_raw_overlay(raw_overlay)
        if normalized_overlay is not None:
            overlays[lou] = normalized_overlay
    return overlays


def write_post_overlays(
    thread_folder: Path,
    overlays: dict[int, PostOverlay],
) -> None:
    payload: PostOverlayFile = {
        "version": POST_OVERLAYS_VERSION,
        "overlays": {
            str(lou): overlays[lou]
            for lou in sorted(overlays)
        },
    }
    write_json_atomically(
        post_overlays_path(thread_folder),
        payload,
        indent=2,
        trailing_newline=True,
    )


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


def save_post_overlay(thread_folder: Path, lou: int, bbcode: str) -> PostOverlay:
    overlays = load_post_overlays(thread_folder)
    overlay = make_post_overlay(bbcode)
    overlays[lou] = overlay
    write_post_overlays(thread_folder, overlays)
    return overlay


def clear_post_overlay(thread_folder: Path, lou: int) -> None:
    overlays = load_post_overlays(thread_folder)
    if lou not in overlays:
        return
    overlays.pop(lou)
    write_post_overlays(thread_folder, overlays)


def post_overlays_fingerprint(thread_folder: Path) -> str:
    overlays = load_post_overlays(thread_folder)
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
    thread_folder: Path,
    records: Sequence[PostRecord],
) -> list[PostRecord]:
    overlays = load_post_overlays(thread_folder)
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
    thread_folder: Path,
    records: Sequence[PostRecord],
) -> dict[int, str]:
    overlays = load_post_overlays(thread_folder)
    source_hash_by_lou: dict[int, str] = {}
    for record in records:
        overlay = overlays.get(record["lou"])
        source_hash = record["source_hash"]
        if overlay is not None:
            source_hash = source_hash_with_overlay(source_hash, overlay)
        source_hash_by_lou[record["lou"]] = source_hash
    return source_hash_by_lou

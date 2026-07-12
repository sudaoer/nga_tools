from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal, TypedDict

from bs4 import BeautifulSoup

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.backup.models import PostRecord
from nga_tools.bbcode_render import ImageSrcResolver, render_web_bbcode
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.html_sanitize import sanitize_post_html

POST_OVERLAYS_VERSION = 2
POST_OVERLAY_MODE_REPLACE: Literal["replace"] = "replace"

_BANNED_BBCODE_TAG_RE = re.compile(
    r"\[/?flash(?:\s|=|\])",
    re.IGNORECASE,
)
_IMG_BBCODE_RE = re.compile(
    r"\[img\](.*?)\[/img\]",
    re.IGNORECASE | re.DOTALL,
)
_ANY_IMG_BBCODE_TAG_RE = re.compile(
    r"\[/?img(?:\s|=|\])",
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
    if not isinstance(bbcode, str):
        raise ValueError("overlay BBCode必须是字符串。")
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
    if _BANNED_BBCODE_TAG_RE.search(bbcode):
        raise ValueError("overlay暂不支持[flash]媒体。")
    if _ANY_IMG_BBCODE_TAG_RE.search(_IMG_BBCODE_RE.sub("", bbcode)):
        raise ValueError("overlay图片只支持[img]NGA图片URL[/img]写法。")
    if BeautifulSoup(bbcode, "html.parser").find("img") is not None:
        raise ValueError("overlay图片只支持[img]NGA图片URL[/img]写法。")


def overlay_image_sources(bbcode: str) -> tuple[str, ...]:
    return tuple(match.group(1).strip() for match in _IMG_BBCODE_RE.finditer(bbcode))


def make_existing_overlay_image_src_resolver(
    bbcode: str,
    output_dir: Path,
    *,
    image_src_from_path: Callable[[str, Path], str | None] | None = None,
    require_all: bool = False,
) -> ImageSrcResolver:
    raw_sources = overlay_image_sources(bbcode)
    normalized_by_source: dict[str, str] = {}
    for raw_source in raw_sources:
        normalized_source = image_store.normalize_nga_image_url(raw_source)
        if not utils.NGA_img_link_verify(normalized_source):
            if require_all:
                raise ValueError(
                    "overlay图片链接必须是完整的NGA图片URL："
                    f"{raw_source or '<空链接>'}"
                )
            continue
        normalized_by_source[raw_source] = normalized_source

    paths_by_url = image_store.existing_image_paths_for_urls(
        output_dir,
        normalized_by_source.values(),
    )
    resolved_src_by_url: dict[str, str] = {}
    for normalized_source, image_path in paths_by_url.items():
        resolved_src = (
            normalized_source
            if image_src_from_path is None
            else image_src_from_path(normalized_source, image_path)
        )
        if resolved_src is not None:
            resolved_src_by_url[normalized_source] = resolved_src
    if require_all:
        for raw_source, normalized_source in normalized_by_source.items():
            if normalized_source not in resolved_src_by_url:
                raise ValueError(
                    "overlay图片尚未下载或本地文件无效："
                    f"{raw_source}"
                )

    def resolve_image_src(image_src: str) -> str | None:
        raw_source = image_src.strip()
        normalized_source = image_store.normalize_nga_image_url(raw_source)
        if not utils.NGA_img_link_verify(normalized_source):
            return None
        return resolved_src_by_url.get(normalized_source)

    return resolve_image_src


def _unresolved_image_src(_image_src: str) -> str | None:
    return None


def render_overlay_html(
    bbcode: str,
    *,
    image_src_resolver: ImageSrcResolver | None = None,
) -> str:
    validate_overlay_bbcode(bbcode)
    return sanitize_post_html(
        render_web_bbcode(
            bbcode,
            image_src_resolver=(
                _unresolved_image_src
                if image_src_resolver is None
                else image_src_resolver
            ),
        )
    )


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
    *,
    output_dir: Path | None = None,
) -> list[PostRecord]:
    if not overlays:
        return list(records)

    applied_records: list[PostRecord] = []
    for record in records:
        overlay = overlays.get(record["lou"])
        if overlay is None:
            applied_records.append(record)
            continue
        image_src_resolver = (
            None
            if output_dir is None
            else make_existing_overlay_image_src_resolver(
                overlay["bbcode"],
                output_dir,
            )
        )
        applied_records.append(
            {
                "lou": record["lou"],
                "pid": record["pid"],
                "post": None,
                "html": render_overlay_html(
                    overlay["bbcode"],
                    image_src_resolver=image_src_resolver,
                ),
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

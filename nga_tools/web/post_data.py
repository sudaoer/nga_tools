from __future__ import annotations

import math
import re
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import quote

from bs4 import BeautifulSoup, Tag

from nga_tools.backup import audio_store, image_index
from nga_tools.backup.audio_store import AudioMapping
from nga_tools.backup.archive_posts import postdate_from_json
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.backup.archive_store_models import ArchivePostVersionRow
from nga_tools.backup.content_codec import decode_content
from nga_tools.backup.floor_map import load_floor_labels_from_archive
from nga_tools.backup.floor_models import (
    MISSING_POST_HTML,
    ORIGINAL_POSTS_PER_PAGE,
    FloorLabels,
)
from nga_tools.backup.html_images import valid_nga_image_src
from nga_tools.backup.post_overlay import (
    PostOverlay,
    make_existing_overlay_image_src_resolver,
    make_post_overlay,
    render_overlay_html,
)
from nga_tools.bbcode_render import ImageSrcResolver, render_web_bbcode
from nga_tools.core.nga_audio import extract_nga_audio_urls, normalize_nga_audio_url
from nga_tools.html_sanitize import sanitize_post_html
from nga_tools.web.thread_data import (
    PostDate,
    ThreadNotFoundError,
    ThreadUnavailableError,
    parse_aid_key,
)
from nga_tools.web.sqlite_access import connect_readonly

PostEmptyReason = Literal["missing", "filtered"]
_IMG_BBCODE_RE = re.compile(r"\[img\](.*?)\[/img\]", re.IGNORECASE | re.DOTALL)


class PostSlot(TypedDict):
    lou: int
    pid: Optional[int]
    versionId: Optional[int]
    manualVersion: bool
    authorName: Optional[str]
    authorUid: Optional[int]
    postdate: Optional[PostDate]
    floorLabel: str
    html: str
    emptyReason: Optional[PostEmptyReason]
    hasOverlay: bool



class PostsResult(TypedDict):
    slots: list[PostSlot]
    items: list[PostSlot]
    total: int
    offset: int
    limit: int
    page: int
    pageSize: int
    pageStartLou: int
    pageEndLou: int
    totalPages: int
    postCount: int
    matchingPostCount: int
    maxLou: Optional[int]



class PostVersionOption(TypedDict):
    id: int
    sourceHash: str
    firstSeenAt: str
    lastSeenAt: str
    seenCount: int
    isLatest: bool
    isSelected: bool
    selectable: bool
    content: str
    contentPreview: str



class PostVersionGroup(TypedDict):
    lou: int
    floorLabel: str
    latestVersionId: int
    selectedVersionId: Optional[int]
    activeVersionId: int
    versions: list[PostVersionOption]



class PostVersionGroupsResult(TypedDict):
    items: list[PostVersionGroup]



class PostVersionPreview(TypedDict):
    item: PostSlot



class PostVersionSelectionResult(TypedDict):
    lou: int
    selectedVersionId: Optional[int]
    activeVersionId: int



class PostOverlayDetail(TypedDict):
    lou: int
    floorLabel: str
    hasOverlay: bool
    bbcode: str
    html: Optional[str]



class PostOverlayPreview(TypedDict):
    html: str



def _load_floor_labels(
    archive_store: ThreadArchiveStore,
    aid: Optional[int],
) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()

    try:
        return load_floor_labels_from_archive(archive_store, aid)
    except (RuntimeError, ValueError):
        return FloorLabels.plain()



def _safe_output_url(path: Path, output_dir: Path) -> Optional[str]:
    output_root = output_dir.resolve()
    resolved_path = path.resolve()
    if not resolved_path.is_relative_to(output_root):
        return None
    if not resolved_path.is_file():
        return None
    relative_path = resolved_path.relative_to(output_root)
    return "/api/files/" + quote(relative_path.as_posix(), safe="/")



def _read_image_mappings_for_urls(
    output_dir: Path,
    urls: set[str],
) -> dict[str, str]:
    try:
        mappings = image_index.ImageIndexStore(output_dir).mappings_for_urls(urls)
    except sqlite3.Error:
        return {}
    return {
        url: mapping.unique_rel_path for url, mapping in mappings.items()
    }



def _output_image_url(
    output_dir: Path,
    image_mappings: dict[str, str],
    image_url: str,
) -> Optional[str]:
    normalized_url = image_index.normalize_nga_image_url(image_url)
    unique_rel_path = image_mappings.get(normalized_url)
    if unique_rel_path is None:
        return None
    return _safe_output_url(output_dir / unique_rel_path, output_dir)



def _read_audio_mappings_for_urls(
    output_dir: Path,
    urls: set[str],
) -> dict[str, AudioMapping]:
    return audio_store.audio_mappings_for_urls(output_dir, urls)



def _output_audio_url(
    output_dir: Path,
    audio_mappings: dict[str, AudioMapping],
    audio_url: str,
) -> Optional[str]:
    normalized_url = normalize_nga_audio_url(audio_url)
    if normalized_url is None:
        return None
    mapping = audio_mappings.get(normalized_url)
    if mapping is None:
        return None
    return _safe_output_url(mapping.path(output_dir), output_dir)



def _normalize_audio_elements(
    html: str,
    output_dir: Path,
    audio_mappings: dict[str, AudioMapping],
) -> str:
    if "<audio" not in html.lower():
        return html
    soup = BeautifulSoup(html, "html.parser")
    for raw_audio in cast(list[Tag], soup.find_all("audio")):
        raw_src = raw_audio.get("src")
        local_src = (
            _output_audio_url(output_dir, audio_mappings, raw_src)
            if isinstance(raw_src, str)
            else None
        )
        if local_src is None:
            unavailable = soup.new_tag("span")
            unavailable["class"] = "nga-audio-unavailable"
            unavailable.string = "音频未下载或不可用"
            raw_audio.replace_with(unavailable)
            continue

        player = soup.new_tag("audio")
        player["class"] = "nga-audio-player"
        player["controls"] = ""
        player["preload"] = "none"
        player["src"] = local_src
        player["aria-label"] = "BGM音频"
        raw_audio.replace_with(player)
    return str(soup)



def _render_overlay_for_web(
    bbcode: str,
    output_dir: Path,
    *,
    require_all_images: bool = False,
) -> str:
    image_src_resolver = make_existing_overlay_image_src_resolver(
        bbcode,
        output_dir,
        image_src_from_path=lambda _url, image_path: _safe_output_url(
            image_path,
            output_dir,
        ),
        require_all=require_all_images,
    )
    html = render_overlay_html(
        bbcode,
        image_src_resolver=image_src_resolver,
    )
    audio_urls = set(extract_nga_audio_urls(bbcode))
    return _normalize_audio_elements(
        html,
        output_dir,
        _read_audio_mappings_for_urls(output_dir, audio_urls),
    )



def _content_image_urls(content: str) -> set[str]:
    urls: set[str] = set()
    for match in _IMG_BBCODE_RE.finditer(content):
        url = valid_nga_image_src(match.group(1))
        if url is not None:
            urls.add(url)
    return urls



def _image_src_resolver(
    output_dir: Path,
    image_mappings: dict[str, str],
) -> ImageSrcResolver:
    def resolve_image_src(image_src: str) -> str | None:
        normalized_src = valid_nga_image_src(image_src)
        if normalized_src is None:
            return None

        output_url = _output_image_url(output_dir, image_mappings, normalized_src)
        return normalized_src if output_url is None else output_url

    return resolve_image_src



def _tag_classes(tag: Tag) -> set[str]:
    raw_classes = tag.get("class")
    if not isinstance(raw_classes, list):
        return set()
    return set(raw_classes)



def _find_first_by_class(tag: Tag, class_name: str) -> Tag | None:
    for child in tag.find_all(True, recursive=False):
        if class_name in _tag_classes(child):
            return child
    return None



def _fold_summary_text(collapse_button: Tag) -> str:
    text = collapse_button.get_text(" ", strip=True)
    if text.startswith("+"):
        text = text[1:].strip()
    return text.removesuffix("...").strip() or "折叠内容"



def _normalize_nga_fold_boxes(html: str) -> str:
    if "foldBox" not in html and "collapse_content" not in html:
        return html

    soup = BeautifulSoup(html, "html.parser")
    for fold_box in cast(list[Tag], soup.find_all("div")):
        if "foldBox" not in _tag_classes(fold_box):
            continue
        collapse_button = _find_first_by_class(fold_box, "collapse_btn")
        collapse_content = _find_first_by_class(fold_box, "collapse_content")
        if collapse_button is None or collapse_content is None:
            continue

        details = soup.new_tag("details")
        details["class"] = "nga-collapse"
        summary = soup.new_tag("summary")
        summary.string = _fold_summary_text(collapse_button)
        content = soup.new_tag("div")
        content["class"] = "nga-collapse-content"
        for child in list(collapse_content.contents):
            content.append(child.extract())
        details.append(summary)
        details.append(content)
        fold_box.replace_with(details)

    return str(soup)



def _post_item_from_row(
    row: ArchivePostVersionRow,
    output_dir: Path,
    floor_labels: FloorLabels,
    image_mappings: dict[str, str],
    audio_mappings: dict[str, AudioMapping],
    overlay: Optional[PostOverlay] = None,
) -> PostSlot:
    if overlay is None:
        html = render_web_bbcode(
            row.content,
            image_src_resolver=_image_src_resolver(
                output_dir,
                image_mappings,
            ),
        )
        html = _normalize_audio_elements(
            html,
            output_dir,
            audio_mappings,
        )
    else:
        html = _render_overlay_for_web(overlay["bbcode"], output_dir)
    html = _normalize_nga_fold_boxes(html)

    return {
        "lou": row.lou,
        "pid": row.pid,
        "versionId": row.version_id,
        "manualVersion": row.manual_selection,
        "authorName": row.author_name,
        "authorUid": row.author_uid,
        "postdate": postdate_from_json(row.postdate_json),
        "floorLabel": floor_labels.label(row.lou),
        "html": sanitize_post_html(html),
        "emptyReason": None,
        "hasOverlay": overlay is not None,
    }



def _empty_post_slot(
    lou: int,
    floor_labels: FloorLabels,
    reason: PostEmptyReason,
) -> PostSlot:
    message = MISSING_POST_HTML
    if reason == "filtered":
        message = "<p><em>此楼层不匹配当前筛选。</em></p>"

    return {
        "lou": lou,
        "pid": None,
        "versionId": None,
        "manualVersion": False,
        "authorName": None,
        "authorUid": None,
        "postdate": None,
        "floorLabel": floor_labels.label(lou),
        "html": message,
        "emptyReason": reason,
        "hasOverlay": False,
    }



def _post_row_matches(
    row: ArchivePostVersionRow,
    query: str,
    lou_from: Optional[int],
    lou_to: Optional[int],
    overlays: dict[int, PostOverlay],
) -> bool:
    content = overlays[row.lou]["bbcode"] if row.lou in overlays else row.content
    if query and query not in content:
        return False
    if lou_from is not None and row.lou < lou_from:
        return False
    if lou_to is not None and row.lou > lou_to:
        return False
    return True



def read_posts(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    *,
    page: int,
    query: str = "",
    lou_from: Optional[int] = None,
    lou_to: Optional[int] = None,
) -> PostsResult:
    if page < 1:
        raise ValueError("page必须大于等于1。")

    aid = parse_aid_key(raw_aid_key)
    thread_folder = output_dir / f"{tid}_{raw_aid_key}"
    if not thread_folder.is_dir():
        raise ThreadNotFoundError("未找到对应备份目录。")
    db_path = thread_folder / ARCHIVE_DB_FILENAME
    if not db_path.is_file():
        raise ThreadUnavailableError("缺少archive.sqlite3。")
    archive_store = ThreadArchiveStore(thread_folder)
    overlays = archive_store.overlays.read_post_overlays()

    page_size = ORIGINAL_POSTS_PER_PAGE
    stripped_query = query.strip()
    has_filter = bool(stripped_query) or lou_from is not None or lou_to is not None
    slot_end_lou = 0
    if has_filter:
        effective_rows = archive_store.posts.read_effective_post_rows()
        post_count = len(effective_rows)
        max_lou = max((row.lou for row in effective_rows), default=None)
        matching_rows = [
            row
            for row in effective_rows
            if _post_row_matches(row, stripped_query, lou_from, lou_to, overlays)
        ]
        matching_post_count = len(matching_rows)
        total = matching_post_count
        total_pages = max(1, math.ceil(total / page_size))
        resolved_page = min(page, total_pages)
        result_offset = (resolved_page - 1) * page_size
        rows = matching_rows[result_offset : result_offset + page_size]
        page_start_lou = rows[0].lou if rows else 0
        page_end_lou = rows[-1].lou if rows else 0
    else:
        stats = archive_store.posts.read_effective_post_stats()
        post_count = stats.post_count
        max_lou = stats.max_lou
        total = max_lou + 1 if max_lou is not None and max_lou >= 0 else 0
        total_pages = max(1, math.ceil(total / page_size))
        resolved_page = min(page, total_pages)
        page_start_lou = (resolved_page - 1) * page_size
        page_end_lou = page_start_lou + page_size - 1
        slot_end_lou = (
            min(page_end_lou, max_lou)
            if max_lou is not None and max_lou >= page_start_lou
            else page_start_lou - 1
        )
        page_lous: set[int] = (
            set(range(page_start_lou, slot_end_lou + 1))
            if slot_end_lou >= page_start_lou
            else set[int]()
        )
        rows = archive_store.posts.read_effective_post_rows(page_lous)
        matching_post_count = post_count
        result_offset = page_start_lou
        page_end_lou = max(page_start_lou, slot_end_lou)

    image_urls: set[str] = set()
    for row in rows:
        if row.lou in overlays:
            continue
        image_urls.update(_content_image_urls(row.content))
    image_mappings = _read_image_mappings_for_urls(output_dir, image_urls)
    audio_urls: set[str] = set()
    for row in rows:
        if row.lou in overlays:
            continue
        audio_urls.update(extract_nga_audio_urls(row.content))
    audio_mappings = _read_audio_mappings_for_urls(output_dir, audio_urls)

    floor_labels = _load_floor_labels(archive_store, aid)
    slots: list[PostSlot]
    if has_filter:
        slots = [
            _post_item_from_row(
                row,
                output_dir,
                floor_labels,
                image_mappings,
                audio_mappings,
                overlays.get(row.lou),
            )
            for row in rows
        ]
    else:
        row_by_lou = {row.lou: row for row in rows}
        slots = []
        for lou in range(page_start_lou, slot_end_lou + 1):
            row = row_by_lou.get(lou)
            if row is None:
                slots.append(_empty_post_slot(lou, floor_labels, "missing"))
                continue
            slots.append(
                _post_item_from_row(
                    row,
                    output_dir,
                    floor_labels,
                    image_mappings,
                    audio_mappings,
                    overlays.get(lou),
                )
            )

    items = [slot for slot in slots if slot["emptyReason"] is None]

    return {
        "slots": slots,
        "items": items,
        "total": total,
        "offset": result_offset if has_filter else page_start_lou,
        "limit": page_size,
        "page": resolved_page,
        "pageSize": page_size,
        "pageStartLou": page_start_lou,
        "pageEndLou": page_end_lou,
        "totalPages": total_pages,
        "postCount": post_count,
        "matchingPostCount": matching_post_count,
        "maxLou": max_lou,
    }



def _archive_store_for_thread(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
) -> tuple[ThreadArchiveStore, Path, Optional[int]]:
    aid = parse_aid_key(raw_aid_key)
    thread_folder = output_dir / f"{tid}_{raw_aid_key}"
    if not thread_folder.is_dir():
        raise ThreadNotFoundError("未找到对应备份目录。")
    db_path = thread_folder / ARCHIVE_DB_FILENAME
    if not db_path.is_file():
        raise ThreadUnavailableError("缺少archive.sqlite3。")
    archive_store = ThreadArchiveStore(thread_folder)
    return archive_store, thread_folder, aid



def _read_effective_row_for_lou(
    archive_store: ThreadArchiveStore,
    lou: int,
) -> ArchivePostVersionRow:
    rows = archive_store.posts.read_effective_post_rows({lou})
    if not rows:
        raise ValueError("未知楼层。")
    return rows[0]



def read_post_overlay(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    archive_store, _thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    row = _read_effective_row_for_lou(archive_store, lou)
    floor_labels = _load_floor_labels(archive_store, aid)
    overlay = archive_store.overlays.read_post_overlays({lou}).get(lou)
    if overlay is None:
        return {
            "lou": lou,
            "floorLabel": floor_labels.label(lou),
            "hasOverlay": False,
            "bbcode": row.content,
            "html": None,
        }

    return {
        "lou": lou,
        "floorLabel": floor_labels.label(lou),
        "hasOverlay": True,
        "bbcode": overlay["bbcode"],
        "html": _render_overlay_for_web(overlay["bbcode"], output_dir),
    }



def preview_post_overlay(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
    bbcode: str,
) -> PostOverlayPreview:
    archive_store, _thread_folder, _aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    _read_effective_row_for_lou(archive_store, lou)
    return {
        "html": _render_overlay_for_web(
            bbcode,
            output_dir,
            require_all_images=True,
        )
    }



def save_thread_post_overlay(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
    bbcode: str,
) -> PostOverlayDetail:
    archive_store, _thread_folder, _aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    _read_effective_row_for_lou(archive_store, lou)
    _render_overlay_for_web(
        bbcode,
        output_dir,
        require_all_images=True,
    )
    archive_store.overlays.upsert_post_overlay(lou, make_post_overlay(bbcode))
    return read_post_overlay(output_dir, tid, raw_aid_key, lou)



def clear_thread_post_overlay(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    archive_store, _thread_folder, _aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    _read_effective_row_for_lou(archive_store, lou)
    archive_store.overlays.delete_post_overlay(lou)
    return read_post_overlay(output_dir, tid, raw_aid_key, lou)



def _content_preview(content: str, max_length: int = 120) -> str:
    preview = " ".join(content.split())
    if len(preview) <= max_length:
        return preview
    return preview[: max_length - 1] + "…"



def read_post_version_groups(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
) -> PostVersionGroupsResult:
    archive_store, _thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    selected_by_lou = archive_store.posts.read_valid_post_version_selections()
    floor_labels = _load_floor_labels(archive_store, aid)
    with closing(connect_readonly(archive_store.db_path)) as connection:
        rows = cast(
            list[tuple[int, int, str, object, str, str, int, int]],
            connection.execute(
                """
                WITH ranked AS (
                    SELECT
                        id,
                        lou,
                        source_hash,
                        content,
                        first_seen_at,
                        last_seen_at,
                        seen_count,
                        ROW_NUMBER() OVER (
                            PARTITION BY lou
                            ORDER BY last_seen_at DESC, id DESC
                        ) AS row_number,
                        COUNT(*) OVER (PARTITION BY lou) AS version_count
                    FROM post_versions
                )
                SELECT
                    id,
                    lou,
                    source_hash,
                    content,
                    first_seen_at,
                    last_seen_at,
                    seen_count,
                    row_number
                FROM ranked
                WHERE version_count > 1
                ORDER BY lou, row_number
                """
            ).fetchall(),
        )

    options_by_lou: dict[int, list[PostVersionOption]] = {}
    latest_version_by_lou: dict[int, int] = {}
    for (
        version_id,
        lou,
        source_hash,
        raw_content,
        first_seen_at,
        last_seen_at,
        seen_count,
        row_number,
    ) in rows:
        content = decode_content(
            raw_content,
            source=f"archive帖子版本{version_id}正文",
        )
        is_latest = row_number == 1
        if is_latest:
            latest_version_by_lou[lou] = version_id
        selected_selection = selected_by_lou.get(lou)
        selected_version_id = (
            None if selected_selection is None else selected_selection["version_id"]
        )
        option: PostVersionOption = {
            "id": version_id,
            "sourceHash": source_hash,
            "firstSeenAt": first_seen_at,
            "lastSeenAt": last_seen_at,
            "seenCount": seen_count,
            "isLatest": is_latest,
            "isSelected": selected_version_id == version_id,
            "selectable": not is_latest,
            "content": content,
            "contentPreview": _content_preview(content),
        }
        options_by_lou.setdefault(lou, []).append(option)

    groups: list[PostVersionGroup] = []
    for lou in sorted(options_by_lou):
        latest_version_id = latest_version_by_lou[lou]
        selected_version = selected_by_lou.get(lou)
        selected_version_id = (
            None if selected_version is None else selected_version["version_id"]
        )
        groups.append(
            {
                "lou": lou,
                "floorLabel": floor_labels.label(lou),
                "latestVersionId": latest_version_id,
                "selectedVersionId": selected_version_id,
                "activeVersionId": selected_version_id or latest_version_id,
                "versions": options_by_lou[lou],
            }
        )

    return {"items": groups}



def read_post_version_preview(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    version_id: int,
) -> PostVersionPreview:
    archive_store, _thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    row = archive_store.posts.read_post_row_for_version(version_id)
    if row is None:
        raise ValueError("未知帖子正文版本。")

    image_urls = _content_image_urls(row.content)
    image_mappings = _read_image_mappings_for_urls(output_dir, image_urls)
    audio_mappings = _read_audio_mappings_for_urls(
        output_dir,
        set(extract_nga_audio_urls(row.content)),
    )
    return {
        "item": _post_item_from_row(
            row,
            output_dir,
            _load_floor_labels(archive_store, aid),
            image_mappings,
            audio_mappings,
        )
    }



def select_post_version(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
    version_id: int,
) -> PostVersionSelectionResult:
    archive_store, _thread_folder, _aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    archive_store.posts.upsert_post_version_selection(lou, version_id)
    return {
        "lou": lou,
        "selectedVersionId": version_id,
        "activeVersionId": version_id,
    }



def clear_post_version_selection(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
) -> PostVersionSelectionResult:
    archive_store, _thread_folder, _aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    latest_version_id = archive_store.posts.delete_post_version_selection(lou)
    return {
        "lou": lou,
        "selectedVersionId": None,
        "activeVersionId": latest_version_id,
    }

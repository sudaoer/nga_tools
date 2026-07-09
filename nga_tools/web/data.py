from __future__ import annotations

import datetime
import json
import math
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.backup.floor_models import (
    MISSING_POST_HTML,
    ORIGINAL_POSTS_PER_PAGE,
    FloorLabels,
)
from nga_tools.backup.post_data import (
    attachment_url_from_value,
    make_image_src_resolver,
    post_data_from_raw,
)
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.web.html_sanitize import sanitize_post_html
from nga_tools.web.render import ImageSrcResolver, render_web_bbcode

ThreadStatus = Literal["ready", "needs_migration", "missing_html", "invalid"]
PostEmptyReason = Literal["missing", "filtered"]
PostDate = int | str

_THREAD_DIR_RE = re.compile(r"^(\d+)_(all|\d+)$")
_POST_HTML_RE = re.compile(r"^post_(\d+)\.html$")
_IMG_BBCODE_RE = re.compile(r"\[img\](.*?)\[/img\]", re.IGNORECASE | re.DOTALL)


class ThreadSummary(TypedDict):
    tid: int
    aid: Optional[int]
    aidKey: str
    dirName: str
    status: ThreadStatus
    message: Optional[str]
    threadName: Optional[str]
    subject: Optional[str]
    author: Optional[str]
    link: Optional[str]
    replies: Optional[int]
    postdate: Optional[int]
    lastpost: Optional[int]
    postCount: int
    minLou: Optional[int]
    maxLou: Optional[int]
    pageCount: int
    updatedAt: Optional[str]
    authorUpdatedAt: Optional[PostDate]
    hasHtmlModified: bool
    hasFloorMap: bool
    hasWarnings: bool


class PostSlot(TypedDict):
    lou: int
    pid: Optional[int]
    authorName: Optional[str]
    authorUid: Optional[int]
    postdate: Optional[PostDate]
    floorLabel: str
    html: str
    emptyReason: Optional[PostEmptyReason]


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


@dataclass(frozen=True)
class ArchiveStats:
    post_count: int
    min_lou: Optional[int]
    max_lou: Optional[int]
    page_count: int
    author_updated_at: Optional[PostDate]


class ThreadNotFoundError(Exception):
    pass


class ThreadUnavailableError(Exception):
    pass


def aid_key(aid: Optional[int]) -> str:
    return "all" if aid is None else str(aid)


def parse_aid_key(value: str) -> Optional[int]:
    if value == "all":
        return None
    try:
        aid = int(value)
    except ValueError as error:
        raise ValueError("aid_key必须是all或整数。") from error
    if aid < 0:
        raise ValueError("aid_key不能为负数。")
    return aid


def parse_thread_dir_name(name: str) -> Optional[tuple[int, Optional[int], str]]:
    match = _THREAD_DIR_RE.fullmatch(name)
    if match is None:
        return None
    tid = int(match.group(1))
    raw_aid_key = match.group(2)
    return tid, parse_aid_key(raw_aid_key), raw_aid_key


def load_thread_metadata() -> dict[tuple[int, str], ThreadConfig]:
    metadata: dict[tuple[int, str], ThreadConfig] = {}
    for item in NGAThreadConfigs().get_thread_configs():
        metadata[(thread_config_tid(item), aid_key(thread_config_aid(item)))] = item
    return metadata


def _optional_str_metadata(
    metadata: Optional[ThreadConfig],
    key: str,
) -> Optional[str]:
    if metadata is None:
        return None
    value = metadata.get(key)
    return value if isinstance(value, str) and value else None


def _optional_int_metadata(
    metadata: Optional[ThreadConfig],
    key: str,
) -> Optional[int]:
    if metadata is None:
        return None
    value = metadata.get(key)
    if type(value) is int:
        return value
    return None


def _metadata_name(metadata: Optional[ThreadConfig]) -> Optional[str]:
    if metadata is None:
        return None
    try:
        return thread_config_name(metadata)
    except ValueError:
        return None


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    resolved_path = db_path.resolve()
    uri = f"file:{quote(str(resolved_path), safe='/:')}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _postdate_from_json_text(post_json: str) -> Optional[PostDate]:
    try:
        raw_post: object = json.loads(post_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_post, dict):
        return None
    post = cast(dict[str, object], raw_post)
    value = post.get("postdate")
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped_value = value.strip()
        return stripped_value if stripped_value else None
    return None


def _read_archive_stats(db_path: Path) -> ArchiveStats:
    latest_cte = """
        WITH latest AS (
            SELECT
                lou,
                post_json,
                ROW_NUMBER() OVER (
                    PARTITION BY lou
                    ORDER BY last_seen_at DESC, id DESC
                ) AS row_number
            FROM post_versions
        )
    """
    with closing(_connect_readonly(db_path)) as connection:
        post_row = cast(
            tuple[int, Optional[int], Optional[int]],
            connection.execute(
                """
                WITH latest AS (
                    SELECT
                        lou,
                        ROW_NUMBER() OVER (
                            PARTITION BY lou
                            ORDER BY last_seen_at DESC, id DESC
                        ) AS row_number
                    FROM post_versions
                )
                SELECT COUNT(*), MIN(lou), MAX(lou)
                FROM latest
                WHERE row_number = 1
                """
            ).fetchone(),
        )
        latest_post_row = cast(
            Optional[tuple[str]],
            connection.execute(
                f"""
                {latest_cte}
                SELECT post_json
                FROM latest
                WHERE row_number = 1
                ORDER BY lou DESC
                LIMIT 1
                """
            ).fetchone(),
        )
        page_row = cast(
            tuple[int],
            connection.execute(
                "SELECT COUNT(DISTINCT page_number) FROM page_snapshots"
            ).fetchone(),
        )

    author_updated_at: Optional[PostDate] = None
    if latest_post_row is not None:
        author_updated_at = _postdate_from_json_text(latest_post_row[0])

    return ArchiveStats(
        post_count=post_row[0],
        min_lou=post_row[1],
        max_lou=post_row[2],
        page_count=page_row[0],
        author_updated_at=author_updated_at,
    )


def _has_post_html_files(html_modified_dir: Path) -> bool:
    if not html_modified_dir.is_dir():
        return False
    return any(
        path.is_file() and _POST_HTML_RE.fullmatch(path.name) is not None
        for path in html_modified_dir.iterdir()
    )


def _latest_mtime(paths: list[Path]) -> Optional[str]:
    timestamps = [path.stat().st_mtime for path in paths if path.exists()]
    if not timestamps:
        return None
    return datetime.datetime.fromtimestamp(
        max(timestamps),
        datetime.timezone.utc,
    ).isoformat()


def _link_with_authorid(link: str, tid: int, aid: int) -> str:
    parts = urlsplit(link)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "authorid"
    ]
    if not any(key == "tid" for key, _value in query_items):
        query_items.insert(0, ("tid", str(tid)))
    query_items.append(("authorid", str(aid)))
    path = parts.path or "/read.php"
    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            urlencode(query_items),
            parts.fragment,
        )
    )


def _thread_link(
    metadata: Optional[ThreadConfig],
    *,
    tid: int,
    aid: Optional[int],
) -> str:
    link = _optional_str_metadata(metadata, "link")
    if link is None:
        try:
            base_url = utils.get_config().base_url
        except (FileNotFoundError, ValueError):
            base_url = "https://bbs.nga.cn"
        link = f"{base_url.rstrip('/')}/read.php?tid={tid}"
    if aid is None:
        return link
    return _link_with_authorid(link, tid, aid)


def _thread_summary_for_folder(
    thread_folder: Path,
    metadata: Optional[ThreadConfig],
) -> Optional[ThreadSummary]:
    parsed = parse_thread_dir_name(thread_folder.name)
    if parsed is None:
        return None

    tid, aid, raw_aid_key = parsed
    db_path = thread_folder / ARCHIVE_DB_FILENAME
    html_modified_dir = thread_folder / "html_modified"
    floor_map_path = thread_folder / "floor_map.json"
    warnings_path = thread_folder / "warnings.log"
    has_html_modified = _has_post_html_files(html_modified_dir)
    has_floor_map = floor_map_path.is_file()
    has_warnings = warnings_path.is_file()
    status: ThreadStatus = "invalid"
    message: Optional[str] = None
    stats = ArchiveStats(
        post_count=0,
        min_lou=None,
        max_lou=None,
        page_count=0,
        author_updated_at=None,
    )

    if db_path.is_file():
        try:
            stats = _read_archive_stats(db_path)
            status = "ready"
            if not has_html_modified:
                message = "未找到html_modified/post_*.html，Web将从原始备份数据渲染。"
        except sqlite3.Error as error:
            status = "invalid"
            message = f"archive.sqlite3无法读取：{error}"
    elif (thread_folder / "json").is_dir():
        status = "needs_migration"
        message = "此目录只有旧分页JSON，请先运行 backup migrate-store。"
    else:
        status = "invalid"
        message = "未找到archive.sqlite3。"

    updated_at = _latest_mtime(
        [
            db_path,
            html_modified_dir / "html_modified_manifest.json",
            floor_map_path,
            warnings_path,
        ]
    )

    return {
        "tid": tid,
        "aid": aid,
        "aidKey": raw_aid_key,
        "dirName": thread_folder.name,
        "status": status,
        "message": message,
        "threadName": _metadata_name(metadata),
        "subject": _optional_str_metadata(metadata, "subject"),
        "author": _optional_str_metadata(metadata, "author"),
        "link": _thread_link(metadata, tid=tid, aid=aid),
        "replies": _optional_int_metadata(metadata, "replies"),
        "postdate": _optional_int_metadata(metadata, "postdate"),
        "lastpost": _optional_int_metadata(metadata, "lastpost"),
        "postCount": stats.post_count,
        "minLou": stats.min_lou,
        "maxLou": stats.max_lou,
        "pageCount": stats.page_count,
        "updatedAt": updated_at,
        "authorUpdatedAt": stats.author_updated_at,
        "hasHtmlModified": has_html_modified,
        "hasFloorMap": has_floor_map,
        "hasWarnings": has_warnings,
    }


def scan_thread_summaries(
    output_dir: Path,
    metadata_by_key: dict[tuple[int, str], ThreadConfig],
) -> list[ThreadSummary]:
    if not output_dir.is_dir():
        return []

    summaries: list[ThreadSummary] = []
    for thread_folder in sorted(output_dir.iterdir(), key=lambda path: path.name):
        if not thread_folder.is_dir():
            continue
        parsed = parse_thread_dir_name(thread_folder.name)
        if parsed is None:
            continue
        tid, _aid, raw_aid_key = parsed
        summary = _thread_summary_for_folder(
            thread_folder,
            metadata_by_key.get((tid, raw_aid_key)),
        )
        if summary is not None:
            summaries.append(summary)

    return sorted(
        summaries,
        key=lambda item: (item["updatedAt"] or "", item["dirName"]),
        reverse=True,
    )


def read_thread_summary(
    output_dir: Path,
    metadata_by_key: dict[tuple[int, str], ThreadConfig],
    tid: int,
    raw_aid_key: str,
) -> ThreadSummary:
    parse_aid_key(raw_aid_key)
    thread_folder = output_dir / f"{tid}_{raw_aid_key}"
    if not thread_folder.is_dir():
        raise ThreadNotFoundError("未找到对应备份目录。")
    summary = _thread_summary_for_folder(
        thread_folder,
        metadata_by_key.get((tid, raw_aid_key)),
    )
    if summary is None:
        raise ThreadNotFoundError("未找到对应备份目录。")
    return summary


def _load_floor_labels(thread_folder: Path, aid: Optional[int]) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()

    path = thread_folder / "floor_map.json"
    if not path.is_file():
        return FloorLabels.plain()

    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return FloorLabels.plain()
    if not isinstance(raw_data, dict):
        return FloorLabels.plain()
    data = cast(dict[str, object], raw_data)
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        return FloorLabels.plain()

    original_lou_by_author_lou: dict[int, int] = {}
    candidate_original_lous_by_author_lou: dict[int, list[int]] = {}
    for raw_entry in cast(list[object], raw_entries):
        if not isinstance(raw_entry, dict):
            continue
        entry = cast(dict[str, object], raw_entry)
        author_lou = entry.get("author_lou")
        if type(author_lou) is not int:
            continue
        original_lou = entry.get("original_lou")
        if type(original_lou) is int:
            original_lou_by_author_lou[author_lou] = original_lou
            continue
        raw_candidates = entry.get("candidate_original_lous")
        if isinstance(raw_candidates, list):
            candidates = [
                item for item in cast(list[object], raw_candidates) if type(item) is int
            ]
            if candidates:
                candidate_original_lous_by_author_lou[author_lou] = candidates

    return FloorLabels(
        original_lou_by_author_lou=original_lou_by_author_lou,
        candidate_original_lous_by_author_lou=candidate_original_lous_by_author_lou,
        show_original=True,
    )


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
    normalized_urls = sorted(
        {
            normalized_url
            for url in urls
            if utils.NGA_img_link_verify(
                normalized_url := image_store.normalize_nga_image_url(url)
            )
        }
    )
    if not normalized_urls:
        return {}

    db_path = output_dir / image_store.IMAGE_INDEX_FILENAME
    if not db_path.is_file():
        return {}

    mappings: dict[str, str] = {}
    try:
        with closing(_connect_readonly(db_path)) as connection:
            for start in range(0, len(normalized_urls), 900):
                chunk = normalized_urls[start : start + 900]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT url, unique_rel_path
                    FROM image_mappings
                    WHERE url IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for url, unique_rel_path in rows:
                    if isinstance(url, str) and isinstance(unique_rel_path, str):
                        mappings[url] = unique_rel_path
    except sqlite3.Error:
        return {}

    return mappings


def _output_image_url(
    output_dir: Path,
    image_mappings: dict[str, str],
    image_url: str,
) -> Optional[str]:
    normalized_url = image_store.normalize_nga_image_url(image_url)
    unique_rel_path = image_mappings.get(normalized_url)
    if unique_rel_path is None:
        return None
    return _safe_output_url(output_dir / unique_rel_path, output_dir)


def _raw_post_from_json(post_json: str) -> dict[str, object]:
    raw_post: object = json.loads(post_json)
    if isinstance(raw_post, dict):
        return cast(dict[str, object], raw_post)
    return {}


def _post_content(raw_post: dict[str, object]) -> str:
    content = raw_post.get("content")
    return content if isinstance(content, str) else ""


def _attachment_urls(raw_post: dict[str, object]) -> set[str]:
    urls: set[str] = set()
    for match in _IMG_BBCODE_RE.finditer(_post_content(raw_post)):
        url = attachment_url_from_value(match.group(1))
        if url is not None:
            urls.add(url)

    try:
        post_data = post_data_from_raw(raw_post)
    except ValueError:
        return urls

    urls.update(attachment["url"] for attachment in post_data["image_attachments"])
    return urls


def _unresolved_image_src(_image_src: str) -> str | None:
    return None


def _image_src_resolver(
    raw_post: dict[str, object],
    output_dir: Path,
    image_mappings: dict[str, str],
) -> ImageSrcResolver:
    attachment_resolver: ImageSrcResolver
    try:
        post_data = post_data_from_raw(raw_post)
        attachment_resolver = make_image_src_resolver(post_data["image_attachments"])
    except ValueError:
        attachment_resolver = _unresolved_image_src

    def resolve_image_src(image_src: str) -> str | None:
        resolved_src = attachment_resolver(image_src)
        candidate_src = image_src.strip() if resolved_src is None else resolved_src
        normalized_src = image_store.normalize_nga_image_url(candidate_src)
        if not utils.NGA_img_link_verify(normalized_src):
            return resolved_src

        output_url = _output_image_url(output_dir, image_mappings, normalized_src)
        return normalized_src if output_url is None else output_url

    return resolve_image_src


def _optional_postdate_from_post(
    post: dict[str, object],
    key: str,
) -> Optional[PostDate]:
    value = post.get(key)
    if type(value) is int:
        return value
    if isinstance(value, str):
        stripped_value = value.strip()
        return stripped_value if stripped_value else None
    return None


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
    row: tuple[int, Optional[int], str, str],
    output_dir: Path,
    floor_labels: FloorLabels,
    image_mappings: dict[str, str],
) -> PostSlot:
    lou, pid, post_json, _content = row
    post = _raw_post_from_json(post_json)
    author_name, author_uid = _post_author(post)
    html = render_web_bbcode(
        _post_content(post),
        image_src_resolver=_image_src_resolver(post, output_dir, image_mappings),
    )
    html = _normalize_nga_fold_boxes(html)

    return {
        "lou": lou,
        "pid": pid,
        "authorName": author_name,
        "authorUid": author_uid,
        "postdate": _optional_postdate_from_post(post, "postdate"),
        "floorLabel": floor_labels.label(lou),
        "html": sanitize_post_html(html),
        "emptyReason": None,
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
        "authorName": None,
        "authorUid": None,
        "postdate": None,
        "floorLabel": floor_labels.label(lou),
        "html": message,
        "emptyReason": reason,
    }


def _latest_posts_where(
    query: str,
    lou_from: Optional[int],
    lou_to: Optional[int],
) -> tuple[str, list[object]]:
    conditions = ["row_number = 1"]
    params: list[object] = []
    if query:
        conditions.append("content LIKE ?")
        params.append(f"%{query}%")
    if lou_from is not None:
        conditions.append("lou >= ?")
        params.append(lou_from)
    if lou_to is not None:
        conditions.append("lou <= ?")
        params.append(lou_to)
    return " AND ".join(conditions), params


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

    page_size = ORIGINAL_POSTS_PER_PAGE
    where_sql, params = _latest_posts_where(query.strip(), lou_from, lou_to)
    latest_cte = """
        WITH latest AS (
            SELECT
                lou,
                pid,
                post_json,
                content,
                ROW_NUMBER() OVER (
                    PARTITION BY lou
                    ORDER BY last_seen_at DESC, id DESC
                ) AS row_number
            FROM post_versions
        )
    """
    with closing(_connect_readonly(db_path)) as connection:
        stats_row = cast(
            tuple[int, Optional[int]],
            connection.execute(
                f"""
                {latest_cte}
                SELECT COUNT(*), MAX(lou)
                FROM latest
                WHERE row_number = 1
                """
            ).fetchone(),
        )
        matching_row = cast(
            tuple[int],
            connection.execute(
                f"""
                {latest_cte}
                SELECT COUNT(*)
                FROM latest
                WHERE {where_sql}
                """,
                tuple(params),
            ).fetchone(),
        )
        post_count = stats_row[0]
        max_lou = stats_row[1]
        slot_total = max_lou + 1 if max_lou is not None and max_lou >= 0 else 0
        total_pages = max(1, math.ceil(slot_total / page_size))
        resolved_page = min(page, total_pages)
        page_start_lou = (resolved_page - 1) * page_size
        page_end_lou = page_start_lou + page_size - 1
        slot_end_lou = (
            min(page_end_lou, max_lou)
            if max_lou is not None and max_lou >= page_start_lou
            else page_start_lou - 1
        )
        rows = cast(
            list[tuple[int, Optional[int], str, str]],
            connection.execute(
                f"""
                {latest_cte}
                SELECT lou, pid, post_json, content
                FROM latest
                WHERE row_number = 1
                    AND lou >= ?
                    AND lou <= ?
                ORDER BY lou
                """,
                (page_start_lou, page_end_lou),
            ).fetchall(),
        )
        matching_page_lous = {
            lou
            for (lou,) in cast(
                list[tuple[int]],
                connection.execute(
                    f"""
                    {latest_cte}
                    SELECT lou
                    FROM latest
                    WHERE {where_sql}
                        AND lou >= ?
                        AND lou <= ?
                    """,
                    (*params, page_start_lou, page_end_lou),
                ).fetchall(),
            )
            if type(lou) is int
        }

    image_urls: set[str] = set()
    for _lou, _pid, post_json, _content in rows:
        image_urls.update(_attachment_urls(_raw_post_from_json(post_json)))
    image_mappings = _read_image_mappings_for_urls(output_dir, image_urls)

    floor_labels = _load_floor_labels(thread_folder, aid)
    row_by_lou = {row[0]: row for row in rows}
    slots: list[PostSlot] = []
    for lou in range(page_start_lou, slot_end_lou + 1):
        row = row_by_lou.get(lou)
        if row is None:
            slots.append(_empty_post_slot(lou, floor_labels, "missing"))
            continue
        if lou not in matching_page_lous:
            slot = _post_item_from_row(row, output_dir, floor_labels, image_mappings)
            slot["html"] = "<p><em>此楼层不匹配当前筛选。</em></p>"
            slot["emptyReason"] = "filtered"
            slots.append(slot)
            continue
        slots.append(
            _post_item_from_row(row, output_dir, floor_labels, image_mappings)
        )

    items = [slot for slot in slots if slot["emptyReason"] is None]

    return {
        "slots": slots,
        "items": items,
        "total": slot_total,
        "offset": page_start_lou,
        "limit": page_size,
        "page": resolved_page,
        "pageSize": page_size,
        "pageStartLou": page_start_lou,
        "pageEndLou": max(page_start_lou, slot_end_lou),
        "totalPages": total_pages,
        "postCount": post_count,
        "matchingPostCount": matching_row[0],
        "maxLou": max_lou,
    }


def safe_output_file(output_dir: Path, relative_path: str) -> Optional[Path]:
    if not relative_path or relative_path.startswith("/"):
        return None
    output_root = output_dir.resolve()
    candidate = (output_root / relative_path).resolve()
    if not candidate.is_relative_to(output_root):
        return None
    if not candidate.is_file():
        return None
    return candidate

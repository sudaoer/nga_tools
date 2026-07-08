from __future__ import annotations

import datetime
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import quote, unquote, urlsplit

from bs4 import BeautifulSoup, Tag

from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.backup.floor_models import FloorLabels
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)

ThreadStatus = Literal["ready", "needs_migration", "missing_html", "invalid"]

_THREAD_DIR_RE = re.compile(r"^(\d+)_(all|\d+)$")
_POST_HTML_RE = re.compile(r"^post_(\d+)\.html$")


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
    hasHtmlModified: bool
    hasFloorMap: bool
    hasWarnings: bool


class PostItem(TypedDict):
    lou: int
    pid: Optional[int]
    authorName: Optional[str]
    authorUid: Optional[int]
    postdate: Optional[int]
    floorLabel: str
    html: str


class PostsResult(TypedDict):
    items: list[PostItem]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True)
class ArchiveStats:
    post_count: int
    min_lou: Optional[int]
    max_lou: Optional[int]
    page_count: int


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


def _read_archive_stats(db_path: Path) -> ArchiveStats:
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
        page_row = cast(
            tuple[int],
            connection.execute(
                "SELECT COUNT(DISTINCT page_number) FROM page_snapshots"
            ).fetchone(),
        )

    return ArchiveStats(
        post_count=post_row[0],
        min_lou=post_row[1],
        max_lou=post_row[2],
        page_count=page_row[0],
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
    stats = ArchiveStats(post_count=0, min_lou=None, max_lou=None, page_count=0)

    if db_path.is_file():
        try:
            stats = _read_archive_stats(db_path)
            if has_html_modified:
                status = "ready"
            else:
                status = "missing_html"
                message = "缺少html_modified/post_*.html，请重新运行备份补齐本地HTML。"
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
        "link": _optional_str_metadata(metadata, "link"),
        "replies": _optional_int_metadata(metadata, "replies"),
        "postdate": _optional_int_metadata(metadata, "postdate"),
        "lastpost": _optional_int_metadata(metadata, "lastpost"),
        "postCount": stats.post_count,
        "minLou": stats.min_lou,
        "maxLou": stats.max_lou,
        "pageCount": stats.page_count,
        "updatedAt": updated_at,
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


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _is_external_src(src: str) -> bool:
    lowered = src.strip().lower()
    return lowered.startswith(
        ("http://", "https://", "//", "data:", "about:", "blob:")
    )


def rewrite_html_image_sources(
    html: str,
    html_dir: Path,
    output_dir: Path,
) -> str:
    if "<img" not in html.lower():
        return html

    soup = BeautifulSoup(html, "html.parser")
    images = cast(list[Tag], soup.find_all("img"))
    for image in images:
        src = _tag_attr_str(image, "src")
        if src is None or _is_external_src(src):
            continue

        parts = urlsplit(src)
        if parts.scheme or parts.netloc or not parts.path or parts.path.startswith("/"):
            continue

        candidate = (html_dir / unquote(parts.path)).resolve()
        output_url = _safe_output_url(candidate, output_dir)
        if output_url is not None:
            image["src"] = output_url

    return str(soup)


def _post_html_path(html_dir: Path, lou: int) -> Path:
    return html_dir / f"post_{lou}.html"


def _optional_int_from_post(post: dict[str, object], key: str) -> Optional[int]:
    value = post.get(key)
    if type(value) is int:
        return value
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


def _post_item_from_row(
    row: tuple[int, Optional[int], str],
    thread_folder: Path,
    output_dir: Path,
    floor_labels: FloorLabels,
) -> PostItem:
    lou, pid, post_json = row
    raw_post: object = json.loads(post_json)
    post = cast(dict[str, object], raw_post) if isinstance(raw_post, dict) else {}
    author_name, author_uid = _post_author(post)
    html_dir = thread_folder / "html_modified"
    html_path = _post_html_path(html_dir, lou)
    try:
        html = html_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        html = "<p><em>本楼层HTML缺失。</em></p>"

    return {
        "lou": lou,
        "pid": pid,
        "authorName": author_name,
        "authorUid": author_uid,
        "postdate": _optional_int_from_post(post, "postdate"),
        "floorLabel": floor_labels.label(lou),
        "html": rewrite_html_image_sources(html, html_dir, output_dir),
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
    offset: int,
    limit: int,
    query: str = "",
    lou_from: Optional[int] = None,
    lou_to: Optional[int] = None,
) -> PostsResult:
    aid = parse_aid_key(raw_aid_key)
    thread_folder = output_dir / f"{tid}_{raw_aid_key}"
    if not thread_folder.is_dir():
        raise ThreadNotFoundError("未找到对应备份目录。")
    db_path = thread_folder / ARCHIVE_DB_FILENAME
    html_dir = thread_folder / "html_modified"
    if not db_path.is_file():
        raise ThreadUnavailableError("缺少archive.sqlite3。")
    if not _has_post_html_files(html_dir):
        raise ThreadUnavailableError("缺少html_modified/post_*.html。")

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
        total_row = cast(
            tuple[int],
            connection.execute(
                f"{latest_cte} SELECT COUNT(*) FROM latest WHERE {where_sql}",
                tuple(params),
            ).fetchone(),
        )
        rows = cast(
            list[tuple[int, Optional[int], str]],
            connection.execute(
                f"""
                {latest_cte}
                SELECT lou, pid, post_json
                FROM latest
                WHERE {where_sql}
                ORDER BY lou
                LIMIT ? OFFSET ?
                """,
                (*params, limit, offset),
            ).fetchall(),
        )

    floor_labels = _load_floor_labels(thread_folder, aid)
    return {
        "items": [
            _post_item_from_row(row, thread_folder, output_dir, floor_labels)
            for row in rows
        ],
        "total": total_row[0],
        "offset": offset,
        "limit": limit,
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

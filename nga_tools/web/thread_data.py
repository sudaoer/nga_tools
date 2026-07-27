from __future__ import annotations

import datetime
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import nga_tools.config as config
from nga_tools.backup.archive_schema import require_current_archive_schema
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.backup.archive_posts import postdate_from_json
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.web.errors import WebConflict, WebNotFound
from nga_tools.web.sqlite_access import open_readonly_connection
from nga_tools.word_count import DEFAULT_MIN_BODY_CHARS, WORD_COUNT_VERSION

ThreadStatus = Literal["ready", "invalid"]
ThreadSummaryDetail = Literal["light", "full"]
PostDate = int | str

_THREAD_DIR_RE = re.compile(r"^(\d+)_(all|\d+)$")


class ThreadSummary(TypedDict):
    tid: int
    aid: Optional[int]
    aidKey: str
    dirName: str
    status: ThreadStatus
    message: Optional[str]
    statsLoaded: bool
    threadName: Optional[str]
    subject: Optional[str]
    author: Optional[str]
    link: Optional[str]
    replies: Optional[int]
    postdate: Optional[int]
    lastpost: Optional[int]
    postCount: Optional[int]
    bodyWordCount: Optional[int]
    bodyChineseCharCount: Optional[int]
    bodyWordPostCount: Optional[int]
    minLou: Optional[int]
    maxLou: Optional[int]
    pageCount: Optional[int]
    updatedAt: Optional[str]
    authorUpdatedAt: Optional[PostDate]
    hasWarnings: bool



class PostVersionThreadSummary(ThreadSummary):
    multiVersionFloorCount: int



class PostVersionThreadSummariesResult(TypedDict):
    items: list[PostVersionThreadSummary]



@dataclass(frozen=True)


class ArchiveStats:
    post_count: int
    body_word_count: Optional[int]
    body_chinese_char_count: Optional[int]
    body_word_post_count: Optional[int]
    min_lou: Optional[int]
    max_lou: Optional[int]
    page_count: int
    author_updated_at: Optional[PostDate]



class ThreadNotFoundError(WebNotFound):
    pass



class ThreadUnavailableError(WebConflict):
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



def _read_archive_stats(db_path: Path) -> ArchiveStats:
    latest_cte = """
        WITH latest AS (
            SELECT
                post_versions.lou,
                post_latest_metadata.postdate_json,
                ROW_NUMBER() OVER (
                    PARTITION BY post_versions.lou
                    ORDER BY post_versions.last_seen_at DESC, post_versions.id DESC
                ) AS row_number
            FROM post_versions
            LEFT JOIN post_latest_metadata
                ON post_latest_metadata.pid = post_versions.pid
                AND post_latest_metadata.lou = post_versions.lou
        )
    """
    with closing(open_readonly_connection(db_path)) as connection:
        require_current_archive_schema(connection, db_path)
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
            Optional[tuple[Optional[str]]],
            connection.execute(
                f"""
                {latest_cte}
                SELECT postdate_json
                FROM latest
                WHERE row_number = 1
                ORDER BY lou DESC
                LIMIT 1
                """
            ).fetchone(),
        )
        page_row = cast(
            tuple[int],
            connection.execute("SELECT COUNT(*) FROM archive_pages").fetchone(),
        )
        body_word_count: Optional[int] = None
        body_chinese_char_count: Optional[int] = None
        body_word_post_count: Optional[int] = None
        word_row = cast(
            tuple[int, int, int, int],
            connection.execute(
                    """
                    WITH latest AS (
                        SELECT
                            word_count_version,
                            word_count_chinese_chars,
                            word_count_chinese_with_punctuation,
                            ROW_NUMBER() OVER (
                                PARTITION BY lou
                                ORDER BY last_seen_at DESC, id DESC
                            ) AS row_number
                        FROM post_versions
                    )
                    SELECT
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_version = ?
                                    THEN 1
                                    ELSE 0
                                END
                            ),
                            0
                        ),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_version = ?
                                        AND word_count_chinese_with_punctuation >= ?
                                    THEN 1
                                    ELSE 0
                                END
                            ),
                            0
                        ),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_version = ?
                                        AND word_count_chinese_with_punctuation >= ?
                                    THEN word_count_chinese_chars
                                    ELSE 0
                                END
                            ),
                            0
                        ),
                        COALESCE(
                            SUM(
                                CASE
                                    WHEN word_count_version = ?
                                        AND word_count_chinese_with_punctuation >= ?
                                    THEN word_count_chinese_with_punctuation
                                    ELSE 0
                                END
                            ),
                            0
                        )
                    FROM latest
                    WHERE row_number = 1
                    """,
                    (
                        WORD_COUNT_VERSION,
                        WORD_COUNT_VERSION,
                        DEFAULT_MIN_BODY_CHARS,
                        WORD_COUNT_VERSION,
                        DEFAULT_MIN_BODY_CHARS,
                        WORD_COUNT_VERSION,
                        DEFAULT_MIN_BODY_CHARS,
                    ),
            ).fetchone(),
        )
        if word_row[0] == post_row[0]:
            body_word_post_count = word_row[1]
            body_chinese_char_count = word_row[2]
            body_word_count = word_row[3]

    author_updated_at: Optional[PostDate] = None
    if latest_post_row is not None:
        author_updated_at = postdate_from_json(latest_post_row[0])

    return ArchiveStats(
        post_count=post_row[0],
        body_word_count=body_word_count,
        body_chinese_char_count=body_chinese_char_count,
        body_word_post_count=body_word_post_count,
        min_lou=post_row[1],
        max_lou=post_row[2],
        page_count=page_row[0],
        author_updated_at=author_updated_at,
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
            base_url = config.get_config().base_url
        except (FileNotFoundError, ValueError):
            base_url = "https://bbs.nga.cn"
        link = f"{base_url.rstrip('/')}/read.php?tid={tid}"
    if aid is None:
        return link
    return _link_with_authorid(link, tid, aid)



def _thread_summary_for_folder(
    thread_folder: Path,
    metadata: Optional[ThreadConfig],
    *,
    detail: ThreadSummaryDetail = "full",
) -> Optional[ThreadSummary]:
    parsed = parse_thread_dir_name(thread_folder.name)
    if parsed is None:
        return None

    tid, aid, raw_aid_key = parsed
    db_path = thread_folder / ARCHIVE_DB_FILENAME
    warnings_path = thread_folder / "warnings.log"
    has_warnings = warnings_path.is_file()
    status: ThreadStatus = "invalid"
    message: Optional[str] = None
    stats_loaded = False
    post_count: Optional[int] = None
    body_word_count: Optional[int] = None
    body_chinese_char_count: Optional[int] = None
    body_word_post_count: Optional[int] = None
    min_lou: Optional[int] = None
    max_lou: Optional[int] = None
    page_count: Optional[int] = None
    author_updated_at: Optional[PostDate] = None

    if db_path.is_file():
        if detail == "light":
            status = "ready"
        else:
            try:
                stats = _read_archive_stats(db_path)
                stats_loaded = True
                post_count = stats.post_count
                body_word_count = stats.body_word_count
                body_chinese_char_count = stats.body_chinese_char_count
                body_word_post_count = stats.body_word_post_count
                min_lou = stats.min_lou
                max_lou = stats.max_lou
                page_count = stats.page_count
                author_updated_at = stats.author_updated_at
                status = "ready"
            except (sqlite3.Error, RuntimeError, ValueError) as error:
                status = "invalid"
                message = f"archive.sqlite3无法读取：{error}"
    elif (thread_folder / "json").is_dir():
        return None
    else:
        status = "invalid"
        message = "未找到archive.sqlite3。"

    updated_at = _latest_mtime(
        [
            db_path,
            Path(str(db_path) + "-wal"),
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
        "statsLoaded": stats_loaded,
        "threadName": _metadata_name(metadata),
        "subject": _optional_str_metadata(metadata, "subject"),
        "author": _optional_str_metadata(metadata, "author"),
        "link": _thread_link(metadata, tid=tid, aid=aid),
        "replies": _optional_int_metadata(metadata, "replies"),
        "postdate": _optional_int_metadata(metadata, "postdate"),
        "lastpost": _optional_int_metadata(metadata, "lastpost"),
        "postCount": post_count,
        "bodyWordCount": body_word_count,
        "bodyChineseCharCount": body_chinese_char_count,
        "bodyWordPostCount": body_word_post_count,
        "minLou": min_lou,
        "maxLou": max_lou,
        "pageCount": page_count,
        "updatedAt": updated_at,
        "authorUpdatedAt": author_updated_at,
        "hasWarnings": has_warnings,
    }



def scan_thread_summaries(
    output_dir: Path,
    metadata_by_key: dict[tuple[int, str], ThreadConfig],
    *,
    detail: ThreadSummaryDetail = "full",
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
            detail=detail,
        )
        if summary is not None:
            summaries.append(summary)

    return sorted(
        summaries,
        key=lambda item: (item["updatedAt"] or "", item["dirName"]),
        reverse=True,
    )



def _read_multi_version_floor_count(thread_folder: Path) -> int:
    archive_db_path = thread_folder / ARCHIVE_DB_FILENAME
    if not archive_db_path.is_file():
        return 0
    try:
        with closing(open_readonly_connection(archive_db_path)) as connection:
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM (
                    SELECT lou
                    FROM post_versions
                    GROUP BY lou
                    HAVING COUNT(*) > 1
                )
                """
            ).fetchone()
    except sqlite3.Error:
        return 0
    if row is None or type(row[0]) is not int:
        return 0
    return row[0]



def read_post_version_thread_summaries(
    output_dir: Path,
    metadata_by_key: dict[tuple[int, str], ThreadConfig],
    *,
    multi_version_only: bool = False,
    detail: ThreadSummaryDetail = "full",
) -> PostVersionThreadSummariesResult:
    items: list[PostVersionThreadSummary] = []
    for summary in scan_thread_summaries(output_dir, metadata_by_key, detail=detail):
        if summary["status"] != "ready":
            continue
        thread_folder = output_dir / summary["dirName"]
        multi_version_floor_count = _read_multi_version_floor_count(thread_folder)
        if multi_version_only and multi_version_floor_count == 0:
            continue
        item = cast(PostVersionThreadSummary, dict(summary))
        item["multiVersionFloorCount"] = multi_version_floor_count
        items.append(item)
    return {"items": items}



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

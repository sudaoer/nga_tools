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
from nga_tools.backup.archive_posts import (
    image_attachments_from_json,
    postdate_from_json,
)
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.backup.archive_store import ArchivePostVersionRow
from nga_tools.backup.archive import refresh_html_modified_for_lous
from nga_tools.backup.floor_models import (
    MISSING_POST_HTML,
    ORIGINAL_POSTS_PER_PAGE,
    FloorLabels,
)
from nga_tools.backup.post_data import (
    attachment_url_from_value,
    make_image_src_resolver,
)
from nga_tools.backup.post_version_selection import (
    load_selections,
    make_selection,
    write_selections,
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
from nga_tools.word_count import DEFAULT_MIN_BODY_CHARS, WORD_COUNT_VERSION

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
    bodyWordCount: Optional[int]
    bodyChineseCharCount: Optional[int]
    bodyWordPostCount: Optional[int]
    minLou: Optional[int]
    maxLou: Optional[int]
    pageCount: int
    updatedAt: Optional[str]
    authorUpdatedAt: Optional[PostDate]
    hasHtmlModified: bool
    hasFloorMap: bool
    hasWarnings: bool


class PostVersionThreadSummary(ThreadSummary):
    multiVersionFloorCount: int


class PostVersionThreadSummariesResult(TypedDict):
    items: list[PostVersionThreadSummary]


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


def _has_word_count_columns(connection: sqlite3.Connection) -> bool:
    rows = connection.execute("PRAGMA table_info(post_versions)").fetchall()
    columns = {row[1] for row in rows if isinstance(row[1], str)}
    return {
        "word_count_version",
        "word_count_chinese_chars",
        "word_count_chinese_with_punctuation",
    } <= columns


def _read_archive_stats(db_path: Path) -> ArchiveStats:
    ThreadArchiveStore(db_path.parent).ensure_schema()
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
            connection.execute(
                "SELECT COUNT(DISTINCT page_number) FROM page_snapshots"
            ).fetchone(),
        )
        body_word_count: Optional[int] = None
        body_chinese_char_count: Optional[int] = None
        body_word_post_count: Optional[int] = None
        if _has_word_count_columns(connection):
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
        body_word_count=None,
        body_chinese_char_count=None,
        body_word_post_count=None,
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
        except (sqlite3.Error, RuntimeError, ValueError) as error:
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
        "bodyWordCount": stats.body_word_count,
        "bodyChineseCharCount": stats.body_chinese_char_count,
        "bodyWordPostCount": stats.body_word_post_count,
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


def _read_multi_version_floor_count(thread_folder: Path) -> int:
    archive_db_path = thread_folder / ARCHIVE_DB_FILENAME
    if not archive_db_path.is_file():
        return 0
    try:
        with closing(_connect_readonly(archive_db_path)) as connection:
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
) -> PostVersionThreadSummariesResult:
    items: list[PostVersionThreadSummary] = []
    for summary in scan_thread_summaries(output_dir, metadata_by_key):
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


def _attachment_urls(
    content: str,
    image_attachments_json: Optional[str],
) -> set[str]:
    urls: set[str] = set()
    for match in _IMG_BBCODE_RE.finditer(content):
        url = attachment_url_from_value(match.group(1))
        if url is not None:
            urls.add(url)

    urls.update(
        attachment["url"]
        for attachment in image_attachments_from_json(image_attachments_json)
    )
    return urls


def _unresolved_image_src(_image_src: str) -> str | None:
    return None


def _image_src_resolver(
    image_attachments_json: Optional[str],
    output_dir: Path,
    image_mappings: dict[str, str],
) -> ImageSrcResolver:
    attachment_resolver: ImageSrcResolver
    attachments = image_attachments_from_json(image_attachments_json)
    if attachments:
        attachment_resolver = make_image_src_resolver(attachments)
    else:
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
) -> PostSlot:
    html = render_web_bbcode(
        row.content,
        image_src_resolver=_image_src_resolver(
            row.image_attachments_json,
            output_dir,
            image_mappings,
        ),
    )
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
    }


def _post_row_matches(
    row: ArchivePostVersionRow,
    query: str,
    lou_from: Optional[int],
    lou_to: Optional[int],
) -> bool:
    if query and query not in row.content:
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
    archive_store.ensure_schema()

    page_size = ORIGINAL_POSTS_PER_PAGE
    effective_rows = archive_store.read_effective_post_rows()
    post_count = len(effective_rows)
    max_lou = max((row.lou for row in effective_rows), default=None)
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
    stripped_query = query.strip()
    matching_lous = {
        row.lou
        for row in effective_rows
        if _post_row_matches(row, stripped_query, lou_from, lou_to)
    }
    rows = [
        row
        for row in effective_rows
        if page_start_lou <= row.lou <= page_end_lou
    ]
    matching_page_lous = {
        lou
        for lou in matching_lous
        if page_start_lou <= lou <= page_end_lou
    }

    image_urls: set[str] = set()
    for row in rows:
        image_urls.update(_attachment_urls(row.content, row.image_attachments_json))
    image_mappings = _read_image_mappings_for_urls(output_dir, image_urls)

    floor_labels = _load_floor_labels(thread_folder, aid)
    row_by_lou = {row.lou: row for row in rows}
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
        "matchingPostCount": len(matching_lous),
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
    archive_store.ensure_schema()
    return archive_store, thread_folder, aid


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
    archive_store, thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    selected_by_lou = archive_store.read_valid_post_version_selections()
    floor_labels = _load_floor_labels(thread_folder, aid)
    with closing(_connect_readonly(archive_store.db_path)) as connection:
        rows = cast(
            list[tuple[int, int, str, str, str, str, int, int]],
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
        content,
        first_seen_at,
        last_seen_at,
        seen_count,
        row_number,
    ) in rows:
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
    archive_store, thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    row = archive_store.read_post_row_for_version(version_id)
    if row is None:
        raise ValueError("未知帖子正文版本。")

    image_urls = _attachment_urls(row.content, row.image_attachments_json)
    image_mappings = _read_image_mappings_for_urls(output_dir, image_urls)
    return {
        "item": _post_item_from_row(
            row,
            output_dir,
            _load_floor_labels(thread_folder, aid),
            image_mappings,
        )
    }


def _latest_version_id_for_lou(connection: sqlite3.Connection, lou: int) -> Optional[int]:
    latest_row = cast(
        Optional[tuple[int]],
        connection.execute(
            """
            SELECT id
            FROM post_versions
            WHERE lou = ?
            ORDER BY last_seen_at DESC, id DESC
            LIMIT 1
            """,
            (lou,),
        ).fetchone(),
    )
    if latest_row is None:
        return None
    return latest_row[0]


def select_post_version(
    output_dir: Path,
    tid: int,
    raw_aid_key: str,
    lou: int,
    version_id: int,
) -> PostVersionSelectionResult:
    archive_store, thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    with closing(_connect_readonly(archive_store.db_path)) as connection:
        version_row = cast(
            Optional[tuple[int, str]],
            connection.execute(
                """
                SELECT lou, source_hash
                FROM post_versions
                WHERE id = ?
                """,
                (version_id,),
            ).fetchone(),
        )
        if version_row is None:
            raise ValueError("未知帖子正文版本。")
        version_lou, source_hash = version_row
        if version_lou != lou:
            raise ValueError("帖子正文版本不属于指定楼层。")
        latest_version_id = _latest_version_id_for_lou(connection, lou)
        if latest_version_id is None:
            raise ValueError("未知楼层。")
        if latest_version_id == version_id:
            raise ValueError("不能手动选择当前最新版。")

    selections = load_selections(thread_folder)
    selections[lou] = make_selection(version_id, source_hash)
    write_selections(thread_folder, selections)
    refresh_html_modified_for_lous(tid, aid, {lou}, thread_folder)
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
    archive_store, thread_folder, aid = _archive_store_for_thread(
        output_dir,
        tid,
        raw_aid_key,
    )
    with closing(_connect_readonly(archive_store.db_path)) as connection:
        latest_version_id = _latest_version_id_for_lou(connection, lou)
        if latest_version_id is None:
            raise ValueError("未知楼层。")

    selections = load_selections(thread_folder)
    selections.pop(lou, None)
    write_selections(thread_folder, selections)
    refresh_html_modified_for_lous(tid, aid, {lou}, thread_folder)
    return {
        "lou": lou,
        "selectedVersionId": None,
        "activeVersionId": latest_version_id,
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

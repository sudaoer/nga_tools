from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypeAlias, TypedDict, cast

from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import ForumThread
from nga_tools.forum.thread_configs import (
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.forum.timing import ForumSyncTimingCollector

DEFAULT_WATCH_CONFIG_PATH = "forum_watch_configs.json"
DEFAULT_NAME_TEMPLATE = "{watch_name}-{tid}"
DEFAULT_MIN_REPLIES = 500
DEFAULT_MIN_AUTHOR_LOUS = 20

JsonObject: TypeAlias = dict[str, object]
SyncStatus: TypeAlias = Literal["added", "updated", "skipped", "conflict"]
ForumScanProgressCallback: TypeAlias = Callable[["ForumScanProgress"], None]
ForumPageCallback: TypeAlias = Callable[[int, list[ForumThread]], None]
ForumDatabaseScanProgressCallback: TypeAlias = Callable[
    ["ForumDatabaseScanProgress"],
    None,
]
ForumThreadSource: TypeAlias = Callable[["ForumWatchConfig"], list[ForumThread]]


class ForumWatchConfig(TypedDict):
    watch_name: str
    fid: int
    pages: int
    min_replies: int
    min_author_lous: int
    keywords: list[str]
    exclude_keywords: list[str]
    include_tids: list[int]
    name_template: str


@dataclass(frozen=True)
class MatchedForumThread:
    watch_name: str
    thread: ForumThread
    thread_name: str


@dataclass(frozen=True)
class ForumScanProgress:
    watch_name: str
    fid: int
    page: int
    pages: int
    scanned_count: int
    matched_count: int


@dataclass(frozen=True)
class ForumDatabaseScanProgress:
    watch_name: str
    fid: int
    scanned_count: int
    matched_count: int


@dataclass(frozen=True)
class SyncOutcome:
    match: MatchedForumThread
    status: SyncStatus
    message: str


def _read_json_object(path: Path) -> JsonObject:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"版面监控配置文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"版面监控配置文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"版面监控配置文件顶层必须是JSON对象：{path}")

    data = cast(dict[object, object], raw_data)
    if not all(isinstance(key, str) for key in data):
        raise ValueError(f"版面监控配置文件的键必须都是字符串：{path}")

    return cast(JsonObject, data)


def _required_str(data: JsonObject, key: str, source: object) -> str:
    value = data.get(key)
    if isinstance(value, str) and value:
        return value
    raise ValueError(f"版面监控配置缺少字符串字段 {key}：{source!r}")


def _optional_str(data: JsonObject, key: str, default: str, source: object) -> str:
    value = data.get(key, default)
    if isinstance(value, str):
        return value
    raise ValueError(f"版面监控配置字段 {key} 必须是字符串：{source!r}")


def _required_int(data: JsonObject, key: str, source: object) -> int:
    value = data.get(key)
    if type(value) is int:
        return value
    raise ValueError(f"版面监控配置缺少整数字段 {key}：{source!r}")


def _optional_positive_int(
    data: JsonObject,
    key: str,
    default: int,
    source: object,
) -> int:
    value = data.get(key, default)
    if type(value) is int and value > 0:
        return value
    raise ValueError(f"版面监控配置字段 {key} 必须是正整数：{source!r}")


def _optional_str_list(data: JsonObject, key: str, source: object) -> list[str]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"版面监控配置字段 {key} 必须是字符串数组：{source!r}")

    values: list[str] = []
    for item in cast(list[object], value):
        if not isinstance(item, str):
            raise ValueError(f"版面监控配置字段 {key} 必须是字符串数组：{source!r}")
        values.append(item)
    return values


def _optional_int_list(data: JsonObject, key: str, source: object) -> list[int]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"版面监控配置字段 {key} 必须是整数数组：{source!r}")

    values: list[int] = []
    for item in cast(list[object], value):
        if type(item) is not int:
            raise ValueError(f"版面监控配置字段 {key} 必须是整数数组：{source!r}")
        values.append(item)
    return values


def _parse_watch_config(item: object) -> ForumWatchConfig:
    if not isinstance(item, dict):
        raise ValueError(f"版面监控配置项必须是对象：{item!r}")

    data = cast(JsonObject, item)
    source: object = data
    return {
        "watch_name": _required_str(data, "watch_name", source),
        "fid": _required_int(data, "fid", source),
        "pages": _optional_positive_int(data, "pages", 1, source),
        "min_replies": _optional_positive_int(
            data,
            "min_replies",
            DEFAULT_MIN_REPLIES,
            source,
        ),
        "min_author_lous": _optional_positive_int(
            data,
            "min_author_lous",
            DEFAULT_MIN_AUTHOR_LOUS,
            source,
        ),
        "keywords": _optional_str_list(data, "keywords", source),
        "exclude_keywords": _optional_str_list(data, "exclude_keywords", source),
        "include_tids": _optional_int_list(data, "include_tids", source),
        "name_template": _optional_str(
            data,
            "name_template",
            DEFAULT_NAME_TEMPLATE,
            source,
        ),
    }


def load_forum_watch_configs(path: str | Path) -> list[ForumWatchConfig]:
    data = _read_json_object(Path(path))
    raw_watch_list = data.get("ForumWatchList", [])
    if not isinstance(raw_watch_list, list):
        raise ValueError("版面监控配置字段 ForumWatchList 必须是数组。")

    return [_parse_watch_config(item) for item in cast(list[object], raw_watch_list)]


def _contains_any(text: str, needles: list[str]) -> bool:
    normalized_text = text.casefold()
    return any(needle.casefold() in normalized_text for needle in needles if needle)


def _thread_is_forced(thread: ForumThread, watch_config: ForumWatchConfig) -> bool:
    return thread["tid"] in watch_config["include_tids"]


def thread_matches_watch(thread: ForumThread, watch_config: ForumWatchConfig) -> bool:
    if _thread_is_forced(thread, watch_config):
        return True

    subject = thread["subject"]
    has_keyword = _contains_any(subject, watch_config["keywords"])
    has_excluded_keyword = _contains_any(subject, watch_config["exclude_keywords"])
    has_enough_replies = thread["replies"] >= watch_config["min_replies"]
    return has_keyword and not has_excluded_keyword and has_enough_replies


def _author_lou_count_for_thread(
    client: NGAClient,
    thread: ForumThread,
    timing_collector: ForumSyncTimingCollector | None = None,
) -> int:
    if timing_collector is None:
        page_data = client.get_page(thread["tid"], thread["authorid"], 1)
    else:
        timing_collector.record_author_page_request()
        with timing_collector.measure("author_page_request"):
            page_data = client.get_page(thread["tid"], thread["authorid"], 1)
    author_lous = page_data.get("vrows")
    if type(author_lous) is int:
        return author_lous
    raise ValueError(
        "NGA只看作者页缺少有效vrows："
        f"tid={thread['tid']}, aid={thread['authorid']}, vrows={author_lous!r}"
    )


def _thread_has_enough_author_lous(
    client: NGAClient,
    thread: ForumThread,
    watch_config: ForumWatchConfig,
    timing_collector: ForumSyncTimingCollector | None = None,
) -> bool:
    return (
        _author_lou_count_for_thread(client, thread, timing_collector)
        >= watch_config["min_author_lous"]
    )


def _template_values(
    watch_config: ForumWatchConfig,
    thread: ForumThread,
) -> dict[str, object]:
    return {
        "watch_name": watch_config["watch_name"],
        "fid": thread["fid"],
        "tid": thread["tid"],
        "subject": thread["subject"],
        "author": thread["author"],
        "authorid": thread["authorid"],
        "postdate": thread["postdate"],
        "lastpost": thread["lastpost"],
        "replies": thread["replies"],
        "forumname": thread["forumname"],
    }


def _render_template(
    template: str,
    watch_config: ForumWatchConfig,
    thread: ForumThread,
) -> str:
    try:
        rendered = template.format(**_template_values(watch_config, thread))
    except (KeyError, IndexError, ValueError) as error:
        raise ValueError(
            f"版面监控模板无法渲染：{template!r}, tid={thread['tid']}"
        ) from error
    return rendered.strip()


def build_matched_thread(
    watch_config: ForumWatchConfig,
    thread: ForumThread,
) -> MatchedForumThread:
    thread_name = _render_template(watch_config["name_template"], watch_config, thread)
    if not thread_name:
        raise ValueError(f"版面监控模板生成了空帖子名称：tid={thread['tid']}")

    return MatchedForumThread(
        watch_name=watch_config["watch_name"],
        thread=thread,
        thread_name=thread_name,
    )


def _matching_thread_or_none(
    client: NGAClient,
    watch_config: ForumWatchConfig,
    thread: ForumThread,
    existing_thread_list: list[ThreadConfig] | None,
    timing_collector: ForumSyncTimingCollector | None = None,
) -> MatchedForumThread | None:
    if not thread_matches_watch(thread, watch_config):
        return None

    matched_thread = build_matched_thread(watch_config, thread)
    if (
        existing_thread_list is not None
        and _match_would_not_add(existing_thread_list, matched_thread)
    ):
        return matched_thread

    if not _thread_is_forced(thread, watch_config):
        if not _thread_has_enough_author_lous(
            client,
            thread,
            watch_config,
            timing_collector,
        ):
            return None
    return matched_thread


def collect_matching_threads(
    client: NGAClient,
    watch_configs: list[ForumWatchConfig],
    progress_callback: ForumScanProgressCallback | None = None,
    existing_thread_list: list[ThreadConfig] | None = None,
    forum_page_callback: ForumPageCallback | None = None,
) -> tuple[int, list[MatchedForumThread]]:
    scanned_count = 0
    matched_threads: list[MatchedForumThread] = []

    for watch_config in watch_configs:
        for page in range(1, watch_config["pages"] + 1):
            page_threads = client.get_forum_threads(watch_config["fid"], page)
            if forum_page_callback is not None:
                forum_page_callback(watch_config["fid"], page_threads)
            scanned_count += len(page_threads)
            for thread in page_threads:
                matched_thread = _matching_thread_or_none(
                    client,
                    watch_config,
                    thread,
                    existing_thread_list,
                )
                if matched_thread is not None:
                    matched_threads.append(matched_thread)
            if progress_callback is not None:
                progress_callback(
                    ForumScanProgress(
                        watch_name=watch_config["watch_name"],
                        fid=watch_config["fid"],
                        page=page,
                        pages=watch_config["pages"],
                        scanned_count=scanned_count,
                        matched_count=len(matched_threads),
                    )
                )

    return scanned_count, matched_threads


def collect_matching_threads_from_thread_source(
    client: NGAClient,
    watch_configs: list[ForumWatchConfig],
    thread_source: ForumThreadSource,
    progress_callback: ForumDatabaseScanProgressCallback | None = None,
    existing_thread_list: list[ThreadConfig] | None = None,
    timing_collector: ForumSyncTimingCollector | None = None,
) -> tuple[int, list[MatchedForumThread]]:
    scanned_count = 0
    matched_threads: list[MatchedForumThread] = []

    for watch_config in watch_configs:
        if timing_collector is None:
            threads = thread_source(watch_config)
        else:
            with timing_collector.measure("database_read"):
                threads = thread_source(watch_config)
            timing_collector.record_scanned_threads(len(threads))
        scanned_count += len(threads)
        for thread in threads:
            matched_thread = _matching_thread_or_none(
                client,
                watch_config,
                thread,
                existing_thread_list,
                timing_collector,
            )
            if matched_thread is not None:
                matched_threads.append(matched_thread)
        if progress_callback is not None:
            progress_callback(
                ForumDatabaseScanProgress(
                    watch_name=watch_config["watch_name"],
                    fid=watch_config["fid"],
                    scanned_count=scanned_count,
                    matched_count=len(matched_threads),
                )
            )

    return scanned_count, matched_threads


def _find_exact_thread(
    thread_list: list[ThreadConfig],
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if (
            thread_config_tid(thread_config) == tid
            and thread_config_aid(thread_config) == aid
        ):
            return thread_config
    return None


def _find_tid_conflict(
    thread_list: list[ThreadConfig],
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if (
            thread_config_tid(thread_config) == tid
            and thread_config_aid(thread_config) != aid
        ):
            return thread_config
    return None


def _find_name_conflict(
    thread_list: list[ThreadConfig],
    thread_name: str,
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if thread_config_name(thread_config) != thread_name:
            continue
        if (
            thread_config_tid(thread_config) == tid
            and thread_config_aid(thread_config) == aid
        ):
            continue
        return thread_config
    return None


def _match_would_not_add(
    thread_list: list[ThreadConfig],
    match: MatchedForumThread,
) -> bool:
    tid = match.thread["tid"]
    aid = match.thread["authorid"]
    return (
        _find_exact_thread(thread_list, tid, aid) is not None
        or _find_tid_conflict(thread_list, tid, aid) is not None
        or _find_name_conflict(thread_list, match.thread_name, tid, aid) is not None
    )


def build_thread_link(base_url: str, tid: int) -> str:
    return f"{base_url.rstrip('/')}/read.php?tid={tid}"


def _managed_thread_fields(
    match: MatchedForumThread,
    *,
    base_url: str,
) -> ThreadConfig:
    thread = match.thread
    return {
        "tid": thread["tid"],
        "aid": thread["authorid"],
        "link": build_thread_link(base_url, thread["tid"]),
        "subject": thread["subject"],
        "author": thread["author"],
        "fid": thread["fid"],
        "forumname": thread["forumname"],
        "replies": thread["replies"],
        "postdate": thread["postdate"],
        "lastpost": thread["lastpost"],
    }


def _update_thread_fields(
    thread_config: ThreadConfig,
    fields: ThreadConfig,
) -> bool:
    changed = False
    for key, value in fields.items():
        if thread_config.get(key) == value:
            continue
        thread_config[key] = value
        changed = True
    return changed


def sync_matches_to_thread_list(
    thread_list: list[ThreadConfig],
    matches: list[MatchedForumThread],
    *,
    base_url: str,
) -> list[SyncOutcome]:
    outcomes: list[SyncOutcome] = []

    for match in matches:
        tid = match.thread["tid"]
        aid = match.thread["authorid"]
        managed_fields = _managed_thread_fields(match, base_url=base_url)

        exact_thread = _find_exact_thread(thread_list, tid, aid)
        if exact_thread is not None:
            if _update_thread_fields(exact_thread, managed_fields):
                outcomes.append(
                    SyncOutcome(
                        match=match,
                        status="updated",
                        message=f"已更新帖子数据：{thread_config_name(exact_thread)}",
                    )
                )
                continue
            outcomes.append(
                SyncOutcome(
                    match=match,
                    status="skipped",
                    message=f"已是最新配置：{thread_config_name(exact_thread)}",
                )
            )
            continue

        tid_conflict = _find_tid_conflict(thread_list, tid, aid)
        if tid_conflict is not None:
            outcomes.append(
                SyncOutcome(
                    match=match,
                    status="conflict",
                    message=(
                        f"tid已存在但aid不同：{thread_config_name(tid_conflict)} "
                        f"(aid={thread_config_aid(tid_conflict)})"
                    ),
                )
            )
            continue

        name_conflict = _find_name_conflict(thread_list, match.thread_name, tid, aid)
        if name_conflict is not None:
            outcomes.append(
                SyncOutcome(
                    match=match,
                    status="conflict",
                    message=(
                        f"名称已被占用：{thread_config_name(name_conflict)} "
                        f"(tid={thread_config_tid(name_conflict)}, "
                        f"aid={thread_config_aid(name_conflict)})"
                    ),
                )
            )
            continue

        thread_list.append(
            {
                "thread_name": match.thread_name,
                **managed_fields,
            }
        )
        outcomes.append(
            SyncOutcome(match=match, status="added", message="已添加配置")
        )

    return outcomes

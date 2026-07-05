from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypeAlias, TypedDict, cast

from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import ForumThread
from nga_tools.thread_configs import ThreadConfig

DEFAULT_WATCH_CONFIG_PATH = "forum_watch_configs.json"
DEFAULT_NAME_TEMPLATE = "{watch_name}-{tid}"
DEFAULT_DESCRIPTION_TEMPLATE = "{forumname} | {author}: {subject}"
DEFAULT_MIN_REPLIES = 500

JsonObject: TypeAlias = dict[str, object]
SyncStatus: TypeAlias = Literal["added", "skipped", "conflict"]


class ForumWatchConfig(TypedDict):
    watch_name: str
    fid: int
    pages: int
    min_replies: int
    keywords: list[str]
    exclude_keywords: list[str]
    include_tids: list[int]
    name_template: str
    description_template: str


@dataclass(frozen=True)
class MatchedForumThread:
    watch_name: str
    thread: ForumThread
    thread_name: str
    description: str


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
        "keywords": _optional_str_list(data, "keywords", source),
        "exclude_keywords": _optional_str_list(data, "exclude_keywords", source),
        "include_tids": _optional_int_list(data, "include_tids", source),
        "name_template": _optional_str(
            data,
            "name_template",
            DEFAULT_NAME_TEMPLATE,
            source,
        ),
        "description_template": _optional_str(
            data,
            "description_template",
            DEFAULT_DESCRIPTION_TEMPLATE,
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


def thread_matches_watch(thread: ForumThread, watch_config: ForumWatchConfig) -> bool:
    if thread["tid"] in watch_config["include_tids"]:
        return True

    subject = thread["subject"]
    has_keyword = _contains_any(subject, watch_config["keywords"])
    has_excluded_keyword = _contains_any(subject, watch_config["exclude_keywords"])
    has_enough_replies = thread["replies"] >= watch_config["min_replies"]
    return has_keyword and not has_excluded_keyword and has_enough_replies


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
        description=_render_template(
            watch_config["description_template"],
            watch_config,
            thread,
        ),
    )


def collect_matching_threads(
    client: NGAClient,
    watch_configs: list[ForumWatchConfig],
) -> tuple[int, list[MatchedForumThread]]:
    scanned_count = 0
    matched_threads: list[MatchedForumThread] = []

    for watch_config in watch_configs:
        for page in range(1, watch_config["pages"] + 1):
            page_threads = client.get_forum_threads(watch_config["fid"], page)
            scanned_count += len(page_threads)
            for thread in page_threads:
                if thread_matches_watch(thread, watch_config):
                    matched_threads.append(build_matched_thread(watch_config, thread))

    return scanned_count, matched_threads


def _find_exact_thread(
    thread_list: list[ThreadConfig],
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if thread_config["tid"] == tid and thread_config.get("aid") == aid:
            return thread_config
    return None


def _find_tid_conflict(
    thread_list: list[ThreadConfig],
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if thread_config["tid"] == tid and thread_config.get("aid") != aid:
            return thread_config
    return None


def _find_name_conflict(
    thread_list: list[ThreadConfig],
    thread_name: str,
    tid: int,
    aid: Optional[int],
) -> Optional[ThreadConfig]:
    for thread_config in thread_list:
        if thread_config["thread_name"] != thread_name:
            continue
        if thread_config["tid"] == tid and thread_config.get("aid") == aid:
            continue
        return thread_config
    return None


def sync_matches_to_thread_list(
    thread_list: list[ThreadConfig],
    matches: list[MatchedForumThread],
) -> list[SyncOutcome]:
    outcomes: list[SyncOutcome] = []

    for match in matches:
        tid = match.thread["tid"]
        aid = match.thread["authorid"]

        exact_thread = _find_exact_thread(thread_list, tid, aid)
        if exact_thread is not None:
            outcomes.append(
                SyncOutcome(
                    match=match,
                    status="skipped",
                    message=f"已存在配置：{exact_thread['thread_name']}",
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
                        f"tid已存在但aid不同：{tid_conflict['thread_name']} "
                        f"(aid={tid_conflict.get('aid')})"
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
                        f"名称已被占用：{name_conflict['thread_name']} "
                        f"(tid={name_conflict['tid']}, aid={name_conflict.get('aid')})"
                    ),
                )
            )
            continue

        thread_list.append(
            {
                "thread_name": match.thread_name,
                "tid": tid,
                "aid": aid,
                "description": match.description,
            }
        )
        outcomes.append(
            SyncOutcome(match=match, status="added", message="已添加配置")
        )

    return outcomes

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

import NGAClient
import utils


DEFAULT_TID = 43877379
DEFAULT_RULE_LOU = 21552
DEFAULT_THREAD_AUTHOR_UID = 62668270
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_OUTPUT = Path("anjia-viewer") / "public" / "data" / "anchors_43877379.json"
POSTS_PER_PAGE = 20
SCHEMA_VERSION = 3
DEFAULT_POST_CHAR_LIMIT = 7000
DEFAULT_REASONING_EFFORT = "high"

DEFAULT_TOPICS: list[dict[str, Any]] = [
    {
        "id": "theme1_travel",
        "name": "主题1：旅行",
        "short_name": "旅行",
        "allow_multiple_per_author": False,
        "description": "人物、地点、事件形式的暑假马车旅行安价。",
        "end_time": "2026-05-04 23:00",
    },
    {
        "id": "theme2_training",
        "name": "主题2：女仆修行 livehouse / 修行之道",
        "short_name": "修行之道",
        "allow_multiple_per_author": False,
        "description": "老师、主要属性培养、事件形式的女仆培训安价。",
        "end_time": "2026-05-04 23:00",
    },
    {
        "id": "theme2_guest",
        "name": "主题2：女仆修行 livehouse / 难缠之客",
        "short_name": "难缠之客",
        "allow_multiple_per_author": False,
        "description": "客人、事件形式的委托或招待测试安价。",
        "end_time": "2026-05-04 23:00",
    },
    {
        "id": "theme3_engagement",
        "name": "主题3：订婚大作战",
        "short_name": "订婚",
        "allow_multiple_per_author": False,
        "description": "人物、地点、事件形式的促成贴贴安价。",
        "end_time": "2026-05-04 23:00",
    },
    {
        "id": "theme4_carriage_name",
        "name": "主题4：马车的名字",
        "short_name": "马车名字",
        "allow_multiple_per_author": False,
        "description": "安价名字形式的马车命名。",
        "end_time": "2026-05-04 23:00",
    },
    {
        "id": "qa",
        "name": "特别环节：诡秘小祥一周年 Q&A",
        "short_name": "Q&A",
        "allow_multiple_per_author": True,
        "description": "向神奇小猪提问；同一作者可提交多个问题。",
    },
]

TOPIC_ALIASES = {
    "主题1": "theme1_travel",
    "主题一": "theme1_travel",
    "theme1": "theme1_travel",
    "travel": "theme1_travel",
    "旅行": "theme1_travel",
    "旅游": "theme1_travel",
    "主题2": "theme2_training",
    "主题二": "theme2_training",
    "theme2": "theme2_training",
    "女仆修行": "theme2_training",
    "修行之道": "theme2_training",
    "training": "theme2_training",
    "难缠之客": "theme2_guest",
    "难缠的客人": "theme2_guest",
    "难缠顾客": "theme2_guest",
    "不速之客": "theme2_guest",
    "guest": "theme2_guest",
    "客人": "theme2_guest",
    "主题3": "theme3_engagement",
    "主题三": "theme3_engagement",
    "theme3": "theme3_engagement",
    "订婚": "theme3_engagement",
    "订婚大作战": "theme3_engagement",
    "engagement": "theme3_engagement",
    "主题4": "theme4_carriage_name",
    "主题四": "theme4_carriage_name",
    "theme4": "theme4_carriage_name",
    "马车名字": "theme4_carriage_name",
    "马车名称": "theme4_carriage_name",
    "马车的名字": "theme4_carriage_name",
    "carriage_name": "theme4_carriage_name",
    "特别环节": "qa",
    "q&a": "qa",
    "qa": "qa",
    "问答": "qa",
}


class CounterFailure(Exception):
    def __init__(self, message: str, warnings: list[str] | None = None):
        super().__init__(message)
        self.warnings = warnings or []


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def page_for_lou(lou: int) -> int:
    return lou // POSTS_PER_PAGE + 1


def to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def to_float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def clamp_confidence(value: Any) -> float | None:
    parsed = to_float(value, None)
    if parsed is None:
        return None
    return max(0.0, min(1.0, parsed))


def parse_int_values(values: list[str]) -> list[int]:
    parsed: list[int] = []
    for raw_value in values:
        for part in raw_value.split(","):
            stripped = part.strip()
            if not stripped:
                continue
            parsed_value = to_int(stripped)
            if parsed_value is None:
                raise argparse.ArgumentTypeError(f"无法解析整数：{stripped}")
            parsed.append(parsed_value)
    return parsed


def normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        parsed_item = to_int(item)
        if parsed_item is not None:
            result.append(parsed_item)
    return sorted(set(result))


def clean_post_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""

    cleaned = strip_reply_quote_blocks(text)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</(?:p|div|li|tr|h[1-6])>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[img\].*?\[/img\]", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"\[/?(?:quote|b|i|u|s|del|color|size|align|url|collapse|fold|pid|tid)[^\]]*\]", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\[[^\]]+\]", "", cleaned)
    for _ in range(2):
        cleaned = unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def strip_reply_quote_blocks(text: str) -> str:
    return re.sub(
        r"\[quote\]\s*\[pid=[^\]]+\]Reply\[/pid\].*?\[/quote\]",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )


def count_reply_quote_blocks(text: Any) -> int:
    if not isinstance(text, str):
        return 0
    return len(
        re.findall(
            r"\[quote\]\s*\[pid=[^\]]+\]Reply\[/pid\].*?\[/quote\]",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
    )


def compact_text(text: str) -> str:
    compacted = re.sub(r"[ \t]+", " ", text.strip())
    compacted = re.sub(r"\n{3,}", "\n\n", compacted)
    return compacted.strip(" \n：:")


def truncate_text(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n...[已截断]"


def read_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file_obj:
        data = json.load(file_obj)
    if not isinstance(data, dict):
        raise ValueError(f"JSON root is not an object: {path}")
    return data


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file_obj:
        json.dump(payload, file_obj, ensure_ascii=False, indent=2)
        file_obj.write("\n")


def page_cache_path(cache_dir: Path, page: int) -> Path:
    return cache_dir / f"page_{page}.json"


def resolve_cache_dir(tid: int, configured_cache_dir: str | None) -> Path:
    if configured_cache_dir:
        cache_dir = Path(configured_cache_dir)
    else:
        cache_dir = Path(utils.get_folder(tid, None, "json"))
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def ensure_page(
    client: NGAClient.NGAClient,
    tid: int,
    page: int,
    cache_dir: Path,
    force_refresh: bool,
    warnings: list[str],
) -> dict[str, Any]:
    cache_path = page_cache_path(cache_dir, page)
    if cache_path.exists() and not force_refresh:
        try:
            return read_json_file(cache_path)
        except (json.JSONDecodeError, ValueError) as exc:
            warnings.append(f"缓存页 {cache_path} 无法读取，已重新抓取：{exc}")

    print(f"正在抓取 tid={tid} 第 {page} 页...")
    page_data = client.get_page(tid, None, page)
    write_json_file(cache_path, page_data)
    return page_data


def flatten_posts(page_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    posts: list[dict[str, Any]] = []
    for page_payload in page_payloads:
        page_posts = page_payload.get("result", [])
        if isinstance(page_posts, list):
            posts.extend(post for post in page_posts if isinstance(post, dict))
    posts.sort(key=lambda post: int(post.get("lou", 0)))
    return posts


def load_page_range(
    client: NGAClient.NGAClient,
    tid: int,
    start_page: int,
    end_page: int,
    cache_dir: Path,
    force_refresh: bool,
    warnings: list[str],
    loaded_pages: dict[int, dict[str, Any]],
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for page in range(start_page, end_page + 1):
        if page in loaded_pages and not force_refresh:
            payload = loaded_pages[page]
        else:
            payload = ensure_page(client, tid, page, cache_dir, force_refresh, warnings)
            loaded_pages[page] = payload
        payloads.append(payload)
    return payloads


def get_author(post: dict[str, Any]) -> dict[str, Any]:
    author = post.get("author", {})
    if not isinstance(author, dict):
        author = {}
    uid = to_int(author.get("uid"))
    return {
        "uid": uid if uid is not None else author.get("uid"),
        "username": str(author.get("username", "")),
    }


def author_key(author: dict[str, Any], fallback_lou: int | None = None) -> str:
    uid = to_int(author.get("uid"))
    if uid is not None:
        return f"uid:{uid}"
    username = str(author.get("username") or "").strip()
    if username:
        return f"name:{username}"
    return f"unknown:{fallback_lou if fallback_lou is not None else 'none'}"


def get_attachment_summary(post: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = post.get("attches", [])
    if not isinstance(attachments, list):
        return []
    result: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        result.append(
            {
                "type": attachment.get("type"),
                "attachurl": attachment.get("attachurl"),
                "name": attachment.get("url_utf8_org_name"),
            }
        )
    return result


def post_record(post: dict[str, Any]) -> dict[str, Any]:
    original_content = post.get("content", "")
    content = clean_post_text(original_content)
    removed_reply_quotes = count_reply_quote_blocks(original_content)
    lou = to_int(post.get("lou"))
    author = get_author(post)
    return {
        "lou": lou,
        "pid": post.get("pid"),
        "postdate": post.get("postdate"),
        "author": author,
        "author_key": author_key(author, lou),
        "content": content,
        "original_content": original_content if isinstance(original_content, str) else "",
        "removed_reply_quotes": removed_reply_quotes,
        "attachments": get_attachment_summary(post),
    }


def find_post_by_lou(posts: list[dict[str, Any]], lou: int) -> dict[str, Any] | None:
    for post in posts:
        if to_int(post.get("lou")) == lou:
            return post
    return None


def default_topics() -> list[dict[str, Any]]:
    return [dict(topic) for topic in DEFAULT_TOPICS]


def normalize_topic_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unclassified"
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    normalized = normalized.replace("q＆a", "q&a")
    return TOPIC_ALIASES.get(normalized, TOPIC_ALIASES.get(value.strip(), normalized))


def topic_map(parsed_rule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    topics = parsed_rule.get("topics")
    if not isinstance(topics, list) or not topics:
        topics = default_topics()
    result: dict[str, dict[str, Any]] = {}
    for topic in topics:
        if not isinstance(topic, dict):
            continue
        topic_id = normalize_topic_id(topic.get("id") or topic.get("name"))
        if topic_id == "unclassified":
            continue
        normalized = dict(topic)
        normalized["id"] = topic_id
        normalized.setdefault("name", topic_id)
        normalized.setdefault("short_name", normalized["name"])
        normalized.setdefault("allow_multiple_per_author", topic_id == "qa")
        result[topic_id] = normalized
    for topic in DEFAULT_TOPICS:
        result.setdefault(topic["id"], dict(topic))
    return result


def topic_names(topic_id: str, parsed_rule: dict[str, Any]) -> tuple[str, str]:
    if topic_id == "unclassified":
        return "未分类", "未分类"
    topics = topic_map(parsed_rule)
    topic = topics.get(topic_id, {"name": topic_id, "short_name": topic_id})
    return str(topic.get("name", topic_id)), str(topic.get("short_name", topic.get("name", topic_id)))


def known_rule(rule_lou: int, rule_post: dict[str, Any]) -> dict[str, Any]:
    return {
        "start_lou": rule_lou,
        "end_lou": None,
        "end_time": None,
        "ignore_author_user": [DEFAULT_THREAD_AUTHOR_UID],
        "not_anjia_lou_list": [],
        "keyword": None,
        "topics": default_topics(),
        "classification_rules": [
            "一条回复可以同时包含多个主题安价，必须拆成多个 entries。",
            "观众可以只安价部分主题，缺失主题不是错误。",
            "每个作者每个普通主题最终只保留 1 个；如果同作者同主题多次提交，按最新楼层作为最终提交。",
            "同作者不同主题可以散落在多个回复中，不能只看单楼。",
            "主题2的修行之道和难缠之客视为两个独立普通主题。",
            "Q&A 特别环节允许同一作者提交多个问题，每个问题拆成一个 qa entry。",
            "不要把普通安价事件中的疑问句误判为 Q&A；只有明显向神奇小猪、作者或 Q&A 环节提问的内容才归为 qa。",
        ],
        "confidence": 1.0,
        "warnings": [],
        "source": "known_rule_21552",
        "rule_postdate": rule_post.get("postdate"),
    }


def apply_manual_overrides(
    parsed_rule: dict[str, Any],
    args: argparse.Namespace,
    warnings: list[str],
) -> dict[str, Any]:
    if args.start_lou is not None:
        parsed_rule["start_lou"] = args.start_lou
        warnings.append(f"使用命令行覆盖 start_lou={args.start_lou}")
    if args.end_lou is not None:
        parsed_rule["end_lou"] = args.end_lou
        warnings.append(f"使用命令行覆盖 end_lou={args.end_lou}")
    if args.end_time is not None:
        parsed_rule["end_time"] = args.end_time
        warnings.append(f"使用命令行覆盖 end_time={args.end_time}")
    if args.keyword is not None:
        parsed_rule["keyword"] = args.keyword
        warnings.append(f"使用命令行覆盖 keyword={args.keyword}")

    ignored_users = set(normalize_int_list(parsed_rule.get("ignore_author_user")))
    ignored_users.add(DEFAULT_THREAD_AUTHOR_UID)
    ignored_users.update(args.ignore_user_values)
    parsed_rule["ignore_author_user"] = sorted(ignored_users)

    ignored_lous = set(normalize_int_list(parsed_rule.get("not_anjia_lou_list")))
    ignored_lous.update(args.ignore_lou_values)
    parsed_rule["not_anjia_lou_list"] = sorted(ignored_lous)
    return parsed_rule


def build_candidates(
    posts: list[dict[str, Any]],
    parsed_rule: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    start_lou = int(parsed_rule["start_lou"])
    end_lou = parsed_rule.get("end_lou")
    end_time = parsed_rule.get("end_time")
    keyword = parsed_rule.get("keyword")
    ignored_users = set(normalize_int_list(parsed_rule.get("ignore_author_user")))
    ignored_lous = set(normalize_int_list(parsed_rule.get("not_anjia_lou_list")))

    candidates: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []
    for post in posts:
        record = post_record(post)
        lou = to_int(record.get("lou"))
        author_uid = to_int(record.get("author", {}).get("uid"))
        reasons: list[str] = []

        if lou is None:
            reasons.append("楼层号缺失或无法解析")
        else:
            if lou <= start_lou:
                reasons.append(f"楼层 {lou} 小于等于起始楼层 {start_lou}")
            if end_lou is not None and lou > int(end_lou):
                reasons.append(f"楼层 {lou} 大于截止楼层 {end_lou}")
            if lou in ignored_lous:
                reasons.append(f"楼层 {lou} 在忽略楼层名单中")

        if author_uid is not None and author_uid in ignored_users:
            reasons.append(f"作者 uid={author_uid} 在忽略名单中")
        if isinstance(end_time, str) and record.get("postdate") and str(record["postdate"]) > end_time:
            reasons.append(f"时间 {record['postdate']} 超过截止时间 {end_time}")
        if isinstance(keyword, str) and keyword and keyword not in record["content"] and keyword not in record["original_content"]:
            reasons.append(f"内容不包含关键词 {keyword}")
        if not str(record.get("content", "")).strip():
            reasons.append("清洗后内容为空")

        if reasons:
            ignored.append({**record, "ignore_reason": "；".join(reasons), "stage": "deterministic"})
        else:
            candidates.append(record)
    return candidates, ignored


def build_author_packs(candidates: list[dict[str, Any]], post_char_limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for candidate in candidates:
        key = str(candidate.get("author_key") or author_key(candidate.get("author", {}), to_int(candidate.get("lou"))))
        if key not in grouped:
            grouped[key] = {
                "author_key": key,
                "author": candidate.get("author", {}),
                "posts": [],
            }
            order.append(key)
        content = str(candidate.get("content", ""))
        grouped[key]["posts"].append(
            {
                "lou": candidate.get("lou"),
                "pid": candidate.get("pid"),
                "postdate": candidate.get("postdate"),
                "content": truncate_text(content, post_char_limit),
                "content_truncated": post_char_limit > 0 and len(content) > post_char_limit,
            }
        )

    packs = [grouped[key] for key in order]
    for pack in packs:
        pack["posts"].sort(key=lambda post: int(post.get("lou") or 0))
    packs.sort(key=lambda pack: min(int(post.get("lou") or 0) for post in pack["posts"]))
    return packs


def load_model_endpoint(model_name: str) -> tuple[str, str]:
    config_path = Path(__file__).resolve().with_name("config.json")
    if not config_path.exists():
        raise CounterFailure(f"找不到模型配置文件：{config_path}")
    config_data = read_json_file(config_path)
    providers = config_data.get("providers")
    if providers is None and "model_names" in config_data:
        providers = [config_data]
    if not isinstance(providers, list):
        raise CounterFailure("config.json 中缺少 providers 列表")

    for provider in providers:
        if not isinstance(provider, dict):
            continue
        model_names = provider.get("model_names", [])
        if isinstance(model_names, list) and model_name in model_names:
            base_url = provider.get("base_url")
            api_key = provider.get("api_key", "")
            if not isinstance(base_url, str) or not base_url:
                raise CounterFailure(f"模型 {model_name} 缺少 base_url")
            if not isinstance(api_key, str):
                api_key = ""
            return base_url, api_key
    raise CounterFailure(f"config.json 中找不到模型：{model_name}")


class DirectChatAgent:
    def __init__(self, model_name: str, reasoning_effort: str):
        from openai import OpenAI

        self.model_name = model_name
        self.reasoning_effort = reasoning_effort
        base_url, api_key = load_model_endpoint(model_name)
        self.thinking_enabled = "deepseek" in base_url.lower()
        self.client = OpenAI(base_url=base_url, api_key=api_key, max_retries=0, timeout=180)

    def chat(self, message: str) -> str:
        request: dict[str, Any] = {
            "model": self.model_name,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": 8192,
            "timeout": 180,
        }
        if self.thinking_enabled:
            request["reasoning_effort"] = self.reasoning_effort
            request["extra_body"] = {"thinking": {"type": "enabled"}}
            request["response_format"] = {"type": "json_object"}
        response = self.client.chat.completions.create(
            **request,
        )
        content = response.choices[0].message.content
        if content is None:
            raise CounterFailure("模型返回空内容")
        return content


def extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?", "", stripped, flags=re.IGNORECASE).strip()
        stripped = re.sub(r"```$", "", stripped).strip()
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        start_index = stripped.find("{")
        end_index = stripped.rfind("}")
        if start_index < 0 or end_index <= start_index:
            raise
        payload = json.loads(stripped[start_index : end_index + 1])
    if not isinstance(payload, dict):
        raise ValueError("模型返回的 JSON 根节点不是对象")
    return payload


def ask_agent_for_json(agent: Any, prompt: str, max_retries: int) -> dict[str, Any]:
    current_prompt = prompt
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        response = agent.chat(current_prompt)
        try:
            return extract_json_object(response)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            if attempt >= max_retries:
                break
            print(f"模型 JSON 解析失败，正在重试 {attempt + 1}/{max_retries}...")
            current_prompt = (
                "上一次回复不是可解析的 JSON。请只返回一个 JSON 对象，不要 Markdown，不要解释。\n"
                f"解析错误：{exc}\n\n原任务：\n{prompt}"
            )
    raise CounterFailure(f"模型连续返回不可解析 JSON：{last_error}")


def build_author_prompt(parsed_rule: dict[str, Any], author_pack: dict[str, Any]) -> str:
    prompt_schema = {
        "items": [
            {
                "topic_id": "theme1_travel | theme2_training | theme2_guest | theme3_engagement | theme4_carriage_name | qa | unclassified",
                "normalized_anchor_text": "只属于这个条目的安价正文，不要混入其他主题",
                "fields": {"人物/地点/事件/老师/属性/客人/名字/问题": "按主题提取；没有则省略"},
                "source_lous": [21555],
                "chosen_lou": 21555,
                "confidence": 0.0,
                "needs_manual_review": False,
                "note": "可选说明",
            }
        ],
        "ignored_posts": [
            {
                "lou": 21555,
                "source_lous": [21555],
                "ignore_reason": "聊天/非安价/同主题较新楼覆盖/其他原因",
                "topic_id": "可选",
                "superseded_by_lou": "可选整数",
            }
        ],
        "warnings": ["可选"],
    }
    model_payload = {"posts": author_pack.get("posts", [])}
    return f"""
你是 NGA 安价统计员。请按 21552 楼规则判断这个观众的候选楼。

重要规则：
- 这个观众的所有未忽略候选楼已经一次性给出，必须整体判断。
- 观众可能只安价部分主题；没有安价的主题不要补，不要猜。
- 不同主题可以分散在多个回复中，应该分别保留。
- 普通主题包括 theme1_travel、theme2_training、theme2_guest、theme3_engagement、theme4_carriage_name；每个普通主题最终只保留 1 条。
- 如果同一普通主题多次提交，只在 items 中返回楼层号最新的那条；旧楼写入 ignored_posts，原因说明被哪个楼覆盖。
- qa 是特别 Q&A，可以有多个问题，不按“每主题 1 条”去重。
- 不要把普通安价事件里的疑问句当成 qa。只有明显是在 Q&A/提问上下文，或向神奇小猪/作者/猪导提问，才归为 qa。
- 如果某楼只是聊天、顶帖、修改说明、无效内容，写入 ignored_posts。
- 如果确实像安价但主题或字段无法确定，使用 topic_id="unclassified"，并设置 needs_manual_review=true。
- 不要返回作者 uid、作者名、author_key 或其他作者标识；脚本会在本地关联来源。

主题定义：
{json.dumps(parsed_rule.get("topics", default_topics()), ensure_ascii=False)}

必须只返回 JSON 对象，不要 Markdown。格式：
{json.dumps(prompt_schema, ensure_ascii=False, indent=2)}

待判断观众候选楼：
{json.dumps(model_payload, ensure_ascii=False, indent=2)}
""".strip()


def post_source_payload(post: dict[str, Any]) -> dict[str, Any]:
    return {
        "lou": post.get("lou"),
        "pid": post.get("pid"),
        "postdate": post.get("postdate"),
        "author": post.get("author"),
        "content": post.get("content", ""),
        "original_content": post.get("original_content", ""),
        "removed_reply_quotes": post.get("removed_reply_quotes", 0),
        "attachments": post.get("attachments", []),
    }


def parse_lou_list(value: Any) -> list[int]:
    if isinstance(value, list):
        raw_values = value
    elif value is None:
        raw_values = []
    else:
        raw_values = [value]
    result: list[int] = []
    for raw in raw_values:
        parsed = to_int(raw)
        if parsed is not None:
            result.append(parsed)
    return sorted(set(result))


def ignored_from_post(post: dict[str, Any], reason: str, stage: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = {
        **post_source_payload(post),
        "author_key": post.get("author_key"),
        "ignore_reason": reason,
        "stage": stage,
    }
    if extra:
        payload.update(extra)
    return payload


def normalize_llm_ignored_item(
    item: dict[str, Any],
    post_by_lou: dict[int, dict[str, Any]],
    author_key_value: str,
) -> list[dict[str, Any]]:
    lous = parse_lou_list(item.get("source_lous"))
    item_lou = to_int(item.get("lou"))
    if item_lou is not None:
        lous.append(item_lou)
    lous = sorted(set(lou for lou in lous if lou in post_by_lou))
    reason = item.get("ignore_reason")
    reason_text = reason if isinstance(reason, str) and reason.strip() else "模型判定不是有效安价"
    extra = {
        "topic_id": normalize_topic_id(item.get("topic_id")) if item.get("topic_id") else None,
        "source_lous": lous,
        "superseded_by_lou": to_int(item.get("superseded_by_lou")),
    }
    result: list[dict[str, Any]] = []
    for lou in lous:
        result.append(ignored_from_post(post_by_lou[lou], reason_text, "llm_author_pack", extra))
    if not result:
        result.append(
            {
                "lou": item_lou,
                "author_key": author_key_value,
                "ignore_reason": reason_text,
                "stage": "llm_author_pack",
                **extra,
            }
        )
    return result


def normalize_author_result(
    raw_author: dict[str, Any],
    author_pack: dict[str, Any],
    candidates_by_lou: dict[int, dict[str, Any]],
    parsed_rule: dict[str, Any],
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    key = str(author_pack["author_key"])
    pack_lous = [int(post["lou"]) for post in author_pack["posts"] if to_int(post.get("lou")) is not None]
    post_by_lou = {lou: candidates_by_lou[lou] for lou in pack_lous if lou in candidates_by_lou}
    proposals: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    raw_items = raw_author.get("items", [])
    if not isinstance(raw_items, list):
        raw_items = []
        warnings.append(f"{key} 的模型返回 items 不是数组，已忽略该字段")

    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        topic_id = normalize_topic_id(raw_item.get("topic_id") or raw_item.get("topic") or raw_item.get("topic_name"))
        source_lous = parse_lou_list(raw_item.get("source_lous"))
        chosen_lou = to_int(raw_item.get("chosen_lou"))
        legacy_lou = to_int(raw_item.get("lou"))
        if chosen_lou is None:
            chosen_lou = legacy_lou
        if chosen_lou is not None:
            source_lous.append(chosen_lou)
        source_lous = sorted(set(lou for lou in source_lous if lou in post_by_lou))
        if not source_lous:
            fallback_lou = max(pack_lous) if pack_lous else None
            if fallback_lou is None or fallback_lou not in post_by_lou:
                warnings.append(f"{key} 有条目缺少有效来源楼，已丢弃")
                continue
            source_lous = [fallback_lou]
            chosen_lou = fallback_lou
            raw_item["needs_manual_review"] = True
        if chosen_lou is None or chosen_lou not in source_lous:
            chosen_lou = max(source_lous)

        chosen_post = post_by_lou[chosen_lou]
        fields = raw_item.get("fields", {})
        if not isinstance(fields, dict):
            fields = {}
        note = raw_item.get("note") or raw_item.get("classification_note")
        normalized_text = raw_item.get("normalized_anchor_text") or raw_item.get("content") or raw_item.get("text")
        if not isinstance(normalized_text, str) or not normalized_text.strip():
            normalized_text = chosen_post.get("content", "")
        topic_name, topic_short_name = topic_names(topic_id, parsed_rule)

        proposals.append(
            {
                "author_key": key,
                "author": chosen_post.get("author", author_pack.get("author", {})),
                "topic_id": topic_id,
                "topic_name": topic_name,
                "topic_short_name": topic_short_name,
                "subtopic_name": raw_item.get("subtopic_name") if isinstance(raw_item.get("subtopic_name"), str) else None,
                "chosen_lou": chosen_lou,
                "source_lous": source_lous,
                "source_posts": [post_source_payload(post_by_lou[lou]) for lou in source_lous],
                "normalized_anchor_text": compact_text(normalized_text),
                "fields": fields,
                "confidence": clamp_confidence(raw_item.get("confidence")),
                "needs_manual_review": bool(raw_item.get("needs_manual_review", False) or topic_id == "unclassified"),
                "classification_source": "llm_author_pack",
                "classification_note": note if isinstance(note, str) else None,
            }
        )

    raw_ignored = raw_author.get("ignored_posts", [])
    if not isinstance(raw_ignored, list):
        raw_ignored = []
        warnings.append(f"{key} 的模型返回 ignored_posts 不是数组，已忽略该字段")
    seen_ignored_lous: set[int] = set()
    for raw_item in raw_ignored:
        if not isinstance(raw_item, dict):
            continue
        normalized_ignored = normalize_llm_ignored_item(raw_item, post_by_lou, key)
        ignored.extend(normalized_ignored)
        for ignored_item in normalized_ignored:
            ignored_lou = to_int(ignored_item.get("lou"))
            if ignored_lou is not None:
                seen_ignored_lous.add(ignored_lou)

    used_lous = {lou for proposal in proposals for lou in proposal.get("source_lous", [])}
    for lou in sorted(set(pack_lous) - used_lous - seen_ignored_lous):
        ignored.append(ignored_from_post(post_by_lou[lou], "模型未将该楼归入任何有效条目", "llm_author_pack"))

    raw_warnings = raw_author.get("warnings", [])
    if isinstance(raw_warnings, list):
        warnings.extend(str(warning) for warning in raw_warnings if str(warning).strip())
    return proposals, ignored


def manual_review_for_pack(author_pack: dict[str, Any], candidates_by_lou: dict[int, dict[str, Any]], reason: str) -> list[dict[str, Any]]:
    proposals: list[dict[str, Any]] = []
    key = str(author_pack["author_key"])
    for pack_post in author_pack.get("posts", []):
        lou = to_int(pack_post.get("lou"))
        if lou is None or lou not in candidates_by_lou:
            continue
        post = candidates_by_lou[lou]
        proposals.append(
            {
                "author_key": key,
                "author": post.get("author", {}),
                "topic_id": "unclassified",
                "topic_name": "未分类",
                "topic_short_name": "未分类",
                "subtopic_name": None,
                "chosen_lou": lou,
                "source_lous": [lou],
                "source_posts": [post_source_payload(post)],
                "normalized_anchor_text": post.get("content", ""),
                "fields": {},
                "confidence": None,
                "needs_manual_review": True,
                "classification_source": "manual_review_fallback",
                "classification_note": reason,
            }
        )
    return proposals


def classify_author_packs_with_llm(
    agent: Any,
    parsed_rule: dict[str, Any],
    candidates: list[dict[str, Any]],
    post_char_limit: int,
    max_retries: int,
    warnings: list[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates_by_lou = {int(candidate["lou"]): candidate for candidate in candidates if to_int(candidate.get("lou")) is not None}
    author_packs = build_author_packs(candidates, post_char_limit)
    proposals: list[dict[str, Any]] = []
    ignored: list[dict[str, Any]] = []

    for index, pack in enumerate(author_packs, start=1):
        batch_lous = [
            int(post["lou"])
            for post in pack.get("posts", [])
            if to_int(post.get("lou")) is not None
        ]
        author = pack.get("author", {})
        username = author.get("username") if isinstance(author, dict) else None
        uid = author.get("uid") if isinstance(author, dict) else None
        print(
            f"正在判定作者 {index}/{len(author_packs)}，"
            f"{username or '未知作者'}({uid if uid is not None else 'unknown'})，"
            f"楼层 {', '.join(str(lou) for lou in batch_lous)}..."
        )
        prompt = build_author_prompt(parsed_rule, pack)
        try:
            response_json = ask_agent_for_json(agent, prompt, max_retries)
            raw_author = response_json
            legacy_authors = response_json.get("authors")
            if isinstance(legacy_authors, list) and legacy_authors and isinstance(legacy_authors[0], dict):
                raw_author = legacy_authors[0]
            normalized_proposals, normalized_ignored = normalize_author_result(
                raw_author,
                pack,
                candidates_by_lou,
                parsed_rule,
                warnings,
            )
            proposals.extend(normalized_proposals)
            ignored.extend(normalized_ignored)
        except Exception as exc:
            reason = f"模型判定作者批次失败：{exc}"
            warnings.append(f"{reason}；作者={pack.get('author_key')}")
            proposals.extend(manual_review_for_pack(pack, candidates_by_lou, reason))
    return proposals, ignored


def classify_author_packs_without_llm(
    candidates: list[dict[str, Any]],
    post_char_limit: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates_by_lou = {int(candidate["lou"]): candidate for candidate in candidates if to_int(candidate.get("lou")) is not None}
    proposals: list[dict[str, Any]] = []
    for pack in build_author_packs(candidates, post_char_limit):
        proposals.extend(manual_review_for_pack(pack, candidates_by_lou, "--no-llm：未调用模型，候选楼需人工复核"))
    return proposals, []


def is_topic_multi_allowed(topic_id: str, parsed_rule: dict[str, Any]) -> bool:
    if topic_id in {"qa", "unclassified"}:
        return True
    topics = topic_map(parsed_rule)
    return bool(topics.get(topic_id, {}).get("allow_multiple_per_author", False))


def ignored_from_superseded(entry: dict[str, Any], winner: dict[str, Any]) -> list[dict[str, Any]]:
    reason = f"同一作者同一主题已有较新提交 #{winner['chosen_lou']}，按规则取最新楼"
    ignored: list[dict[str, Any]] = []
    for source_post in entry.get("source_posts", []):
        post = dict(source_post)
        post["author_key"] = entry.get("author_key")
        ignored.append(
            ignored_from_post(
                post,
                reason,
                "superseded",
                {
                    "topic_id": entry.get("topic_id"),
                    "topic_name": entry.get("topic_name"),
                    "source_lous": entry.get("source_lous", []),
                    "superseded_by_lou": winner.get("chosen_lou"),
                    "confidence": entry.get("confidence"),
                },
            )
        )
    return ignored


def enforce_latest_policy(
    proposals: list[dict[str, Any]],
    parsed_rule: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    keep_direct: list[dict[str, Any]] = []
    for proposal in proposals:
        topic_id = str(proposal.get("topic_id", "unclassified"))
        if is_topic_multi_allowed(topic_id, parsed_rule):
            keep_direct.append(proposal)
            continue
        key = (str(proposal.get("author_key")), topic_id)
        grouped.setdefault(key, []).append(proposal)

    kept: list[dict[str, Any]] = [*keep_direct]
    ignored: list[dict[str, Any]] = []
    for group_entries in grouped.values():
        if len(group_entries) == 1:
            kept.append(group_entries[0])
            continue
        group_entries.sort(key=lambda entry: int(entry.get("chosen_lou") or 0))
        winner = group_entries[-1]
        superseded_lous: list[int] = []
        for superseded in group_entries[:-1]:
            superseded_lous.extend(parse_lou_list(superseded.get("source_lous")))
            ignored.extend(ignored_from_superseded(superseded, winner))
        winner["superseded_lous"] = sorted(set(superseded_lous))
        kept.append(winner)
    return kept, ignored


def finalize_entries(proposals: list[dict[str, Any]], parsed_rule: dict[str, Any]) -> list[dict[str, Any]]:
    order = {topic["id"]: index for index, topic in enumerate(DEFAULT_TOPICS)}
    entries: list[dict[str, Any]] = []
    for proposal in proposals:
        chosen_lou = int(proposal.get("chosen_lou") or 0)
        source_posts = proposal.get("source_posts", [])
        if not isinstance(source_posts, list):
            source_posts = []
        chosen_post = next((post for post in source_posts if to_int(post.get("lou")) == chosen_lou), source_posts[0] if source_posts else {})
        topic_id = str(proposal.get("topic_id", "unclassified"))
        topic_name, topic_short_name = topic_names(topic_id, parsed_rule)
        entries.append(
            {
                "id": 0,
                "topic_id": topic_id,
                "topic_name": topic_name,
                "topic_short_name": topic_short_name,
                "subtopic_name": proposal.get("subtopic_name"),
                "author": proposal.get("author", chosen_post.get("author", {})),
                "author_key": proposal.get("author_key"),
                "lou": chosen_lou,
                "pid": chosen_post.get("pid"),
                "postdate": chosen_post.get("postdate"),
                "content": proposal.get("normalized_anchor_text") or chosen_post.get("content", ""),
                "fields": proposal.get("fields", {}),
                "raw_clean_content": chosen_post.get("content", ""),
                "original_content": chosen_post.get("original_content", ""),
                "attachments": chosen_post.get("attachments", []),
                "source_lous": sorted(set(parse_lou_list(proposal.get("source_lous")))),
                "source_posts": source_posts,
                "superseded_lous": sorted(set(parse_lou_list(proposal.get("superseded_lous")))),
                "confidence": proposal.get("confidence"),
                "needs_manual_review": bool(proposal.get("needs_manual_review", False)),
                "classification_source": proposal.get("classification_source"),
                "classification_note": proposal.get("classification_note"),
                "has_duplicate": False,
                "duplicate_lous": [],
                "duplicate_entry_ids": [],
            }
        )

    entries.sort(key=lambda entry: (order.get(str(entry.get("topic_id")), 999), int(entry.get("lou") or 0), str(entry.get("author_key"))))
    for index, entry in enumerate(entries, start=1):
        entry["id"] = index
    return entries


def build_anchors(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for entry in entries:
        key = str(entry.get("author_key") or author_key(entry.get("author", {}), to_int(entry.get("lou"))))
        if key not in grouped:
            grouped[key] = {
                "author": entry.get("author", {}),
                "posts": {},
                "entries": [],
            }
        grouped[key]["entries"].append(entry)
        for source_post in entry.get("source_posts", []):
            lou = to_int(source_post.get("lou"))
            if lou is not None:
                grouped[key]["posts"][lou] = source_post

    anchors: list[dict[str, Any]] = []
    for group in grouped.values():
        posts = [group["posts"][lou] for lou in sorted(group["posts"])]
        group_entries = sorted(group["entries"], key=lambda item: int(item.get("id") or 0))
        if not posts:
            posts = [
                {
                    "lou": entry.get("lou"),
                    "pid": entry.get("pid"),
                    "postdate": entry.get("postdate"),
                    "author": entry.get("author"),
                    "content": entry.get("raw_clean_content") or entry.get("content", ""),
                    "original_content": entry.get("original_content", ""),
                    "attachments": entry.get("attachments", []),
                }
                for entry in group_entries
            ]
        numeric_confidences = [entry["confidence"] for entry in group_entries if isinstance(entry.get("confidence"), (int, float))]
        anchors.append(
            {
                "id": 0,
                "author": group.get("author", {}),
                "posts": posts,
                "entries": group_entries,
                "first_lou": min(int(post.get("lou") or 0) for post in posts),
                "first_postdate": posts[0].get("postdate") if posts else None,
                "has_duplicate": any(bool(entry.get("has_duplicate")) for entry in group_entries),
                "duplicate_lous": sorted({lou for entry in group_entries for lou in parse_lou_list(entry.get("duplicate_lous"))}),
                "topic_ids": sorted({str(entry.get("topic_id")) for entry in group_entries}),
                "confidence": round(sum(numeric_confidences) / len(numeric_confidences), 4) if numeric_confidences else None,
                "needs_manual_review": any(bool(entry.get("needs_manual_review")) for entry in group_entries),
                "source_lous": sorted({lou for entry in group_entries for lou in parse_lou_list(entry.get("source_lous"))}),
            }
        )
    anchors.sort(key=lambda anchor: int(anchor.get("first_lou") or 0))
    for index, anchor in enumerate(anchors, start=1):
        anchor["id"] = index
    return anchors


def compress_int_ranges(values: list[int]) -> list[dict[str, int]]:
    if not values:
        return []
    sorted_values = sorted(set(values))
    ranges: list[dict[str, int]] = []
    range_start = sorted_values[0]
    previous = sorted_values[0]
    for current in sorted_values[1:]:
        if current == previous + 1:
            previous = current
            continue
        ranges.append({"start": range_start, "end": previous, "count": previous - range_start + 1})
        range_start = current
        previous = current
    ranges.append({"start": range_start, "end": previous, "count": previous - range_start + 1})
    return ranges


def build_raw_stats(posts: list[dict[str, Any]], parsed_rule: dict[str, Any]) -> dict[str, Any]:
    lous = sorted(lou for lou in (to_int(post.get("lou")) for post in posts) if lou is not None)
    if not lous:
        return {"loaded_post_count": 0, "min_lou": None, "max_lou": None, "missing_lou_count": 0, "missing_lou_ranges": []}
    start_lou = int(parsed_rule["start_lou"])
    end_lou = parsed_rule.get("end_lou")
    upper_lou = int(end_lou) if end_lou is not None else max(lous)
    present = set(lous)
    missing = [lou for lou in range(start_lou + 1, upper_lou + 1) if lou not in present]
    return {
        "loaded_post_count": len(posts),
        "min_lou": min(lous),
        "max_lou": max(lous),
        "missing_lou_count": len(missing),
        "missing_lou_ranges": compress_int_ranges(missing),
    }


def failure_payload(args: argparse.Namespace, message: str, warnings: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "status": "failed",
            "tid": int(args.tid),
            "rule_lou": int(args.rule_lou),
            "generated_at": now_iso(),
            "model": None if args.no_llm else args.model,
            "llm_used": False,
            "manual_review_required": True,
        },
        "rule_post": None,
        "parsed_rule": None,
        "topics": default_topics(),
        "entries": [],
        "anchors": [],
        "ignored": [],
        "warnings": [*warnings, message],
        "raw_stats": {},
    }


def run_counter(args: argparse.Namespace) -> dict[str, Any]:
    warnings: list[str] = []
    tid = int(args.tid)
    rule_lou = int(args.rule_lou)
    cache_dir = resolve_cache_dir(tid, args.cache_dir)
    client = NGAClient.NGAClient()
    loaded_pages: dict[int, dict[str, Any]] = {}

    first_page = ensure_page(client, tid, 1, cache_dir, args.force_refresh, warnings)
    loaded_pages[1] = first_page
    total_pages = int(first_page.get("totalPage", 1))
    rule_page = page_for_lou(rule_lou)
    if rule_page > total_pages:
        raise CounterFailure(f"当前全帖共 {total_pages} 页，规则楼 {rule_lou} 预计在第 {rule_page} 页，线上数据不足。", warnings)

    rule_pages = load_page_range(client, tid, rule_page, rule_page, cache_dir, args.force_refresh, warnings, loaded_pages)
    rule_posts = flatten_posts(rule_pages)
    rule_post = find_post_by_lou(rule_posts, rule_lou)
    if rule_post is None:
        raise CounterFailure(f"已抓取规则楼页面，但没有找到 {rule_lou} 楼。可能被吞楼或权限不足。", warnings)

    rule_post_payload = post_record(rule_post)
    parsed_rule = apply_manual_overrides(known_rule(rule_lou, rule_post_payload), args, warnings)
    start_page = page_for_lou(int(parsed_rule["start_lou"]))
    end_lou = parsed_rule.get("end_lou")
    end_page = page_for_lou(int(end_lou)) if end_lou is not None else total_pages
    end_page = max(start_page, min(end_page, total_pages))

    page_payloads = load_page_range(client, tid, start_page, end_page, cache_dir, args.force_refresh, warnings, loaded_pages)
    posts = flatten_posts(page_payloads)
    candidates, ignored = build_candidates(posts, parsed_rule)

    if args.no_llm:
        warnings.append("已启用 --no-llm，候选楼全部标记人工复核。")
        proposals, model_ignored = classify_author_packs_without_llm(candidates, args.post_char_limit)
    else:
        agent = DirectChatAgent(args.model, args.reasoning_effort)
        proposals, model_ignored = classify_author_packs_with_llm(
            agent,
            parsed_rule,
            candidates,
            args.post_char_limit,
            args.max_retries,
            warnings,
        )
    ignored.extend(model_ignored)

    kept_proposals, superseded_ignored = enforce_latest_policy(proposals, parsed_rule)
    ignored.extend(superseded_ignored)
    entries = finalize_entries(kept_proposals, parsed_rule)
    anchors = build_anchors(entries)
    raw_stats = build_raw_stats(posts, parsed_rule)

    topic_counts: dict[str, int] = {}
    for entry in entries:
        topic_id = str(entry.get("topic_id", "unclassified"))
        topic_counts[topic_id] = topic_counts.get(topic_id, 0) + 1

    manual_review_required = bool(
        args.no_llm
        or warnings
        or parsed_rule.get("warnings")
        or any(entry.get("needs_manual_review") for entry in entries)
    )
    superseded_count = sum(1 for item in ignored if item.get("stage") == "superseded" or item.get("superseded_by_lou"))

    payload = {
        "schema_version": SCHEMA_VERSION,
        "meta": {
            "status": "success",
            "tid": tid,
            "rule_lou": rule_lou,
            "generated_at": now_iso(),
            "model": None if args.no_llm else args.model,
            "llm_used": not args.no_llm,
            "manual_review_required": manual_review_required,
            "cache_dir": str(cache_dir),
            "source_page_range": {"start": start_page, "end": end_page},
            "total_pages": total_pages,
            "candidate_count": len(candidates),
            "entry_count": len(entries),
            "anchor_count": len(entries),
            "author_count": len(anchors),
            "ignored_count": len(ignored),
            "duplicate_entry_count": 0,
            "duplicate_author_count": 0,
            "superseded_count": superseded_count,
            "topic_counts": topic_counts,
        },
        "rule_post": rule_post_payload,
        "parsed_rule": parsed_rule,
        "topics": parsed_rule.get("topics", default_topics()),
        "entries": entries,
        "anchors": anchors,
        "ignored": ignored,
        "warnings": warnings,
        "raw_stats": raw_stats,
    }
    write_json_file(Path(args.output), payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为 tid=43877379 按 21552 楼规则统计 NGA 安价并输出 viewer JSON。")
    parser.add_argument("--tid", type=int, default=DEFAULT_TID, help="帖子 tid，默认 43877379。")
    parser.add_argument("--rule-lou", type=int, default=DEFAULT_RULE_LOU, help="规则楼层，默认 21552。")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="用于作者包语义判定的模型。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 JSON 路径。")
    parser.add_argument("--cache-dir", default=None, help="分页 JSON 缓存目录，默认 output/{tid}_all/json。")
    parser.add_argument("--force-refresh", action="store_true", help="忽略已有缓存，重新抓取分页 JSON。")
    parser.add_argument("--no-llm", action="store_true", help="不调用模型，仅输出候选楼并标记人工复核。")
    parser.add_argument("--max-retries", type=int, default=2, help="模型 JSON 解析失败时的重试次数。")
    parser.add_argument("--reasoning-effort", default=DEFAULT_REASONING_EFFORT, choices=["high", "max"], help="DeepSeek 思考强度，默认 high。")
    parser.add_argument("--post-char-limit", type=int, default=DEFAULT_POST_CHAR_LIMIT, help="单楼发送给模型的正文字符上限；0 表示不截断。")
    parser.add_argument("--start-lou", type=int, default=None, help="手动覆盖起始楼层。")
    parser.add_argument("--end-lou", type=int, default=None, help="手动覆盖截止楼层。")
    parser.add_argument("--end-time", default=None, help="手动覆盖截止时间，格式 YYYY-MM-DD HH:MM。")
    parser.add_argument("--keyword", default=None, help="手动覆盖关键词过滤。")
    parser.add_argument("--ignore-user", action="append", default=[], help="额外忽略 uid，可重复或逗号分隔。")
    parser.add_argument("--ignore-lou", action="append", default=[], help="额外忽略楼层，可重复或逗号分隔。")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.ignore_user_values = parse_int_values(args.ignore_user)
    args.ignore_lou_values = parse_int_values(args.ignore_lou)

    try:
        payload = run_counter(args)
        print(
            "统计完成："
            f"有效条目 {payload['meta']['entry_count']} 条，"
            f"作者 {payload['meta']['author_count']} 个，"
            f"忽略 {payload['meta']['ignored_count']} 条，"
            f"输出 {args.output}"
        )
        return 0
    except CounterFailure as exc:
        payload = failure_payload(args, str(exc), exc.warnings)
        write_json_file(Path(args.output), payload)
        print(f"统计失败：{exc}")
        print(f"失败信息已写入：{args.output}")
        return 1
    except Exception as exc:
        payload = failure_payload(args, str(exc), [])
        write_json_file(Path(args.output), payload)
        print(f"统计失败：{exc}")
        print(f"失败信息已写入：{args.output}")
        return 1


if __name__ == "__main__":
    sys.exit(main())

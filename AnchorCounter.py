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
SCHEMA_VERSION = 2
DEFAULT_TOPICS = [
    {
        "id": "theme1_travel",
        "name": "主题1：旅行",
        "short_name": "旅行",
        "allow_multiple_per_author": False,
        "description": "人物、地点、事件形式的暑假马车旅行安价。",
    },
    {
        "id": "theme2_training",
        "name": "主题2：女仆修行 livehouse / 修行之道",
        "short_name": "修行之道",
        "allow_multiple_per_author": False,
        "description": "老师、主要属性培养、事件形式的女仆培训安价。",
    },
    {
        "id": "theme2_guest",
        "name": "主题2：女仆修行 livehouse / 难缠之客",
        "short_name": "难缠之客",
        "allow_multiple_per_author": False,
        "description": "客人、事件形式的委托或招待测试安价。",
    },
    {
        "id": "theme3_engagement",
        "name": "主题3：订婚大作战",
        "short_name": "订婚",
        "allow_multiple_per_author": False,
        "description": "人物、地点、事件形式的促成贴贴安价。",
    },
    {
        "id": "theme4_carriage_name",
        "name": "主题4：马车的名字",
        "short_name": "马车名字",
        "allow_multiple_per_author": False,
        "description": "安价名字形式的马车命名。",
    },
    {
        "id": "qa",
        "name": "特别环节：诡秘小祥一周年 Q&A",
        "short_name": "Q&A",
        "allow_multiple_per_author": True,
        "description": "向神奇小猪提问；同一作者可提交多个问题。",
    },
]


class CounterFailure(Exception):
    def __init__(self, message: str, warnings: list[str] | None = None):
        super().__init__(message)
        self.warnings = warnings or []


def clean_post_text(text: Any) -> str:
    if not isinstance(text, str):
        return ""

    cleaned = text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
    cleaned = re.sub(r"\[img\].*?\[/img\]", "", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = re.sub(r"\[/?[^\]]+\]", "", cleaned)
    cleaned = unescape(cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = re.sub(r"[ \t]+\n", "\n", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def page_for_lou(lou: int) -> int:
    return lou // POSTS_PER_PAGE + 1


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
    page_payloads: list[dict[str, Any]] = []
    for page in range(start_page, end_page + 1):
        if page in loaded_pages and not force_refresh:
            page_payload = loaded_pages[page]
        else:
            page_payload = ensure_page(client, tid, page, cache_dir, force_refresh, warnings)
            loaded_pages[page] = page_payload
        page_payloads.append(page_payload)
    return page_payloads


def find_post_by_lou(posts: list[dict[str, Any]], lou: int) -> dict[str, Any] | None:
    for post in posts:
        if to_int(post.get("lou")) == lou:
            return post
    return None


def to_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def to_float(value: Any, default: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


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


def get_author(post: dict[str, Any]) -> dict[str, Any]:
    author = post.get("author", {})
    if not isinstance(author, dict):
        author = {}
    uid = to_int(author.get("uid"))
    return {
        "uid": uid if uid is not None else author.get("uid"),
        "username": str(author.get("username", "")),
    }


def get_attachment_summary(post: dict[str, Any]) -> list[dict[str, Any]]:
    attachments = post.get("attches", [])
    if not isinstance(attachments, list):
        return []
    summary: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        summary.append(
            {
                "type": attachment.get("type"),
                "attachurl": attachment.get("attachurl"),
                "name": attachment.get("url_utf8_org_name"),
            }
        )
    return summary


def post_record(post: dict[str, Any]) -> dict[str, Any]:
    original_content = post.get("content", "")
    content_text = clean_post_text(original_content)
    return {
        "lou": to_int(post.get("lou")),
        "pid": post.get("pid"),
        "postdate": post.get("postdate"),
        "author": get_author(post),
        "content": content_text,
        "original_content": original_content if isinstance(original_content, str) else "",
        "attachments": get_attachment_summary(post),
    }


def default_parsed_rule(rule_lou: int, warnings: list[str]) -> dict[str, Any]:
    return {
        "start_lou": rule_lou,
        "end_lou": None,
        "end_time": None,
        "ignore_author_user": [DEFAULT_THREAD_AUTHOR_UID],
        "not_anjia_lou_list": [],
        "keyword": None,
        "topics": default_topics(),
        "classification_rules": [],
        "confidence": 0.0,
        "warnings": warnings,
        "source": "fallback",
    }


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
    def __init__(self, model_name: str):
        import httpx
        from openai import OpenAI

        self.model_name = model_name
        base_url, api_key = load_model_endpoint(model_name)
        config_path = Path(__file__).resolve().with_name("config.json")
        config_data = read_json_file(config_path)
        proxy_port = config_data.get("proxy", {}).get("port")
        proxy = f"http://127.0.0.1:{proxy_port}" if isinstance(proxy_port, int) and proxy_port > 0 else None
        http_client = httpx.Client(
            proxy=proxy,
            timeout=httpx.Timeout(45.0, connect=10.0),
        )
        self.client = OpenAI(
            base_url=base_url,
            api_key=api_key,
            http_client=http_client,
            max_retries=0,
        )

    def chat(self, message: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[{"role": "user", "content": message}],
            temperature=0,
            max_tokens=2048,
            timeout=45,
        )
        content = response.choices[0].message.content
        if content is None:
            raise CounterFailure("模型返回空内容")
        return content


def build_agent(model_name: str) -> Any:
    return DirectChatAgent(model_name)


def reset_agent_history(agent: Any) -> None:
    if hasattr(agent, "history"):
        agent.history = []


def ask_agent_for_json(agent: Any, prompt: str, max_retries: int) -> dict[str, Any]:
    last_error: Exception | None = None
    current_prompt = prompt
    for attempt in range(max_retries + 1):
        response = agent.chat(current_prompt)
        reset_agent_history(agent)
        try:
            return extract_json_object(response)
        except (json.JSONDecodeError, ValueError) as exc:
            last_error = exc
            current_prompt = (
                "上一次回复不是可解析的 JSON。请只返回一个 JSON 对象，不要 Markdown，不要解释。\n"
                f"解析错误：{exc}\n\n原任务：\n{prompt}"
            )
            if attempt < max_retries:
                print(f"模型 JSON 解析失败，正在重试 {attempt + 1}/{max_retries}...")
    raise CounterFailure(f"模型连续返回不可解析 JSON：{last_error}")


def normalize_int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        parsed_item = to_int(item)
        if parsed_item is not None:
            result.append(parsed_item)
    return sorted(set(result))


def default_topics() -> list[dict[str, Any]]:
    return [dict(topic) for topic in DEFAULT_TOPICS]


def normalize_topic_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "unclassified"
    normalized = value.strip().lower().replace(" ", "_").replace("-", "_")
    aliases = {
        "主题1": "theme1_travel",
        "theme1": "theme1_travel",
        "travel": "theme1_travel",
        "旅行": "theme1_travel",
        "主题2": "theme2_training",
        "theme2": "theme2_training",
        "修行之道": "theme2_training",
        "training": "theme2_training",
        "女仆修行": "theme2_training",
        "难缠之客": "theme2_guest",
        "guest": "theme2_guest",
        "客人": "theme2_guest",
        "主题3": "theme3_engagement",
        "theme3": "theme3_engagement",
        "订婚": "theme3_engagement",
        "engagement": "theme3_engagement",
        "主题4": "theme4_carriage_name",
        "theme4": "theme4_carriage_name",
        "马车名字": "theme4_carriage_name",
        "马车的名字": "theme4_carriage_name",
        "carriage_name": "theme4_carriage_name",
        "特别环节": "qa",
        "q&a": "qa",
        "qa": "qa",
        "问答": "qa",
    }
    return aliases.get(normalized, normalized)


def normalize_topics(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) == 0:
        return default_topics()

    topics_by_id = {topic["id"]: dict(topic) for topic in DEFAULT_TOPICS}
    for item in value:
        if not isinstance(item, dict):
            continue
        topic_id = normalize_topic_id(item.get("id") or item.get("topic_id") or item.get("name"))
        if topic_id == "unclassified":
            continue
        base = topics_by_id.get(topic_id, {"id": topic_id})
        base["id"] = topic_id
        if isinstance(item.get("name"), str) and item["name"].strip():
            base["name"] = item["name"].strip()
        base.setdefault("name", topic_id)
        if isinstance(item.get("short_name"), str) and item["short_name"].strip():
            base["short_name"] = item["short_name"].strip()
        base.setdefault("short_name", base["name"])
        if "allow_multiple_per_author" in item:
            base["allow_multiple_per_author"] = bool(item["allow_multiple_per_author"])
        base.setdefault("allow_multiple_per_author", False)
        if isinstance(item.get("description"), str):
            base["description"] = item["description"]
        if isinstance(item.get("end_time"), str):
            base["end_time"] = item["end_time"]
        topics_by_id[topic_id] = base

    ordered_ids = [topic["id"] for topic in DEFAULT_TOPICS]
    extra_ids = [topic_id for topic_id in topics_by_id if topic_id not in ordered_ids]
    return [topics_by_id[topic_id] for topic_id in [*ordered_ids, *sorted(extra_ids)] if topic_id in topics_by_id]


def topic_lookup(parsed_rule: dict[str, Any]) -> dict[str, dict[str, Any]]:
    topics = normalize_topics(parsed_rule.get("topics"))
    return {str(topic["id"]): topic for topic in topics}


def normalize_parsed_rule(raw_rule: dict[str, Any], rule_lou: int) -> dict[str, Any]:
    start_lou = to_int(raw_rule.get("start_lou"))
    end_lou = to_int(raw_rule.get("end_lou"))
    confidence = to_float(raw_rule.get("confidence"), 0.0)
    warnings_value = raw_rule.get("warnings", [])
    classification_rules_value = raw_rule.get("classification_rules", [])

    if not isinstance(warnings_value, list):
        warnings_value = [str(warnings_value)]
    if not isinstance(classification_rules_value, list):
        classification_rules_value = [str(classification_rules_value)]

    ignore_author_user = normalize_int_list(raw_rule.get("ignore_author_user"))
    ignore_author_user.append(DEFAULT_THREAD_AUTHOR_UID)

    keyword = raw_rule.get("keyword")
    if keyword is not None and not isinstance(keyword, str):
        keyword = str(keyword)

    end_time = raw_rule.get("end_time")
    if end_time is not None and not isinstance(end_time, str):
        end_time = str(end_time)

    return {
        "start_lou": start_lou if start_lou is not None else rule_lou,
        "end_lou": end_lou,
        "end_time": end_time,
        "ignore_author_user": sorted(set(ignore_author_user)),
        "not_anjia_lou_list": normalize_int_list(raw_rule.get("not_anjia_lou_list")),
        "keyword": keyword,
        "topics": normalize_topics(raw_rule.get("topics")),
        "classification_rules": [str(item) for item in classification_rules_value],
        "confidence": max(0.0, min(float(confidence or 0.0), 1.0)),
        "warnings": [str(item) for item in warnings_value],
        "source": raw_rule.get("source", "llm"),
    }


def parse_rule_with_llm(agent: Any, rule_post: dict[str, Any], rule_lou: int, max_retries: int) -> dict[str, Any]:
    rule_text = rule_post["content"]
    rule_postdate = rule_post.get("postdate") or "未知"
    prompt = f"""
你是 NGA 安价统计规则解析器。请根据规则楼正文提取统计参数。

必须只返回 JSON 对象，字段如下：
{{
  "start_lou": 整数或 null,
  "end_lou": 整数或 null,
  "end_time": "YYYY-MM-DD HH:MM" 或 null,
  "ignore_author_user": [整数 uid],
  "not_anjia_lou_list": [整数楼层],
  "keyword": 字符串或 null,
    "topics": [
        {{
            "id": "theme1_travel | theme2_training | theme2_guest | theme3_engagement | theme4_carriage_name | qa",
            "name": "主题显示名",
            "short_name": "短显示名",
            "allow_multiple_per_author": true 或 false,
            "end_time": "YYYY-MM-DD HH:MM" 或 null,
            "description": "怎样识别这个主题的安价"
        }}
    ],
  "classification_rules": [字符串],
  "confidence": 0 到 1 的数字,
  "warnings": [字符串]
}}

规则：

规则楼清洗文本：
{rule_text}
""".strip()
    raw_rule = ask_agent_for_json(agent, prompt, max_retries)
    normalized_rule = normalize_parsed_rule(raw_rule, rule_lou)
    normalized_rule["source"] = "llm"
    return normalized_rule


def parse_known_rule(rule_lou: int, rule_post: dict[str, Any]) -> dict[str, Any]:
    topics = default_topics()
    for topic in topics:
        if topic["id"] != "qa":
            topic["end_time"] = "2026-05-04 23:00"
    return {
        "start_lou": rule_lou,
        "end_lou": None,
        "end_time": None,
        "ignore_author_user": [DEFAULT_THREAD_AUTHOR_UID],
        "not_anjia_lou_list": [],
        "keyword": None,
        "topics": topics,
        "classification_rules": [
            "一条回复可以同时包含多个主题安价，必须拆成多个 entries。",
            "每个作者每个普通主题限安价 1 个；主题2的修行之道和难缠之客视为两个独立主题。",
            "Q&A 特别环节允许同一作者提交多个问题，每个问题拆成一个 qa entry。",
            "主题1旅行：识别人物、地点、事件。",
            "主题2修行之道：识别老师、主要属性培养、事件。",
            "主题2难缠之客：识别客人、事件。",
            "主题3订婚大作战：识别人物、地点、事件。",
            "主题4马车名字：识别安价名字。",
            "聊天、补充说明、非安价内容应判定为无效；无法确定的内容使用 unclassified 并标记人工复核。",
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

    ignore_users = set(normalize_int_list(parsed_rule.get("ignore_author_user")))
    ignore_users.update(args.ignore_user_values)
    ignore_users.add(DEFAULT_THREAD_AUTHOR_UID)
    parsed_rule["ignore_author_user"] = sorted(ignore_users)

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
        lou = record.get("lou")
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
        if isinstance(keyword, str) and keyword and keyword not in record["original_content"] and keyword not in record["content"]:
            reasons.append(f"内容不包含关键词 {keyword}")

        if reasons:
            ignored.append({**record, "ignore_reason": "；".join(reasons), "stage": "deterministic"})
        else:
            candidates.append(record)
    return candidates, ignored


def truncate_text(text: str, limit: int = 900) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[已截断]"


def classify_candidates_without_llm(candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    classifications: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        lou = to_int(candidate.get("lou"))
        if lou is None:
            continue
        local_classification = classify_candidate_with_patterns(candidate, None)
        classifications[lou] = local_classification or manual_review_classification(candidate, "--no-llm 未能按本地规则拆分")
    return classifications


TOPIC_MARKERS: list[tuple[str, re.Pattern[str]]] = [
    ("theme1_travel", re.compile(r"主题\s*[一1][：:、\s]*(?:旅[游行])?|旅行安价|旅游安价")),
    ("theme2_training", re.compile(r"修行之道|女仆修行安价|女仆修行")),
    ("theme2_guest", re.compile(r"难缠之客|不速之客")),
    ("theme3_engagement", re.compile(r"主题\s*[三3]|订婚大作战|订婚")),
    ("theme4_carriage_name", re.compile(r"马车(?:的)?(?:名字|名称|名)|马车名称安价|马车名字安价|起名")),
    ("qa", re.compile(r"Q&A|QA|问答|提问|问题")),
]
ANCHOR_HINT_PATTERN = re.compile(
    r"安价|主题\s*[一二三四1234]|人物[：:]|地点[：:]|事件[：:]|老师[：:]|属性[：:]|课程[：:]|"
    r"修行之道|难缠之客|不速之客|马车(?:的)?(?:名字|名称|名)|Q&A|QA|问答|提问|问题"
)
FIELD_LABEL_PATTERN = re.compile(
    r"(?P<label>人物|地点|事件|老师|属性|培养|课程|客人|名字|名称|安价名字|问题|提问)\s*[：:]\s*"
)


def compact_anchor_text(value: str) -> str:
    compacted = re.sub(r"\n{2,}", "\n", value.strip())
    compacted = re.sub(r"[ \t]+", " ", compacted)
    return compacted.strip(" \n：:")


def parse_labeled_fields(section: str) -> dict[str, str]:
    matches = list(FIELD_LABEL_PATTERN.finditer(section))
    fields: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group("label")
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(section)
        value = compact_anchor_text(section[start:end])
        if not value:
            continue
        normalized_label = {
            "培养": "属性",
            "安价名字": "名字",
            "名称": "名字",
            "提问": "问题",
        }.get(label, label)
        fields[normalized_label] = value
    return fields


def topic_name_for(topic_id: str, parsed_rule: dict[str, Any] | None) -> tuple[str, str]:
    if parsed_rule is not None:
        topics = normalize_topics(parsed_rule.get("topics"))
    else:
        topics = default_topics()
    topic_map = {topic["id"]: topic for topic in topics}
    topic = topic_map.get(topic_id, {"name": topic_id, "short_name": topic_id})
    return str(topic.get("name", topic_id)), str(topic.get("short_name", topic.get("name", topic_id)))


def make_pattern_entry(
    topic_id: str,
    section: str,
    parsed_rule: dict[str, Any] | None,
    fields: dict[str, str] | None = None,
    confidence: float = 0.82,
    note: str | None = None,
) -> dict[str, Any] | None:
    normalized_text = compact_anchor_text(section)
    fields = fields or parse_labeled_fields(section)
    if not normalized_text and not fields:
        return None
    topic_name, topic_short_name = topic_name_for(topic_id, parsed_rule)
    return {
        "topic_id": topic_id,
        "topic_name": topic_name,
        "topic_short_name": topic_short_name,
        "subtopic_name": None,
        "normalized_anchor_text": normalized_text,
        "fields": fields,
        "confidence": confidence,
        "needs_manual_review": False,
        "note": note,
    }


def collect_topic_sections(text: str) -> list[tuple[str, str]]:
    markers: list[tuple[int, int, str]] = []
    for topic_id, pattern in TOPIC_MARKERS:
        for match in pattern.finditer(text):
            markers.append((match.start(), match.end(), topic_id))
    markers.sort(key=lambda item: item[0])

    filtered_markers: list[tuple[int, int, str]] = []
    last_start = -1
    for marker in markers:
        if marker[0] == last_start:
            continue
        filtered_markers.append(marker)
        last_start = marker[0]

    sections: list[tuple[str, str]] = []
    for index, (start, _end, topic_id) in enumerate(filtered_markers):
        next_start = filtered_markers[index + 1][0] if index + 1 < len(filtered_markers) else len(text)
        section = compact_anchor_text(text[start:next_start])
        if section:
            sections.append((topic_id, section))
    return sections


def extract_qa_entries(section: str, parsed_rule: dict[str, Any] | None) -> list[dict[str, Any]]:
    question_text = re.sub(r"^(?:Q&A|QA|问答|提问|问题)\s*[：:]?", "", section.strip(), flags=re.IGNORECASE)
    fragments = [fragment.strip() for fragment in re.split(r"(?<=[？?])", question_text) if fragment.strip()]
    entries: list[dict[str, Any]] = []
    for fragment in fragments or [question_text]:
        normalized = compact_anchor_text(fragment)
        if not normalized:
            continue
        entry = make_pattern_entry("qa", normalized, parsed_rule, {"问题": normalized}, 0.78, "本地规则识别为提问")
        if entry is not None:
            entries.append(entry)
    return entries


def classify_candidate_with_patterns(
    candidate: dict[str, Any],
    parsed_rule: dict[str, Any] | None,
) -> dict[str, Any] | None:
    lou = to_int(candidate.get("lou"))
    if lou is None:
        return None
    content = str(candidate.get("content", ""))
    if not content.strip():
        return {
            "lou": lou,
            "is_anchor": False,
            "ignore_reason": "空内容",
            "needs_manual_review": False,
            "source": "local_patterns",
            "entries": [],
        }
    if not ANCHOR_HINT_PATTERN.search(content):
        return {
            "lou": lou,
            "is_anchor": False,
            "ignore_reason": "未发现安价关键词或字段标签",
            "needs_manual_review": False,
            "source": "local_patterns",
            "entries": [],
        }

    entries: list[dict[str, Any]] = []
    for topic_id, section in collect_topic_sections(content):
        if topic_id == "qa":
            entries.extend(extract_qa_entries(section, parsed_rule))
            continue
        fields = parse_labeled_fields(section)
        if topic_id == "theme4_carriage_name" and "名字" not in fields:
            name_value = re.sub(r".*?马车(?:的)?(?:名字|名称|名)(?:安价)?\s*[：:]?", "", section, count=1)
            name_value = compact_anchor_text(name_value.split("\n", 1)[0])
            if name_value:
                fields["名字"] = name_value
        entry = make_pattern_entry(topic_id, section, parsed_rule, fields, 0.84, "本地规则识别")
        if entry is not None:
            entries.append(entry)

    if not entries and re.search(r"人物\s*[：:].*地点\s*[：:].*事件\s*[：:]", content, flags=re.DOTALL):
        entry = make_pattern_entry("theme1_travel", content, parsed_rule, parse_labeled_fields(content), 0.76, "本地规则按人物/地点/事件识别为主题1")
        if entry is not None:
            entries.append(entry)

    if not entries:
        return None

    return {
        "lou": lou,
        "is_anchor": True,
        "ignore_reason": None,
        "needs_manual_review": False,
        "source": "local_patterns",
        "entries": entries,
    }


def normalize_classification_entry(entry: dict[str, Any], parsed_rule: dict[str, Any]) -> dict[str, Any] | None:
    topic_map = topic_lookup(parsed_rule)
    topic_id = normalize_topic_id(entry.get("topic_id") or entry.get("topic") or entry.get("topic_name"))
    if topic_id == "unclassified" and not entry.get("normalized_anchor_text"):
        return None
    topic_meta = topic_map.get(topic_id, {"id": topic_id, "name": entry.get("topic_name") or topic_id, "short_name": entry.get("topic_name") or topic_id})
    normalized_text = entry.get("normalized_anchor_text") or entry.get("content") or entry.get("text")
    fields = entry.get("fields", {})
    if not isinstance(fields, dict):
        fields = {}
    note = entry.get("note") or entry.get("classification_note")
    subtopic_name = entry.get("subtopic_name")
    return {
        "topic_id": topic_id,
        "topic_name": str(topic_meta.get("name", topic_id)),
        "topic_short_name": str(topic_meta.get("short_name", topic_meta.get("name", topic_id))),
        "subtopic_name": subtopic_name if isinstance(subtopic_name, str) else None,
        "normalized_anchor_text": normalized_text if isinstance(normalized_text, str) else "",
        "fields": fields,
        "confidence": to_float(entry.get("confidence"), None),
        "needs_manual_review": bool(entry.get("needs_manual_review", False)),
        "note": note if isinstance(note, str) else None,
    }


def normalize_classification_item(item: dict[str, Any], parsed_rule: dict[str, Any]) -> dict[str, Any] | None:
    lou = to_int(item.get("lou"))
    if lou is None:
        return None
    is_anchor = item.get("is_anchor")
    if not isinstance(is_anchor, bool):
        is_anchor = str(is_anchor).lower() in {"true", "1", "yes", "是"}
    ignore_reason = item.get("ignore_reason")
    raw_entries = item.get("entries", [])
    entries: list[dict[str, Any]] = []
    if isinstance(raw_entries, list):
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            normalized_entry = normalize_classification_entry(raw_entry, parsed_rule)
            if normalized_entry is not None:
                entries.append(normalized_entry)

    if is_anchor and len(entries) == 0:
        legacy_entry = normalize_classification_entry(
            {
                "topic_id": item.get("topic_id") or "unclassified",
                "topic_name": item.get("topic_name") or "未分类",
                "normalized_anchor_text": item.get("normalized_anchor_text") or "",
                "confidence": item.get("confidence"),
                "needs_manual_review": item.get("needs_manual_review", True),
                "note": "模型使用了旧格式，未拆分主题",
            },
            parsed_rule,
        )
        if legacy_entry is not None:
            entries.append(legacy_entry)

    return {
        "lou": lou,
        "is_anchor": is_anchor and len(entries) > 0,
        "ignore_reason": ignore_reason if isinstance(ignore_reason, str) else None,
        "needs_manual_review": bool(item.get("needs_manual_review", False)),
        "entries": entries,
        "source": "llm",
    }


def classify_candidates_with_llm(
    agent: Any,
    parsed_rule: dict[str, Any],
    candidates: list[dict[str, Any]],
    batch_size: int,
    max_retries: int,
    warnings: list[str],
) -> dict[int, dict[str, Any]]:
    classifications: dict[int, dict[str, Any]] = {}
    if batch_size <= 0:
        batch_size = 20

    pending_candidates: list[dict[str, Any]] = []
    for candidate in candidates:
        lou = to_int(candidate.get("lou"))
        if lou is None:
            continue
        local_classification = classify_candidate_with_patterns(candidate, parsed_rule)
        if local_classification is None:
            pending_candidates.append(candidate)
        else:
            classifications[lou] = local_classification

    if len(pending_candidates) != len(candidates):
        print(f"本地规则已判定 {len(candidates) - len(pending_candidates)}/{len(candidates)} 条，剩余 {len(pending_candidates)} 条交给模型。")
    if not pending_candidates:
        return classifications

    for start_index in range(0, len(pending_candidates), batch_size):
        batch = pending_candidates[start_index : start_index + batch_size]
        batch_number = start_index // batch_size + 1
        batch_total = (len(pending_candidates) + batch_size - 1) // batch_size
        print(
            f"正在判定候选批次 {batch_number}/{batch_total}，"
            f"楼层 {batch[0].get('lou')} - {batch[-1].get('lou')}..."
        )
        batch_payload = [
            {
                "lou": candidate["lou"],
                "postdate": candidate.get("postdate"),
                "author": candidate.get("author"),
                "content": truncate_text(str(candidate.get("content", ""))),
            }
            for candidate in batch
        ]
        prompt = f"""
你是 NGA 安价候选楼层判定器。请根据 parsed_rule 判断每条候选是否为有效安价。

必须只返回 JSON 对象，格式如下：
{{
  "items": [
    {{
      "lou": 整数,
      "is_anchor": true 或 false,
      "ignore_reason": "无效原因；有效时为 null",
            "needs_manual_review": true 或 false,
            "entries": [
                {{
                    "topic_id": "theme1_travel | theme2_training | theme2_guest | theme3_engagement | theme4_carriage_name | qa",
                    "topic_name": "主题显示名",
                    "subtopic_name": "可选子项名或 null",
                    "normalized_anchor_text": "只属于这个主题的安价正文，不要混入其他主题",
                    "fields": {{"人物": "...", "地点": "...", "事件": "..."}},
                    "confidence": 0 到 1 的数字,
                    "needs_manual_review": true 或 false,
                    "note": "可选说明"
                }}
            ]
    }}
  ]
}}

拆分要求：
- 一条回复可能同时安价多个主题，必须拆成多个 entries。
- 主题2的“修行之道”和“难缠之客”是两个独立 entries。
- Q&A 可以从同一回复中拆出多个问题，每个问题一个 entry，topic_id 都为 qa。
- 如果回复只是聊天、顶帖、修改说明、不是安价，is_anchor=false 且 entries=[]。
- 如果无法确定属于哪个主题，不要丢弃；使用 topic_id="unclassified" 并标记 needs_manual_review=true。

parsed_rule:
{json.dumps(parsed_rule, ensure_ascii=False)}

候选楼层：
{json.dumps(batch_payload, ensure_ascii=False)}
""".strip()
        try:
            response_json = ask_agent_for_json(agent, prompt, max_retries)
            items = response_json.get("items", [])
            if not isinstance(items, list):
                raise ValueError("items 不是列表")
            returned_lous: set[int] = set()
            for item in items:
                if not isinstance(item, dict):
                    continue
                normalized = normalize_classification_item(item, parsed_rule)
                if normalized is None:
                    continue
                lou = normalized["lou"]
                returned_lous.add(lou)
                classifications[lou] = normalized

            expected_lous = {int(candidate["lou"]) for candidate in batch if candidate.get("lou") is not None}
            missing_lous = sorted(expected_lous - returned_lous)
            if missing_lous:
                warnings.append(f"模型未返回这些楼层的判定，已标记人工复核：{missing_lous}")
                for candidate in batch:
                    lou = to_int(candidate.get("lou"))
                    if lou in missing_lous:
                        classifications[lou] = manual_review_classification(candidate, "模型未返回该楼层")
        except Exception as exc:
            batch_lous = [candidate.get("lou") for candidate in batch]
            warnings.append(f"模型判定批次失败，已标记人工复核。楼层={batch_lous}，错误={exc}")
            for candidate in batch:
                lou = to_int(candidate.get("lou"))
                if lou is not None:
                    classifications[lou] = manual_review_classification(candidate, str(exc))
    return classifications


def manual_review_classification(candidate: dict[str, Any], reason: str) -> dict[str, Any]:
    lou = int(candidate["lou"])
    return {
        "lou": lou,
        "is_anchor": True,
        "ignore_reason": reason,
        "needs_manual_review": True,
        "source": "manual_review_fallback",
        "entries": [
            {
                "topic_id": "unclassified",
                "topic_name": "未分类",
                "topic_short_name": "未分类",
                "subtopic_name": None,
                "normalized_anchor_text": candidate.get("content", ""),
                "fields": {},
                "confidence": None,
                "needs_manual_review": True,
                "note": reason,
            }
        ],
    }


def aggregate_entries(
    candidates: list[dict[str, Any]],
    classifications: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    grouped_by_author: dict[str, dict[str, Any]] = {}
    entries: list[dict[str, Any]] = []
    ignored_by_llm: list[dict[str, Any]] = []

    for candidate in candidates:
        lou = to_int(candidate.get("lou"))
        if lou is None:
            ignored_by_llm.append({**candidate, "ignore_reason": "楼层号缺失", "stage": "aggregation"})
            continue
        classification = classifications.get(lou, manual_review_classification(candidate, "缺少判定结果"))
        if not classification.get("is_anchor"):
            ignored_by_llm.append(
                {
                    **candidate,
                    "ignore_reason": classification.get("ignore_reason") or "模型判定不是有效安价",
                    "stage": classification.get("source", "llm"),
                }
            )
            continue

        author = candidate.get("author", {})
        author_uid = author.get("uid") if isinstance(author, dict) else None
        author_key = str(author_uid)
        if author_key not in grouped_by_author:
            grouped_by_author[author_key] = {
                "author": author,
                "posts": [],
                "entries": [],
            }
        grouped_by_author[author_key]["posts"].append(
            {
                "lou": lou,
                "pid": candidate.get("pid"),
                "postdate": candidate.get("postdate"),
                "content": candidate.get("content", ""),
                "original_content": candidate.get("original_content", ""),
                "attachments": candidate.get("attachments", []),
                "classification_source": classification.get("source"),
                "classification_note": classification.get("ignore_reason"),
                "needs_manual_review": bool(classification.get("needs_manual_review", False)),
            }
        )

        raw_entries = classification.get("entries", [])
        if not isinstance(raw_entries, list):
            raw_entries = []
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, dict):
                continue
            entry = {
                "id": 0,
                "topic_id": raw_entry.get("topic_id", "unclassified"),
                "topic_name": raw_entry.get("topic_name", "未分类"),
                "topic_short_name": raw_entry.get("topic_short_name", raw_entry.get("topic_name", "未分类")),
                "subtopic_name": raw_entry.get("subtopic_name"),
                "author": author,
                "lou": lou,
                "pid": candidate.get("pid"),
                "postdate": candidate.get("postdate"),
                "content": raw_entry.get("normalized_anchor_text") or candidate.get("content", ""),
                "fields": raw_entry.get("fields", {}),
                "raw_clean_content": candidate.get("content", ""),
                "original_content": candidate.get("original_content", ""),
                "attachments": candidate.get("attachments", []),
                "confidence": raw_entry.get("confidence"),
                "needs_manual_review": bool(raw_entry.get("needs_manual_review") or classification.get("needs_manual_review", False)),
                "classification_source": classification.get("source"),
                "classification_note": raw_entry.get("note") or classification.get("ignore_reason"),
                "has_duplicate": False,
                "duplicate_lous": [],
                "duplicate_entry_ids": [],
            }
            entries.append(entry)
            grouped_by_author[author_key]["entries"].append(entry)

    topic_meta = {topic["id"]: topic for topic in DEFAULT_TOPICS}
    duplicate_groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        topic_id = str(entry.get("topic_id", "unclassified"))
        allow_multiple = bool(topic_meta.get(topic_id, {}).get("allow_multiple_per_author", topic_id == "qa"))
        if allow_multiple:
            continue
        author = entry.get("author", {})
        author_uid = author.get("uid") if isinstance(author, dict) else None
        duplicate_groups.setdefault((str(author_uid), topic_id), []).append(entry)

    for duplicate_entries in duplicate_groups.values():
        if len(duplicate_entries) <= 1:
            continue
        duplicate_lous = sorted({int(entry["lou"]) for entry in duplicate_entries if entry.get("lou") is not None})
        for entry in duplicate_entries:
            entry["has_duplicate"] = True
            entry["duplicate_lous"] = duplicate_lous
            entry["needs_manual_review"] = True

    entries.sort(key=lambda entry: (str(entry.get("topic_id", "")), int(entry.get("lou", 0))))
    for index, entry in enumerate(entries, start=1):
        entry["id"] = index
    for duplicate_entries in duplicate_groups.values():
        if len(duplicate_entries) <= 1:
            continue
        duplicate_ids = sorted(int(entry["id"]) for entry in duplicate_entries)
        for entry in duplicate_entries:
            entry["duplicate_entry_ids"] = duplicate_ids

    anchors = list(grouped_by_author.values())
    anchors.sort(key=lambda anchor: min(post["lou"] for post in anchor["posts"]))
    for index, anchor in enumerate(anchors, start=1):
        anchor_posts = sorted(anchor["posts"], key=lambda post: post["lou"])
        anchor_entries = sorted(anchor["entries"], key=lambda entry: int(entry["id"]))
        anchor["id"] = index
        anchor["posts"] = anchor_posts
        anchor["entries"] = anchor_entries
        anchor["first_lou"] = anchor_posts[0]["lou"]
        anchor["first_postdate"] = anchor_posts[0].get("postdate")
        anchor["has_duplicate"] = any(entry.get("has_duplicate") for entry in anchor_entries)
        anchor["duplicate_lous"] = sorted({lou for entry in anchor_entries for lou in entry.get("duplicate_lous", [])})
        anchor["topic_ids"] = sorted({str(entry.get("topic_id")) for entry in anchor_entries})
        numeric_confidences = [entry["confidence"] for entry in anchor_entries if isinstance(entry.get("confidence"), (int, float))]
        anchor["confidence"] = round(sum(numeric_confidences) / len(numeric_confidences), 4) if numeric_confidences else None
        anchor["needs_manual_review"] = any(entry.get("needs_manual_review") for entry in anchor_entries)
    return entries, anchors, ignored_by_llm


def compress_int_ranges(values: list[int]) -> list[dict[str, int]]:
    if not values:
        return []
    sorted_values = sorted(set(values))
    ranges: list[dict[str, int]] = []
    range_start = sorted_values[0]
    previous_value = sorted_values[0]
    for current_value in sorted_values[1:]:
        if current_value == previous_value + 1:
            previous_value = current_value
            continue
        ranges.append({"start": range_start, "end": previous_value, "count": previous_value - range_start + 1})
        range_start = current_value
        previous_value = current_value
    ranges.append({"start": range_start, "end": previous_value, "count": previous_value - range_start + 1})
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


def build_rule_post_payload(rule_post: dict[str, Any]) -> dict[str, Any]:
    return post_record(rule_post)


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
        raise CounterFailure(
            f"当前全帖共 {total_pages} 页，规则楼 {rule_lou} 预计在第 {rule_page} 页，线上数据不足。",
            warnings,
        )

    rule_window_start = max(1, rule_page - 1)
    rule_window_end = min(total_pages, rule_page + 1)
    rule_pages = load_page_range(
        client,
        tid,
        rule_window_start,
        rule_window_end,
        cache_dir,
        args.force_refresh,
        warnings,
        loaded_pages,
    )
    rule_posts = flatten_posts(rule_pages)
    rule_post = find_post_by_lou(rule_posts, rule_lou)
    if rule_post is None:
        raise CounterFailure(f"已抓取规则楼附近页面，但没有找到 {rule_lou} 楼。可能被吞楼或权限不足。", warnings)

    rule_post_payload = build_rule_post_payload(rule_post)
    agent = None
    if args.no_llm:
        warnings.append("已启用 --no-llm，规则解析使用默认值并标记人工复核。")
        parsed_rule = default_parsed_rule(rule_lou, ["未调用模型解析规则"])
    elif not args.parse_rule_with_llm:
        print(f"使用内置规则解析 {rule_lou} 楼多主题安价规则。")
        parsed_rule = parse_known_rule(rule_lou, rule_post_payload)
    else:
        print(f"正在使用模型 {args.model} 解析 {rule_lou} 楼规则...")
        agent = build_agent(args.model)
        parsed_rule = parse_rule_with_llm(agent, rule_post_payload, rule_lou, args.max_retries)
    parsed_rule = apply_manual_overrides(parsed_rule, args, warnings)

    start_page = page_for_lou(int(parsed_rule["start_lou"]))
    end_lou = parsed_rule.get("end_lou")
    end_page = page_for_lou(int(end_lou)) if end_lou is not None else total_pages
    end_page = max(start_page, min(end_page, total_pages))
    page_payloads = load_page_range(
        client,
        tid,
        start_page,
        end_page,
        cache_dir,
        args.force_refresh,
        warnings,
        loaded_pages,
    )
    posts = flatten_posts(page_payloads)
    candidates, ignored = build_candidates(posts, parsed_rule)

    if args.no_llm:
        classifications = classify_candidates_without_llm(candidates)
    else:
        if agent is None:
            agent = build_agent(args.model)
        classifications = classify_candidates_with_llm(
            agent,
            parsed_rule,
            candidates,
            args.batch_size,
            args.max_retries,
            warnings,
        )

    entries, anchors, llm_ignored = aggregate_entries(candidates, classifications)
    ignored.extend(llm_ignored)
    raw_stats = build_raw_stats(posts, parsed_rule)
    manual_review_required = bool(
        args.no_llm
        or parsed_rule.get("warnings")
        or any(entry.get("needs_manual_review") for entry in entries)
        or warnings
    )
    duplicate_entry_count = sum(1 for entry in entries if entry.get("has_duplicate"))
    topic_counts: dict[str, int] = {}
    for entry in entries:
        topic_id = str(entry.get("topic_id", "unclassified"))
        topic_counts[topic_id] = topic_counts.get(topic_id, 0) + 1

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
            "duplicate_entry_count": duplicate_entry_count,
            "duplicate_author_count": sum(1 for anchor in anchors if anchor.get("has_duplicate")),
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="为 tid=43877379 按规则楼统计 NGA 安价并输出 JSON。")
    parser.add_argument("--tid", type=int, default=DEFAULT_TID, help="帖子 tid，默认 43877379。")
    parser.add_argument("--rule-lou", type=int, default=DEFAULT_RULE_LOU, help="规则楼层，默认 21552。")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="用于规则解析和语义判定的模型。")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 JSON 路径。")
    parser.add_argument("--cache-dir", default=None, help="全帖分页 JSON 缓存目录，默认 output/{tid}_all/json。")
    parser.add_argument("--force-refresh", action="store_true", help="忽略已有缓存，重新抓取分页 JSON。")
    parser.add_argument("--no-llm", action="store_true", help="不调用模型，仅输出确定性过滤结果并标记人工复核。")
    parser.add_argument("--parse-rule-with-llm", action="store_true", help="强制使用模型解析规则楼；默认使用 21552 楼内置规则。")
    parser.add_argument("--batch-size", type=int, default=20, help="模型判定候选楼层的批大小。")
    parser.add_argument("--max-retries", type=int, default=2, help="模型 JSON 解析失败时的重试次数。")
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
            f"有效安价 {payload['meta']['anchor_count']} 条，"
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
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
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_OUTPUT = Path("anjia-viewer") / "public" / "data" / "anchors_43877379.json"
POSTS_PER_PAGE = 20
SCHEMA_VERSION = 1


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


def build_agent(model_name: str) -> Any:
    from Agent import build

    base_url, api_key = load_model_endpoint(model_name)
    return build(model_name=model_name, base_url=base_url, api_key=api_key, name="AnchorCounter")


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
        "classification_rules": [str(item) for item in classification_rules_value],
        "confidence": max(0.0, min(float(confidence or 0.0), 1.0)),
        "warnings": [str(item) for item in warnings_value],
        "source": raw_rule.get("source", "llm"),
    }


def parse_rule_with_llm(agent: Any, rule_post: dict[str, Any], rule_lou: int, max_retries: int) -> dict[str, Any]:
    rule_text = rule_post["content"]
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
  "classification_rules": [字符串],
  "confidence": 0 到 1 的数字,
  "warnings": [字符串]
}}

规则：
- 如果正文没有明确开始楼层，请使用 {rule_lou}。
- 不要编造 uid 或楼层；无法确定的内容放入 warnings。
- classification_rules 写给后续候选楼层判定使用，描述什么算有效安价、什么应忽略。

规则楼清洗文本：
{rule_text}
""".strip()
    raw_rule = ask_agent_for_json(agent, prompt, max_retries)
    normalized_rule = normalize_parsed_rule(raw_rule, rule_lou)
    normalized_rule["source"] = "llm"
    return normalized_rule


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


def truncate_text(text: str, limit: int = 1800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[已截断]"


def classify_candidates_without_llm(candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    classifications: dict[int, dict[str, Any]] = {}
    for candidate in candidates:
        lou = to_int(candidate.get("lou"))
        if lou is None:
            continue
        classifications[lou] = {
            "lou": lou,
            "is_anchor": True,
            "normalized_anchor_text": candidate.get("content", ""),
            "ignore_reason": None,
            "confidence": None,
            "needs_manual_review": True,
            "source": "deterministic_no_llm",
        }
    return classifications


def normalize_classification_item(item: dict[str, Any]) -> dict[str, Any] | None:
    lou = to_int(item.get("lou"))
    if lou is None:
        return None
    is_anchor = item.get("is_anchor")
    if not isinstance(is_anchor, bool):
        is_anchor = str(is_anchor).lower() in {"true", "1", "yes", "是"}
    normalized_text = item.get("normalized_anchor_text")
    ignore_reason = item.get("ignore_reason")
    return {
        "lou": lou,
        "is_anchor": is_anchor,
        "normalized_anchor_text": normalized_text if isinstance(normalized_text, str) else "",
        "ignore_reason": ignore_reason if isinstance(ignore_reason, str) else None,
        "confidence": to_float(item.get("confidence"), None),
        "needs_manual_review": bool(item.get("needs_manual_review", False)),
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

    for start_index in range(0, len(candidates), batch_size):
        batch = candidates[start_index : start_index + batch_size]
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
      "normalized_anchor_text": "有效安价的规范化文本；无效时为空字符串",
      "ignore_reason": "无效原因；有效时为 null",
      "confidence": 0 到 1 的数字,
      "needs_manual_review": true 或 false
    }}
  ]
}}

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
                normalized = normalize_classification_item(item)
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
        "normalized_anchor_text": candidate.get("content", ""),
        "ignore_reason": reason,
        "confidence": None,
        "needs_manual_review": True,
        "source": "manual_review_fallback",
    }


def aggregate_anchors(
    candidates: list[dict[str, Any]],
    classifications: dict[int, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[str, dict[str, Any]] = {}
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
                    "confidence": classification.get("confidence"),
                    "stage": classification.get("source", "llm"),
                }
            )
            continue

        author = candidate.get("author", {})
        author_uid = author.get("uid") if isinstance(author, dict) else None
        author_key = str(author_uid)
        anchor_post = {
            "lou": lou,
            "pid": candidate.get("pid"),
            "postdate": candidate.get("postdate"),
            "content": classification.get("normalized_anchor_text") or candidate.get("content", ""),
            "raw_clean_content": candidate.get("content", ""),
            "original_content": candidate.get("original_content", ""),
            "attachments": candidate.get("attachments", []),
            "confidence": classification.get("confidence"),
            "needs_manual_review": bool(classification.get("needs_manual_review", False)),
            "classification_source": classification.get("source"),
            "classification_note": classification.get("ignore_reason"),
        }

        if author_key not in grouped:
            grouped[author_key] = {
                "author": author,
                "posts": [],
            }
        grouped[author_key]["posts"].append(anchor_post)

    anchors = list(grouped.values())
    anchors.sort(key=lambda anchor: min(post["lou"] for post in anchor["posts"]))
    for index, anchor in enumerate(anchors, start=1):
        anchor_posts = sorted(anchor["posts"], key=lambda post: post["lou"])
        anchor["id"] = index
        anchor["posts"] = anchor_posts
        anchor["first_lou"] = anchor_posts[0]["lou"]
        anchor["first_postdate"] = anchor_posts[0].get("postdate")
        anchor["has_duplicate"] = len(anchor_posts) > 1
        anchor["duplicate_lous"] = [post["lou"] for post in anchor_posts] if len(anchor_posts) > 1 else []
        numeric_confidences = [post["confidence"] for post in anchor_posts if isinstance(post.get("confidence"), (int, float))]
        anchor["confidence"] = round(sum(numeric_confidences) / len(numeric_confidences), 4) if numeric_confidences else None
        anchor["needs_manual_review"] = any(post.get("needs_manual_review") for post in anchor_posts)
    return anchors, ignored_by_llm


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
    else:
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

    anchors, llm_ignored = aggregate_anchors(candidates, classifications)
    ignored.extend(llm_ignored)
    raw_stats = build_raw_stats(posts, parsed_rule)
    manual_review_required = bool(
        args.no_llm
        or parsed_rule.get("warnings")
        or any(anchor.get("needs_manual_review") for anchor in anchors)
        or warnings
    )

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
            "anchor_count": len(anchors),
            "ignored_count": len(ignored),
            "duplicate_author_count": sum(1 for anchor in anchors if anchor.get("has_duplicate")),
        },
        "rule_post": rule_post_payload,
        "parsed_rule": parsed_rule,
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
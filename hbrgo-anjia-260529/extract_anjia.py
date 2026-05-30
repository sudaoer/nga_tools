from __future__ import annotations

import html
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional, TypedDict, cast

from bs4 import BeautifulSoup

WORK_DIR = Path(__file__).resolve().parent
RULES_PATH = WORK_DIR / "rules.json"
THREAD_JSON_DIR = WORK_DIR / "thread_json"
ANJIA_DIR = WORK_DIR / "anjia"
SUMMARY_PATH = WORK_DIR / "anjia_summary.json"
LOCAL_TZ = timezone(timedelta(hours=8))


class SourcePost(TypedDict):
    pid: int
    lou: int
    page: int
    postdate: str
    uid: int
    username: str


class AnjiaRecord(TypedDict):
    id: str
    accepted_candidate: bool
    duplicate_policy: str
    user_submission_index: int
    source_post: SourcePost
    anjia_text: str
    plain_text: str
    raw_content: str
    rule_refs: dict[str, object]
    flags: list[str]
    notes: str


def _read_json(path: Path) -> dict[str, Any]:
    raw_data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_data, dict):
        raise ValueError(f"JSON顶层必须是对象：{path}")
    return raw_data


def _write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _page_number(path: Path) -> int:
    stem = path.stem
    page_part = stem.removeprefix("page_")
    if not page_part.isdecimal():
        return 0
    return int(page_part)


def _page_paths() -> list[Path]:
    if not THREAD_JSON_DIR.exists():
        raise FileNotFoundError(
            f"未找到全贴JSON目录：{THREAD_JSON_DIR}。请先运行download_full_thread.py。"
        )
    return sorted(THREAD_JSON_DIR.glob("page_*.json"), key=_page_number)


def _post_datetime(postdate: str) -> datetime:
    return datetime.strptime(postdate, "%Y-%m-%d %H:%M").replace(tzinfo=LOCAL_TZ)


def _strip_quote_blocks(content: str) -> str:
    pattern = re.compile(r"\[quote\].*?\[/quote\]", re.IGNORECASE | re.DOTALL)
    previous = content
    while True:
        current = pattern.sub("", previous)
        if current == previous:
            return current
        previous = current


def _plain_text(content: str) -> str:
    with_breaks = re.sub(r"<br\s*/?>", "\n", content, flags=re.IGNORECASE)
    soup = BeautifulSoup(with_breaks, "html.parser")
    text = soup.get_text("\n")
    text = html.unescape(text)
    text = re.sub(r"\[/?[^\]]+\]", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_anjia_text(plain_text: str, marker_pattern: re.Pattern[str]) -> Optional[str]:
    match = marker_pattern.search(plain_text)
    if match is None:
        return None
    return plain_text[match.end() :].strip(" \t\r\n:：")


def _content_rule_notes(anjia_text: str) -> tuple[list[str], list[str], bool]:
    flags: list[str] = []
    notes: list[str] = []
    blocks_candidate = False

    if "出轨" in anjia_text and any(
        keyword in anjia_text
        for keyword in ("爱音", "愛音", "祥子", "爱祥", "愛祥")
    ):
        flags.append("forbidden_aishou_cheating_event")
        notes.append("6355楼规则：涉及爱祥关系时不允许出轨类事件。")
        blocks_candidate = True

    if "加权" in anjia_text:
        flags.append("weighting_ignored")
        notes.append("6356楼规则：不存在加权机制，此处仅保留原文。")

    return flags, notes, blocks_candidate


def _required_int(data: dict[str, object], key: str, context: str) -> int:
    value = data.get(key)
    if type(value) is not int:
        raise ValueError(f"{context}缺少整数字段{key}。")
    return value


def _required_str(data: dict[str, object], key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{context}缺少字符串字段{key}。")
    return value


def _iter_posts() -> list[tuple[int, dict[str, object]]]:
    posts: list[tuple[int, dict[str, object]]] = []
    for path in _page_paths():
        page_number = _page_number(path)
        page_data = _read_json(path)
        result = page_data.get("result")
        if not isinstance(result, list):
            raise ValueError(f"页面JSON缺少result列表：{path}")
        for raw_post in result:
            if not isinstance(raw_post, dict):
                continue
            posts.append((page_number, cast(dict[str, object], raw_post)))
    return posts


def _rule_refs(rules: dict[str, Any]) -> dict[str, object]:
    collection = rules.get("collection")
    rule_posts = rules.get("rule_posts")
    return {
        "rules_version": rules.get("version"),
        "collection": collection if isinstance(collection, dict) else {},
        "rule_posts": rule_posts if isinstance(rule_posts, list) else [],
    }


def _build_records(rules: dict[str, Any]) -> list[AnjiaRecord]:
    collection = rules.get("collection")
    if not isinstance(collection, dict):
        raise ValueError("rules.json缺少collection配置。")

    start_after_lou = collection.get("start_after_original_lou")
    deadline_text = collection.get("deadline")
    marker_regex = collection.get("marker_regex")
    exclude_author_uid = collection.get("exclude_author_uid")
    if type(start_after_lou) is not int:
        raise ValueError("collection.start_after_original_lou必须是整数。")
    if not isinstance(deadline_text, str):
        raise ValueError("collection.deadline必须是字符串。")
    if not isinstance(marker_regex, str):
        raise ValueError("collection.marker_regex必须是字符串。")
    if type(exclude_author_uid) is not int:
        raise ValueError("collection.exclude_author_uid必须是整数。")

    deadline = datetime.fromisoformat(deadline_text)
    marker_pattern = re.compile(marker_regex)
    rule_refs = _rule_refs(rules)
    records: list[AnjiaRecord] = []

    for page_number, post in _iter_posts():
        lou = _required_int(post, "lou", f"第{page_number}页帖子")
        if lou <= start_after_lou:
            continue

        postdate = _required_str(post, "postdate", f"第{page_number}页第{lou}楼")
        if _post_datetime(postdate) > deadline:
            continue

        author = post.get("author")
        if not isinstance(author, dict):
            continue
        author_data = cast(dict[str, object], author)
        uid = _required_int(author_data, "uid", f"第{page_number}页第{lou}楼作者")
        if uid == exclude_author_uid:
            continue

        content = _required_str(post, "content", f"第{page_number}页第{lou}楼")
        content_without_quotes = _strip_quote_blocks(content)
        plain = _plain_text(content_without_quotes)
        anjia_text = _extract_anjia_text(plain, marker_pattern)
        if anjia_text is None:
            continue

        pid = _required_int(post, "pid", f"第{page_number}页第{lou}楼")
        username_value = author_data.get("username")
        username = username_value if isinstance(username_value, str) else f"UID:{uid}"
        source_post: SourcePost = {
            "pid": pid,
            "lou": lou,
            "page": page_number,
            "postdate": postdate,
            "uid": uid,
            "username": username,
        }
        flags, notes, blocks_candidate = _content_rule_notes(anjia_text)
        record_id = f"anjia_{len(records) + 1:04d}_lou_{lou}_pid_{pid}"
        records.append(
            {
                "id": record_id,
                "accepted_candidate": not blocks_candidate,
                "duplicate_policy": "keep_all_first_submission_per_user_is_candidate",
                "user_submission_index": 1,
                "source_post": source_post,
                "anjia_text": anjia_text,
                "plain_text": plain,
                "raw_content": content,
                "rule_refs": rule_refs,
                "flags": flags,
                "notes": " ".join(notes),
            }
        )

    records.sort(
        key=lambda item: (
            item["source_post"]["postdate"],
            item["source_post"]["lou"],
            item["source_post"]["pid"],
        )
    )

    submissions_by_uid: defaultdict[int, int] = defaultdict(int)
    for index, record in enumerate(records, start=1):
        uid = record["source_post"]["uid"]
        submissions_by_uid[uid] += 1
        user_submission_index = submissions_by_uid[uid]
        record["id"] = (
            f"anjia_{index:04d}_lou_{record['source_post']['lou']}_"
            f"pid_{record['source_post']['pid']}"
        )
        record["user_submission_index"] = user_submission_index
        if user_submission_index > 1:
            record["accepted_candidate"] = False
            record["flags"].append("duplicate_user_submission")
            duplicate_note = "同一用户的后续投稿；按规则首条为采纳候选。"
            if record["notes"]:
                record["notes"] = f"{record['notes']} {duplicate_note}"
            else:
                record["notes"] = duplicate_note

    return records


def _clear_old_records() -> None:
    if not ANJIA_DIR.exists():
        return
    for path in ANJIA_DIR.glob("anjia_*.json"):
        path.unlink()


def main() -> None:
    rules = _read_json(RULES_PATH)
    records = _build_records(rules)

    _clear_old_records()
    for record in records:
        _write_json(ANJIA_DIR / f"{record['id']}.json", record)

    _write_json(
        SUMMARY_PATH,
        {
            "rules_version": rules.get("version"),
            "record_count": len(records),
            "accepted_candidate_count": sum(
                1 for record in records if record["accepted_candidate"]
            ),
            "duplicate_count": sum(
                1 for record in records if not record["accepted_candidate"]
            ),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        },
    )

    print(f"已提取{len(records)}条安价投稿。")
    print(f"逐条JSON已写入：{ANJIA_DIR}")
    print(f"摘要已写入：{SUMMARY_PATH}")


if __name__ == "__main__":
    main()

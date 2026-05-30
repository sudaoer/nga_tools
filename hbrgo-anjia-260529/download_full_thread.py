from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData

WORK_DIR = Path(__file__).resolve().parent
RULES_PATH = WORK_DIR / "rules.json"
THREAD_JSON_DIR = WORK_DIR / "thread_json"
META_PATH = WORK_DIR / "thread_meta.json"
FULL_REFRESH_FROM_PAGE = 2515


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


def _page_path(page_number: int) -> Path:
    return THREAD_JSON_DIR / f"page_{page_number}.json"


def _existing_page_numbers() -> set[int]:
    if not THREAD_JSON_DIR.exists():
        return set()

    page_numbers: set[int] = set()
    for path in THREAD_JSON_DIR.iterdir():
        if not path.is_file():
            continue
        stem = path.stem
        if not stem.startswith("page_"):
            continue
        page_part = stem.removeprefix("page_")
        if page_part.isdecimal():
            page_numbers.add(int(page_part))
    return page_numbers


def _thread_id_from_rules() -> int:
    rules = _read_json(RULES_PATH)
    thread = rules.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("rules.json缺少thread配置。")
    tid = thread.get("tid")
    if type(tid) is not int:
        raise ValueError("rules.json中的thread.tid必须是整数。")
    return tid


def main() -> None:
    tid = _thread_id_from_rules()
    client = NGAClient()
    page_count = client.get_page_count(tid, None)

    existing_pages = _existing_page_numbers()
    if existing_pages:
        tail_start = min(max(existing_pages), page_count)
    else:
        tail_start = 1

    refresh_start = min(tail_start, FULL_REFRESH_FROM_PAGE)
    missing_pages = set(range(1, page_count + 1)) - existing_pages
    refresh_pages = set(range(refresh_start, page_count + 1)) | missing_pages

    print(
        f"准备下载hbrgo全贴：远端{page_count}页，本地{len(existing_pages)}页，"
        f"从第{refresh_start}页起刷新，需获取{len(refresh_pages)}页。",
        flush=True,
    )
    for page_number in sorted(refresh_pages):
        print(f"正在获取第{page_number}页...", flush=True)
        page_data: PageData = client.get_page(tid, None, page_number)
        _write_json(_page_path(page_number), page_data)

    _write_json(
        META_PATH,
        {
            "tid": tid,
            "aid": None,
            "source": "nga full thread",
            "total_pages": page_count,
            "local_pages": sorted(_existing_page_numbers()),
            "refreshed_pages": sorted(refresh_pages),
            "full_refresh_from_page": FULL_REFRESH_FROM_PAGE,
            "actual_refresh_start": refresh_start,
            "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "rules_file": str(RULES_PATH.name),
        },
    )
    print(f"全贴JSON已写入：{THREAD_JSON_DIR}", flush=True)
    print(f"下载元数据已写入：{META_PATH}", flush=True)


if __name__ == "__main__":
    main()

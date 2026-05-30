from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TypedDict, cast

from nga_tools import utils
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData

FLOOR_MAP_FILENAME = "floor_map.json"
PAGE_JSON_RE = re.compile(r"^page_(\d+)\.json$")
MISSING_POST_HTML = "<p><em>本楼层内容缺失。</em></p>"


class AuthorPostRef(TypedDict):
    pid: int
    author_lou: int


class FloorMapEntry(TypedDict):
    pid: int
    author_lou: int
    original_lou: int


@dataclass(frozen=True)
class FloorLabels:
    original_lou_by_author_lou: dict[int, int]
    show_original: bool

    @classmethod
    def plain(cls) -> "FloorLabels":
        return cls(original_lou_by_author_lou={}, show_original=False)

    def label(self, author_lou: int) -> str:
        if not self.show_original:
            return f"第{author_lou}楼"

        original_lou = self.original_lou_by_author_lou.get(author_lou)
        if original_lou is None:
            return f"第{author_lou}楼（原楼层未知）"

        return f"第{author_lou}楼（原{original_lou}楼）"


def get_floor_map_path(tid: int, aid: int) -> Path:
    return Path(utils.get_folder(tid, aid)) / FLOOR_MAP_FILENAME


def is_missing_post_html(html_content: str) -> bool:
    return html_content.strip() == MISSING_POST_HTML


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"JSON文件顶层必须是对象：{path}")

    data = cast(dict[object, object], raw_data)
    if not all(isinstance(key, str) for key in data):
        raise ValueError(f"JSON对象的键必须都是字符串：{path}")

    return cast(dict[str, object], data)


def _required_int(data: dict[str, object], key: str, source: Path) -> int:
    value = data.get(key)
    if type(value) is int:
        return value
    raise ValueError(f"{source} 缺少整数字段：{key}")


def _page_post_refs(page_data: PageData, source: str) -> list[tuple[int, int]]:
    raw_posts = page_data.get("result")
    if not isinstance(raw_posts, list):
        raise ValueError(f"{source} 缺少帖子列表。")

    posts: list[tuple[int, int]] = []
    for raw_post in cast(list[object], raw_posts):
        if not isinstance(raw_post, dict):
            raise ValueError(f"{source} 中的帖子不是对象：{raw_post!r}")
        post = cast(dict[str, object], raw_post)
        pid = post.get("pid")
        lou = post.get("lou")
        if type(pid) is not int or type(lou) is not int:
            raise ValueError(f"{source} 中的帖子pid/lou字段无效：{raw_post!r}")
        posts.append((pid, lou))

    return posts


def _page_json_sort_key(path: Path) -> int:
    match = PAGE_JSON_RE.fullmatch(path.name)
    if not match:
        return 0
    return int(match.group(1))


def read_author_posts_from_json(tid: int, aid: int) -> list[AuthorPostRef]:
    folder_json = Path(utils.get_folder(tid, aid, "json"))
    page_paths = sorted(
        (
            path
            for path in folder_json.iterdir()
            if path.is_file() and PAGE_JSON_RE.fullmatch(path.name)
        ),
        key=_page_json_sort_key,
    )
    if not page_paths:
        raise RuntimeError(
            f"缺少只看作者JSON备份：{folder_json}。请先运行 backup all。"
        )

    author_posts: list[AuthorPostRef] = []
    for path in page_paths:
        page_data = cast(PageData, _read_json_object(path))
        for pid, author_lou in _page_post_refs(page_data, str(path)):
            author_posts.append({"pid": pid, "author_lou": author_lou})

    return author_posts


def _write_floor_map(
    tid: int,
    aid: int,
    entries: Sequence[FloorMapEntry],
) -> None:
    path = get_floor_map_path(tid, aid)
    data = {
        "tid": tid,
        "aid": aid,
        "entries": list(entries),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    print(f"已写入楼层映射：{path}")


def _load_floor_map_entries(path: Path) -> list[FloorMapEntry]:
    data = _read_json_object(path)
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path} 缺少entries列表。")

    entries: list[FloorMapEntry] = []
    for raw_entry in cast(list[object], raw_entries):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{path} 中的楼层映射项不是对象：{raw_entry!r}")
        entry = cast(dict[str, object], raw_entry)
        entries.append(
            {
                "pid": _required_int(entry, "pid", path),
                "author_lou": _required_int(entry, "author_lou", path),
                "original_lou": _required_int(entry, "original_lou", path),
            }
        )

    return entries


def load_floor_labels(tid: int, aid: Optional[int]) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()

    path = get_floor_map_path(tid, aid)
    if not path.exists():
        raise RuntimeError(
            f"缺少楼层映射文件：{path}。请先运行 backup floors 生成floor_map.json。"
        )

    original_lou_by_author_lou: dict[int, int] = {}
    for entry in _load_floor_map_entries(path):
        original_lou_by_author_lou[entry["author_lou"]] = entry["original_lou"]

    return FloorLabels(
        original_lou_by_author_lou=original_lou_by_author_lou,
        show_original=True,
    )


def validate_floor_labels(
    floor_labels: FloorLabels,
    html_sources_by_lou: dict[int, str],
) -> None:
    if not floor_labels.show_original:
        return

    missing_lous = [
        lou
        for lou, html_content in sorted(html_sources_by_lou.items())
        if lou not in floor_labels.original_lou_by_author_lou
        and not is_missing_post_html(html_content)
    ]
    if not missing_lous:
        return

    preview = ", ".join(str(lou) for lou in missing_lous[:10])
    if len(missing_lous) > 10:
        preview += ", ..."
    raise RuntimeError(
        f"楼层映射缺少{len(missing_lous)}个非缺失楼层：{preview}。"
        "请先运行 backup floors 刷新floor_map.json。"
    )


def build_and_save_floor_map(
    client: NGAClient,
    tid: int,
    aid: int,
    author_posts: Sequence[AuthorPostRef],
) -> FloorLabels:
    if not author_posts:
        raise RuntimeError("没有可用于生成楼层映射的只看作者帖子。")

    print(f"准备生成楼层映射：只看作者{len(author_posts)}楼。", flush=True)

    pid_to_author_lous: dict[int, list[int]] = {}
    author_lou_to_pid: dict[int, int] = {}
    for post in author_posts:
        pid = post["pid"]
        author_lou = post["author_lou"]
        pid_to_author_lous.setdefault(pid, []).append(author_lou)
        author_lou_to_pid[author_lou] = pid

    found_original_by_author_lou: dict[int, int] = {}
    page_count = client.get_page_count(tid, None)
    print(
        f"开始生成楼层映射：只看作者{len(author_posts)}楼，"
        f"扫描原帖{page_count}页。",
        flush=True,
    )

    for page_number in range(1, page_count + 1):
        if page_number == 1 or page_number % 50 == 0:
            print(
                f"正在扫描原帖第{page_number}/{page_count}页，"
                f"已匹配{len(found_original_by_author_lou)}/{len(author_posts)}楼...",
                flush=True,
            )

        page_data = client.get_page(tid, None, page_number)
        for pid, original_lou in _page_post_refs(page_data, f"原帖第{page_number}页"):
            for author_lou in pid_to_author_lous.get(pid, []):
                found_original_by_author_lou[author_lou] = original_lou

        if len(found_original_by_author_lou) == len(author_posts):
            break

    missing_author_lous = sorted(
        author_lou
        for author_lou in author_lou_to_pid
        if author_lou not in found_original_by_author_lou
    )
    if missing_author_lous:
        preview = ", ".join(str(lou) for lou in missing_author_lous[:10])
        if len(missing_author_lous) > 10:
            preview += ", ..."
        raise RuntimeError(
            f"有{len(missing_author_lous)}个只看作者楼层未找到原帖楼层：{preview}。"
        )

    entries: list[FloorMapEntry] = []
    for author_lou in sorted(author_lou_to_pid):
        entries.append(
            {
                "pid": author_lou_to_pid[author_lou],
                "author_lou": author_lou,
                "original_lou": found_original_by_author_lou[author_lou],
            }
        )

    _write_floor_map(tid, aid, entries)
    return FloorLabels(
        original_lou_by_author_lou=found_original_by_author_lou,
        show_original=True,
    )


def generate_floor_map_from_backup(tid: int, aid: Optional[int]) -> None:
    if aid is None:
        print("未指定aid，原帖楼层与当前楼层一致，无需生成floor_map.json。")
        return

    author_posts = read_author_posts_from_json(tid, aid)
    build_and_save_floor_map(NGAClient(), tid, aid, author_posts)

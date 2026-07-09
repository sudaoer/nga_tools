from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, cast

from nga_tools import utils
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.console import report_info, report_progress, report_warning
from nga_tools.core.atomic import write_json_atomically
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData
from nga_tools.backup.floor_models import (
    FLOOR_MAP_FILENAME,
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    MISSING_POST_HTML,
    ORIGINAL_POSTS_PER_PAGE,
    AuthorPostRef,
    FloorLabels,
    FloorMapBuildResult,
    FloorMapEntry,
    MissingOriginalInference,
    OriginalPostSnapshot,
    RecoveredMissingPost,
)


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


def _optional_int(data: dict[str, object], key: str, source: Path) -> Optional[int]:
    value = data.get(key)
    if value is None:
        return None
    if type(value) is int:
        return value
    raise ValueError(f"{source} 字段必须是整数或null：{key}")


def _optional_int_list(
    data: dict[str, object],
    key: str,
    source: Path,
) -> list[int]:
    value = data.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{source} 字段必须是整数列表：{key}")

    result: list[int] = []
    for item in cast(list[object], value):
        if type(item) is not int:
            raise ValueError(f"{source} 字段必须是整数列表：{key}")
        result.append(item)
    return result


def _page_post_dicts(
    page_data: PageData,
    source: str,
    *,
    allow_missing_posts: bool = False,
) -> list[dict[str, object]]:
    raw_posts = page_data.get("result")
    if raw_posts is None and allow_missing_posts:
        report_warning(f"{source} 缺少帖子列表，按空页处理。")
        return []

    if not isinstance(raw_posts, list):
        raise ValueError(f"{source} 缺少帖子列表。")

    posts: list[dict[str, object]] = []
    for raw_post in cast(list[object], raw_posts):
        if not isinstance(raw_post, dict):
            raise ValueError(f"{source} 中的帖子不是对象：{raw_post!r}")
        post = cast(dict[str, object], raw_post)
        pid = post.get("pid")
        lou = post.get("lou")
        if type(pid) is not int or type(lou) is not int:
            raise ValueError(f"{source} 中的帖子pid/lou字段无效：{raw_post!r}")
        posts.append(post)

    return posts


def _post_author_uid(post: dict[str, object]) -> Optional[int]:
    author = post.get("author")
    if not isinstance(author, dict):
        return None
    uid = cast(dict[str, object], author).get("uid")
    if type(uid) is int:
        return uid
    return None


def _post_content(post: dict[str, object]) -> str:
    content = post.get("content")
    if isinstance(content, str):
        return content
    return ""


def read_author_posts_from_archive(tid: int, aid: int) -> list[AuthorPostRef]:
    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    return ThreadArchiveStore(thread_folder).read_latest_author_post_refs()


def read_unresolved_missing_author_lous_from_floor_map(
    tid: int,
    aid: int,
    *,
    present_lous: set[int] | None = None,
    total_lou_count: int | None = None,
) -> list[int]:
    path = get_floor_map_path(tid, aid)
    if not path.exists():
        return []

    try:
        entries = _load_floor_map_entries(path)
    except (FileNotFoundError, ValueError) as error:
        report_warning(f"楼层映射缺失楼缓存无效，忽略：{path}: {error}")
        return []

    missing_lous: set[int] = set()
    for entry in entries:
        author_lou = entry["author_lou"]
        if entry["pid"] is not None:
            continue
        if "original_pid" in entry:
            continue
        if present_lous is not None and author_lou in present_lous:
            continue
        if total_lou_count is not None and author_lou > total_lou_count:
            continue
        missing_lous.add(author_lou)

    return sorted(missing_lous)


def find_missing_author_lous(
    author_posts: Sequence[AuthorPostRef],
    total_lou_count: int | None = None,
) -> list[int]:
    author_lous = sorted({post["author_lou"] for post in author_posts})
    expected_lou = 1
    missing_lous: list[int] = []
    for author_lou in author_lous:
        if author_lou != expected_lou:
            missing_lous.extend(range(expected_lou, author_lou))
            expected_lou = author_lou
        expected_lou += 1

    if total_lou_count is not None and expected_lou <= total_lou_count:
        missing_lous.extend(range(expected_lou, total_lou_count + 1))

    return missing_lous


def _write_floor_map(
    tid: int,
    aid: int,
    entries: Sequence[FloorMapEntry],
    *,
    input_signature: str | None = None,
) -> None:
    path = get_floor_map_path(tid, aid)
    data: dict[str, object] = {
        "version": FLOOR_MAP_VERSION,
        "floor_map_generation_version": FLOOR_MAP_GENERATION_VERSION,
        "algorithm": FLOOR_MAP_HASH_ALGORITHM,
        "tid": tid,
        "aid": aid,
        "entries": list(entries),
    }
    if input_signature is not None:
        data["input_signature"] = input_signature
    write_json_atomically(path, data, indent=4)
    report_info(f"已写入楼层映射：{path}")


def _load_floor_map_entries_from_data(
    data: dict[str, object],
    path: Path,
) -> list[FloorMapEntry]:
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ValueError(f"{path} 缺少entries列表。")

    entries: list[FloorMapEntry] = []
    for raw_entry in cast(list[object], raw_entries):
        if not isinstance(raw_entry, dict):
            raise ValueError(f"{path} 中的楼层映射项不是对象：{raw_entry!r}")
        entry = cast(dict[str, object], raw_entry)
        floor_map_entry: FloorMapEntry = {
            "pid": _optional_int(entry, "pid", path),
            "author_lou": _required_int(entry, "author_lou", path),
            "original_lou": _optional_int(entry, "original_lou", path),
        }
        original_pid = _optional_int(entry, "original_pid", path)
        if original_pid is not None:
            floor_map_entry["original_pid"] = original_pid
        candidate_original_lous = _optional_int_list(
            entry, "candidate_original_lous", path
        )
        if candidate_original_lous:
            floor_map_entry["candidate_original_lous"] = candidate_original_lous
        entries.append(floor_map_entry)

    return entries


def _load_floor_map_entries(path: Path) -> list[FloorMapEntry]:
    return _load_floor_map_entries_from_data(_read_json_object(path), path)


def _floor_labels_from_entries(entries: Sequence[FloorMapEntry]) -> FloorLabels:
    original_lou_by_author_lou: dict[int, int] = {}
    candidate_original_lous_by_author_lou: dict[int, list[int]] = {}
    for entry in entries:
        original_lou = entry["original_lou"]
        if original_lou is not None:
            original_lou_by_author_lou[entry["author_lou"]] = original_lou
            continue

        candidate_original_lous = entry.get("candidate_original_lous")
        if candidate_original_lous:
            candidate_original_lous_by_author_lou[entry["author_lou"]] = (
                candidate_original_lous
            )

    return FloorLabels(
        original_lou_by_author_lou=original_lou_by_author_lou,
        candidate_original_lous_by_author_lou=candidate_original_lous_by_author_lou,
        show_original=True,
    )


def load_floor_labels(tid: int, aid: Optional[int]) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()

    path = get_floor_map_path(tid, aid)
    if not path.exists():
        raise RuntimeError(
            f"缺少楼层映射文件：{path}。请先运行 backup floors 生成floor_map.json。"
        )

    return _floor_labels_from_entries(_load_floor_map_entries(path))


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


def floor_map_input_signature(
    author_posts: Sequence[AuthorPostRef],
    missing_author_lous: Sequence[int],
) -> str:
    payload = {
        "floor_map_generation_version": FLOOR_MAP_GENERATION_VERSION,
        "author_posts": [
            [post["author_lou"], post["pid"]]
            for post in sorted(author_posts, key=lambda item: item["author_lou"])
        ],
        "missing_author_lous": sorted(set(missing_author_lous)),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def load_floor_map_build_result_if_current(
    tid: int,
    aid: int,
    author_posts: Sequence[AuthorPostRef],
    missing_author_lous: Sequence[int],
) -> FloorMapBuildResult | None:
    path = get_floor_map_path(tid, aid)
    if not path.exists():
        return None

    try:
        data = _read_json_object(path)
    except (FileNotFoundError, ValueError) as error:
        report_warning(f"楼层映射缓存无效，重新生成：{path}: {error}")
        return None
    if data.get("version") != FLOOR_MAP_VERSION:
        return None
    if data.get("floor_map_generation_version") != FLOOR_MAP_GENERATION_VERSION:
        return None
    if data.get("algorithm") != FLOOR_MAP_HASH_ALGORITHM:
        return None
    if data.get("input_signature") != floor_map_input_signature(
        author_posts,
        missing_author_lous,
    ):
        return None

    entries = _load_floor_map_entries_from_data(data, path)
    return FloorMapBuildResult(
        floor_labels=_floor_labels_from_entries(entries),
        recovered_missing_posts_by_author_lou={},
    )


def build_and_save_floor_map(
    client: NGAClient,
    tid: int,
    aid: int,
    author_posts: Sequence[AuthorPostRef],
    missing_author_lous: Sequence[int],
    *,
    strict: bool = True,
) -> FloorMapBuildResult:
    if not author_posts:
        raise RuntimeError("没有可用于生成楼层映射的只看作者帖子。")

    report_progress(f"准备生成楼层映射：只看作者{len(author_posts)}楼")
    input_signature = floor_map_input_signature(author_posts, missing_author_lous)

    author_lou_to_pid: dict[int, int] = {}
    for post in author_posts:
        pid = post["pid"]
        author_lou = post["author_lou"]
        author_lou_to_pid[author_lou] = pid

    found_original_by_author_lou, existing_missing_originals, existing_candidates = (
        _load_reusable_floor_map(
            tid,
            aid,
            author_lou_to_pid,
            set(missing_author_lous),
        )
    )
    original_lou_by_author_lou = {
        **found_original_by_author_lou,
        **existing_missing_originals,
    }
    seen_original_lous: set[int] = set(found_original_by_author_lou.values())
    original_posts_by_lou: dict[int, OriginalPostSnapshot] = {}
    scanned_pages: set[int] = set()

    pending_author_lous = sorted(
        author_lou
        for author_lou in author_lou_to_pid
        if author_lou not in found_original_by_author_lou
    )
    if pending_author_lous:
        page_count = client.get_page_count(tid, None)
        pages_to_scan = _pages_for_pending_author_lous(
            pending_author_lous,
            original_lou_by_author_lou,
            page_count,
        )
        report_progress(
            f"增量扫描原帖{len(pages_to_scan)}页，"
            f"匹配{len(pending_author_lous)}个未映射楼层",
            completed=0,
            total=len(pages_to_scan),
        )
        _scan_original_pages(
            client,
            tid,
            pages_to_scan,
            scanned_pages,
            seen_original_lous,
            original_posts_by_lou,
            _pid_to_author_lous_for_author_lous(
                pending_author_lous,
                author_lou_to_pid,
            ),
            original_lou_by_author_lou,
            len(author_posts),
        )
        found_original_by_author_lou = {
            author_lou: original_lou_by_author_lou[author_lou]
            for author_lou in author_lou_to_pid
            if author_lou in original_lou_by_author_lou
        }
    else:
        report_info("已有楼层映射覆盖所有非缺失楼层。")

    unmapped_author_lous = sorted(
        author_lou
        for author_lou in author_lou_to_pid
        if author_lou not in original_lou_by_author_lou
    )
    if unmapped_author_lous:
        preview = ", ".join(str(lou) for lou in unmapped_author_lous[:10])
        if len(unmapped_author_lous) > 10:
            preview += ", ..."
        message = f"有{len(unmapped_author_lous)}个只看作者楼层未找到原帖楼层：{preview}。"
        if strict:
            raise RuntimeError(message)
        report_warning(message)

    missing_inference = _infer_missing_original_lous(
        client,
        tid,
        original_lou_by_author_lou,
        seen_original_lous,
        original_posts_by_lou,
        scanned_pages,
        missing_author_lous,
        existing_candidates,
    )
    inferred_missing_originals = missing_inference.exact_original_by_author_lou
    candidate_missing_originals = missing_inference.candidate_originals_by_author_lou
    if inferred_missing_originals:
        report_info(f"已补全{len(inferred_missing_originals)}个缺失楼的原帖楼层。")
    if candidate_missing_originals:
        report_info(f"已记录{len(candidate_missing_originals)}个缺失楼的候选原帖楼层。")

    all_original_by_author_lou = {
        **original_lou_by_author_lou,
        **inferred_missing_originals,
    }
    final_candidate_missing_originals = {
        author_lou: candidate_lous
        for author_lou, candidate_lous in candidate_missing_originals.items()
        if author_lou not in all_original_by_author_lou
    }
    exact_missing_originals = {
        author_lou: all_original_by_author_lou[author_lou]
        for author_lou in missing_author_lous
        if author_lou not in author_lou_to_pid
        and author_lou in all_original_by_author_lou
    }
    recovered_missing_posts = _recover_missing_posts_from_original_pages(
        client,
        tid,
        exact_missing_originals,
        scanned_pages,
        seen_original_lous,
        original_posts_by_lou,
    )

    entries: list[FloorMapEntry] = []
    for author_lou in sorted(author_lou_to_pid):
        original_lou = original_lou_by_author_lou.get(author_lou)
        entries.append(
            {
                "pid": author_lou_to_pid[author_lou],
                "author_lou": author_lou,
                "original_lou": original_lou,
            }
        )
    for author_lou in sorted(exact_missing_originals):
        entry: FloorMapEntry = {
            "pid": None,
            "author_lou": author_lou,
            "original_lou": exact_missing_originals[author_lou],
        }
        recovered_post = recovered_missing_posts.get(author_lou)
        if recovered_post is not None:
            entry["original_pid"] = recovered_post["original_pid"]
        entries.append(entry)
    for author_lou in sorted(final_candidate_missing_originals):
        entries.append(
            {
                "pid": None,
                "author_lou": author_lou,
                "original_lou": None,
                "candidate_original_lous": final_candidate_missing_originals[
                    author_lou
                ],
            }
        )
    entries.sort(key=lambda entry: entry["author_lou"])

    _write_floor_map(tid, aid, entries, input_signature=input_signature)
    return FloorMapBuildResult(
        floor_labels=FloorLabels(
            original_lou_by_author_lou=all_original_by_author_lou,
            candidate_original_lous_by_author_lou=final_candidate_missing_originals,
            show_original=True,
        ),
        recovered_missing_posts_by_author_lou=recovered_missing_posts,
    )


def _load_reusable_floor_map(
    tid: int,
    aid: int,
    author_lou_to_pid: dict[int, int],
    missing_author_lous: set[int],
) -> tuple[dict[int, int], dict[int, int], dict[int, list[int]]]:
    path = get_floor_map_path(tid, aid)
    if not path.exists():
        return {}, {}, {}

    original_lou_by_author_lou: dict[int, int] = {}
    missing_original_lou_by_author_lou: dict[int, int] = {}
    candidate_originals_by_author_lou: dict[int, list[int]] = {}
    for entry in _load_floor_map_entries(path):
        author_lou = entry["author_lou"]
        entry_pid = entry["pid"]
        original_lou = entry["original_lou"]

        if author_lou in author_lou_to_pid:
            if entry_pid != author_lou_to_pid[author_lou]:
                continue
            if original_lou is not None:
                original_lou_by_author_lou[author_lou] = original_lou
            continue

        if author_lou not in missing_author_lous or entry_pid is not None:
            continue
        if original_lou is not None:
            missing_original_lou_by_author_lou[author_lou] = original_lou
            continue

        candidate_original_lous = entry.get("candidate_original_lous")
        if candidate_original_lous:
            candidate_originals_by_author_lou[author_lou] = candidate_original_lous

    if (
        original_lou_by_author_lou
        or missing_original_lou_by_author_lou
        or candidate_originals_by_author_lou
    ):
        report_info(
            f"复用已有楼层映射：确定{len(original_lou_by_author_lou)}楼，"
            f"缺失确定{len(missing_original_lou_by_author_lou)}楼，"
            f"候选{len(candidate_originals_by_author_lou)}楼。",
        )
    return (
        original_lou_by_author_lou,
        missing_original_lou_by_author_lou,
        candidate_originals_by_author_lou,
    )


def _original_page_for_lou(lou: int) -> int:
    return max(1, lou // ORIGINAL_POSTS_PER_PAGE + 1)


def _pages_for_original_interval(start_lou: int, end_lou: int) -> list[int]:
    start_page = _original_page_for_lou(start_lou)
    end_page = _original_page_for_lou(end_lou)
    return list(range(start_page, end_page + 1))


def _pages_for_pending_author_lous(
    pending_author_lous: Sequence[int],
    original_lou_by_author_lou: dict[int, int],
    page_count: int,
) -> list[int]:
    if not pending_author_lous:
        return []
    if not original_lou_by_author_lou:
        return list(range(1, page_count + 1))

    mapped_author_lous = sorted(original_lou_by_author_lou)
    pages: set[int] = set()
    chunk_start = pending_author_lous[0]
    previous_lou = chunk_start
    for pending_lou in list(pending_author_lous[1:]) + [-1]:
        if pending_lou != previous_lou + 1:
            chunk_end = previous_lou
            prev_candidates = [lou for lou in mapped_author_lous if lou < chunk_start]
            next_candidates = [lou for lou in mapped_author_lous if lou > chunk_end]
            if prev_candidates:
                start_page = _original_page_for_lou(
                    original_lou_by_author_lou[prev_candidates[-1]]
                )
            else:
                start_page = 1
            if next_candidates:
                end_page = _original_page_for_lou(
                    original_lou_by_author_lou[next_candidates[0]]
                )
            else:
                end_page = page_count
            pages.update(range(start_page, end_page + 1))
            chunk_start = pending_lou
        previous_lou = pending_lou

    return sorted(page for page in pages if 1 <= page <= page_count)


def _pid_to_author_lous_for_author_lous(
    author_lous: Sequence[int],
    author_lou_to_pid: dict[int, int],
) -> dict[int, list[int]]:
    pid_to_author_lous: dict[int, list[int]] = {}
    for author_lou in author_lous:
        pid = author_lou_to_pid[author_lou]
        pid_to_author_lous.setdefault(pid, []).append(author_lou)
    return pid_to_author_lous


def _scan_original_pages(
    client: NGAClient,
    tid: int,
    page_numbers: Sequence[int],
    scanned_pages: set[int],
    seen_original_lous: set[int],
    original_posts_by_lou: Optional[dict[int, OriginalPostSnapshot]],
    pid_to_author_lous: dict[int, list[int]],
    original_lou_by_author_lou: dict[int, int],
    author_post_count: int,
) -> None:
    target_author_lous = {
        author_lou
        for author_lous in pid_to_author_lous.values()
        for author_lou in author_lous
    }
    for index, page_number in enumerate(page_numbers, start=1):
        if page_number in scanned_pages:
            continue
        if target_author_lous:
            matched_count = sum(
                1
                for author_lou in target_author_lous
                if author_lou in original_lou_by_author_lou
            )
            progress_text = f"已匹配{matched_count}/{author_post_count}楼"
        else:
            progress_text = "正在收集原帖楼层信息"
        report_progress(
            f"正在扫描原帖第{page_number}页，{progress_text}",
            completed=index - 1,
            total=len(page_numbers),
        )

        page_data = client.get_page(tid, None, page_number)
        scanned_pages.add(page_number)
        for post in _page_post_dicts(
            page_data,
            f"原帖第{page_number}页",
            allow_missing_posts=True,
        ):
            pid = cast(int, post["pid"])
            original_lou = cast(int, post["lou"])
            seen_original_lous.add(original_lou)
            if original_posts_by_lou is not None:
                original_posts_by_lou[original_lou] = {
                    "pid": pid,
                    "lou": original_lou,
                    "author_uid": _post_author_uid(post),
                    "content": _post_content(post),
                }
            for author_lou in pid_to_author_lous.get(pid, []):
                original_lou_by_author_lou[author_lou] = original_lou
    report_progress(
        "原帖扫描完成",
        completed=len(page_numbers),
        total=len(page_numbers),
    )


def _recover_missing_posts_from_original_pages(
    client: NGAClient,
    tid: int,
    exact_missing_originals: dict[int, int],
    scanned_pages: set[int],
    seen_original_lous: set[int],
    original_posts_by_lou: dict[int, OriginalPostSnapshot],
) -> dict[int, RecoveredMissingPost]:
    if not exact_missing_originals:
        return {}

    pages_to_scan = [
        page
        for page in sorted(
            {
                _original_page_for_lou(original_lou)
                for original_lou in exact_missing_originals.values()
            }
        )
        if page not in scanned_pages
    ]
    if pages_to_scan:
        _scan_original_pages(
            client,
            tid,
            pages_to_scan,
            scanned_pages,
            seen_original_lous,
            original_posts_by_lou,
            {},
            {},
            0,
        )

    recovered: dict[int, RecoveredMissingPost] = {}
    for author_lou, original_lou in exact_missing_originals.items():
        original_post = original_posts_by_lou.get(original_lou)
        if original_post is None or original_post["author_uid"] != -1:
            continue
        recovered[author_lou] = {
            "original_pid": original_post["pid"],
            "original_lou": original_lou,
            "content": original_post["content"],
        }

    if recovered:
        report_info(f"已从匿名原帖恢复{len(recovered)}个缺失楼内容。")
    return recovered


def _possible_candidates_by_position(
    author_gap_lous: Sequence[int],
    original_gap_lous: Sequence[int],
) -> dict[int, list[int]]:
    candidates_by_author_lou: dict[int, list[int]] = {}
    author_count = len(author_gap_lous)
    original_count = len(original_gap_lous)
    for index, author_lou in enumerate(author_gap_lous):
        start = index
        end = original_count - (author_count - index) + 1
        candidates_by_author_lou[author_lou] = list(original_gap_lous[start:end])
    return candidates_by_author_lou


def _infer_missing_original_lous(
    client: NGAClient,
    tid: int,
    original_lou_by_author_lou: dict[int, int],
    seen_original_lous: set[int],
    original_posts_by_lou: dict[int, OriginalPostSnapshot],
    scanned_pages: set[int],
    missing_author_lous: Sequence[int],
    existing_candidates: dict[int, list[int]],
) -> MissingOriginalInference:
    inferred: dict[int, int] = {}
    candidates: dict[int, list[int]] = {}
    processed_missing_lous: set[int] = set()
    if not missing_author_lous:
        return MissingOriginalInference(inferred, existing_candidates)

    mapped_author_lous = sorted(original_lou_by_author_lou)
    if not mapped_author_lous:
        return MissingOriginalInference(inferred, existing_candidates)

    missing_lous = sorted(set(missing_author_lous))
    for missing_lou in missing_lous:
        if missing_lou in original_lou_by_author_lou or missing_lou in inferred:
            continue

        prev_candidates = [lou for lou in mapped_author_lous if lou < missing_lou]
        next_candidates = [lou for lou in mapped_author_lous if lou > missing_lou]
        if not prev_candidates or not next_candidates:
            existing_candidate_lous = existing_candidates.get(missing_lou)
            if existing_candidate_lous:
                candidates[missing_lou] = existing_candidate_lous
            report_warning(f"无法推断第{missing_lou}楼的原帖楼层。")
            continue

        prev_author_lou = prev_candidates[-1]
        next_author_lou = next_candidates[0]
        author_gap_lous = [
            lou for lou in missing_lous if prev_author_lou < lou < next_author_lou
            and lou not in original_lou_by_author_lou
            and lou not in inferred
        ]
        if not author_gap_lous:
            continue
        processed_missing_lous.update(author_gap_lous)

        prev_original_lou = original_lou_by_author_lou[prev_author_lou]
        next_original_lou = original_lou_by_author_lou[next_author_lou]
        pages_to_scan = [
            page
            for page in _pages_for_original_interval(prev_original_lou, next_original_lou)
            if page not in scanned_pages
        ]
        if pages_to_scan:
            _scan_original_pages(
                client,
                tid,
                pages_to_scan,
                scanned_pages,
                seen_original_lous,
                original_posts_by_lou,
                {},
                original_lou_by_author_lou,
                len(original_lou_by_author_lou),
            )

        original_gap_lous = [
            lou
            for lou in range(prev_original_lou + 1, next_original_lou)
            if lou not in seen_original_lous
        ]
        anonymous_original_lous = [
            lou
            for lou in range(prev_original_lou + 1, next_original_lou)
            if (
                (original_post := original_posts_by_lou.get(lou)) is not None
                and original_post["author_uid"] == -1
            )
        ]
        possible_original_lous = sorted(
            {*original_gap_lous, *anonymous_original_lous}
        )

        if len(author_gap_lous) != len(possible_original_lous):
            possible_candidates = _possible_candidates_by_position(
                author_gap_lous,
                possible_original_lous,
            )
            report_warning(
                f"无法唯一推断第{missing_lou}楼的原帖楼层，"
                f"只看作者缺失{len(author_gap_lous)}楼，"
                f"原帖区间缺失{len(original_gap_lous)}楼，"
                f"匿名候选{len(anonymous_original_lous)}楼。"
            )
            for author_lou, candidate_lous in possible_candidates.items():
                if candidate_lous:
                    candidates[author_lou] = candidate_lous
            continue

        for author_lou, original_lou in zip(author_gap_lous, possible_original_lous):
            inferred[author_lou] = original_lou

    for author_lou, candidate_lous in existing_candidates.items():
        if (
            author_lou not in original_lou_by_author_lou
            and author_lou not in inferred
            and author_lou not in candidates
            and author_lou not in processed_missing_lous
        ):
            candidates[author_lou] = candidate_lous

    return MissingOriginalInference(inferred, candidates)


def generate_floor_map_from_backup(tid: int, aid: Optional[int]) -> None:
    if aid is None:
        report_info("未指定aid，原帖楼层与当前楼层一致，无需生成floor_map.json。")
        return

    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    author_posts = archive_store.read_latest_author_post_refs()
    author_total_lou_count = archive_store.read_latest_author_total_lou_count()
    present_lous = {post["author_lou"] for post in author_posts}
    missing_author_lous = sorted(
        {
            *find_missing_author_lous(author_posts, author_total_lou_count),
            *read_unresolved_missing_author_lous_from_floor_map(
                tid,
                aid,
                present_lous=present_lous,
                total_lou_count=author_total_lou_count,
            ),
        }
    )
    build_and_save_floor_map(
        NGAClient(),
        tid,
        aid,
        author_posts,
        missing_author_lous,
    )

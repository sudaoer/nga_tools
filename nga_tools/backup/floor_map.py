from __future__ import annotations

import json
import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

import requests

from nga_tools import utils
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.console import (
    WarningCategory,
    report_info,
    report_progress,
    report_warning,
)
from nga_tools.network_limits import get_api_concurrency
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import record_timing_label, record_timing_metric, time_section
from nga_tools.backup.floor_models import (
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
    StoredFloorMap,
)


def is_missing_post_html(html_content: str) -> bool:
    return html_content.strip() == MISSING_POST_HTML


def _page_post_dicts(
    page_data: PageData,
    source: str,
    *,
    allow_missing_posts: bool = False,
) -> list[dict[str, object]]:
    raw_posts = page_data.get("result")
    if raw_posts is None and allow_missing_posts:
        report_warning(
            WarningCategory.POST_CONTENT,
            f"{source} 缺少帖子列表，按空页处理。",
        )
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


def read_unresolved_missing_author_lous_from_archive(
    archive_store: ThreadArchiveStore,
    *,
    present_lous: set[int] | None = None,
    total_lou_count: int | None = None,
) -> list[int]:
    """Read unresolved missing lous; ``total_lou_count`` is NGA ``vrows`` count."""
    try:
        stored_floor_map = archive_store.read_floor_map()
    except ValueError as error:
        report_warning(
            WarningCategory.FLOOR_MAP,
            f"楼层映射缺失楼缓存无效，忽略：{archive_store.db_path}: {error}"
        )
        return []
    if stored_floor_map is None:
        return []

    missing_lous: set[int] = set()
    for entry in stored_floor_map.entries:
        author_lou = entry["author_lou"]
        if entry["pid"] is not None:
            continue
        if "original_pid" in entry:
            continue
        if present_lous is not None and author_lou in present_lous:
            continue
        if total_lou_count is not None and author_lou >= total_lou_count:
            continue
        missing_lous.add(author_lou)

    return sorted(missing_lous)


def find_missing_author_lous(
    author_posts: Sequence[AuthorPostRef],
    total_lou_count: int | None = None,
) -> list[int]:
    """Find missing author lous; ``total_lou_count`` is NGA ``vrows`` count."""
    author_lous = sorted({post["author_lou"] for post in author_posts})
    expected_lou = 1
    missing_lous: list[int] = []
    for author_lou in author_lous:
        if author_lou != expected_lou:
            missing_lous.extend(range(expected_lou, author_lou))
            expected_lou = author_lou
        expected_lou += 1

    if total_lou_count is not None:
        # NGA author lous are 0-based; vrows is a row count, not the max lou.
        last_author_lou = total_lou_count - 1
        if last_author_lou >= 0 and expected_lou <= last_author_lou:
            missing_lous.extend(range(expected_lou, last_author_lou + 1))

    return missing_lous


def _write_floor_map(
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: int,
    entries: Sequence[FloorMapEntry],
    *,
    input_signature: str,
) -> None:
    archive_store.replace_floor_map(
        StoredFloorMap(
            version=FLOOR_MAP_VERSION,
            generation_version=FLOOR_MAP_GENERATION_VERSION,
            algorithm=FLOOR_MAP_HASH_ALGORITHM,
            tid=tid,
            aid=aid,
            input_signature=input_signature,
            entries=list(entries),
        )
    )
    report_info(f"已写入楼层映射：{archive_store.db_path}")


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


def load_floor_labels_from_archive(
    archive_store: ThreadArchiveStore,
    aid: Optional[int],
) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()

    stored_floor_map = archive_store.read_floor_map()
    if stored_floor_map is None:
        raise RuntimeError(
            f"archive.sqlite3缺少楼层映射：{archive_store.db_path}。"
            "请使用--name或--tid指定该帖子，运行backup sub "
            "--force-processing刷新备份。"
        )

    return _floor_labels_from_entries(stored_floor_map.entries)


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
        "请使用--name或--tid指定该帖子，运行backup sub "
        "--force-processing刷新备份。"
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
    archive_store: ThreadArchiveStore,
    author_posts: Sequence[AuthorPostRef],
    missing_author_lous: Sequence[int],
) -> FloorMapBuildResult | None:
    try:
        stored_floor_map = archive_store.read_floor_map()
    except ValueError as error:
        report_warning(
            WarningCategory.FLOOR_MAP,
            f"楼层映射缓存无效，重新生成：{archive_store.db_path}: {error}"
        )
        return None
    if stored_floor_map is None:
        return None
    if stored_floor_map.version != FLOOR_MAP_VERSION:
        return None
    if stored_floor_map.generation_version != FLOOR_MAP_GENERATION_VERSION:
        return None
    if stored_floor_map.algorithm != FLOOR_MAP_HASH_ALGORITHM:
        return None
    if stored_floor_map.input_signature != floor_map_input_signature(
        author_posts,
        missing_author_lous,
    ):
        return None

    return FloorMapBuildResult(
        floor_labels=_floor_labels_from_entries(stored_floor_map.entries),
        recovered_missing_posts_by_author_lou={},
    )


def build_and_save_floor_map(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
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
            archive_store,
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
        _scan_pending_author_pages(
            client,
            tid,
            pages_to_scan,
            page_count,
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
        report_warning(WarningCategory.FLOOR_MAP, message)

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

    _write_floor_map(
        archive_store,
        tid,
        aid,
        entries,
        input_signature=input_signature,
    )
    return FloorMapBuildResult(
        floor_labels=FloorLabels(
            original_lou_by_author_lou=all_original_by_author_lou,
            candidate_original_lous_by_author_lou=final_candidate_missing_originals,
            show_original=True,
        ),
        recovered_missing_posts_by_author_lou=recovered_missing_posts,
    )


def _load_reusable_floor_map(
    archive_store: ThreadArchiveStore,
    author_lou_to_pid: dict[int, int],
    missing_author_lous: set[int],
) -> tuple[dict[int, int], dict[int, int], dict[int, list[int]]]:
    stored_floor_map = archive_store.read_floor_map()
    if stored_floor_map is None:
        return {}, {}, {}

    original_lou_by_author_lou: dict[int, int] = {}
    missing_original_lou_by_author_lou: dict[int, int] = {}
    candidate_originals_by_author_lou: dict[int, list[int]] = {}
    for entry in stored_floor_map.entries:
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


@dataclass(frozen=True, slots=True)
class _OriginalPageApplication:
    post_pids: frozenset[int]
    newly_matched_author_lous: int


def _apply_original_page(
    page_data: PageData,
    page_number: int,
    seen_original_lous: set[int],
    original_posts_by_lou: Optional[dict[int, OriginalPostSnapshot]],
    pid_to_author_lous: dict[int, list[int]],
    original_lou_by_author_lou: dict[int, int],
) -> _OriginalPageApplication:
    post_pids: set[int] = set()
    newly_matched_author_lous = 0
    for post in _page_post_dicts(
        page_data,
        f"原帖第{page_number}页",
        allow_missing_posts=True,
    ):
        pid = cast(int, post["pid"])
        original_lou = cast(int, post["lou"])
        post_pids.add(pid)
        seen_original_lous.add(original_lou)
        if original_posts_by_lou is not None:
            original_posts_by_lou[original_lou] = {
                "pid": pid,
                "lou": original_lou,
                "author_uid": _post_author_uid(post),
                "content": _post_content(post),
                "raw_post": dict(post),
            }
        for author_lou in pid_to_author_lous.get(pid, []):
            if author_lou not in original_lou_by_author_lou:
                newly_matched_author_lous += 1
            original_lou_by_author_lou[author_lou] = original_lou
    return _OriginalPageApplication(
        post_pids=frozenset(post_pids),
        newly_matched_author_lous=newly_matched_author_lous,
    )


def _record_pending_scan_metrics(
    *,
    sequential_pages: int,
    zero_hit_pages: int,
    pid_requests: int,
    pid_hits: int,
    skipped_pages: int,
    recovered_author_lous: int,
    peak_page_recovery: int,
    fallback_reason: str,
) -> None:
    record_timing_metric("楼层映射顺序原帖页数", sequential_pages)
    record_timing_metric("楼层映射零命中原帖页数", zero_hit_pages)
    record_timing_metric("楼层映射PID定位请求数", pid_requests)
    record_timing_metric("楼层映射PID定位命中数", pid_hits)
    record_timing_metric("楼层映射PID跳过页数", skipped_pages)
    record_timing_metric("楼层映射本次恢复作者楼数", recovered_author_lous)
    record_timing_metric("楼层映射单页最多恢复作者楼数", peak_page_recovery)
    record_timing_metric("楼层映射PID定位回退数", int(fallback_reason != "none"))
    record_timing_label("楼层映射PID定位结果", fallback_reason)


def _scan_pending_author_pages(
    client: NGAClient,
    tid: int,
    page_numbers: Sequence[int],
    page_count: int,
    scanned_pages: set[int],
    seen_original_lous: set[int],
    original_posts_by_lou: dict[int, OriginalPostSnapshot],
    pid_to_author_lous: dict[int, list[int]],
    original_lou_by_author_lou: dict[int, int],
    author_post_count: int,
) -> None:
    pages_to_scan = list(
        dict.fromkeys(
            page_number
            for page_number in page_numbers
            if page_number not in scanned_pages
        )
    )
    page_index = {
        page_number: index for index, page_number in enumerate(pages_to_scan)
    }
    target_author_lous = {
        author_lou
        for author_lous in pid_to_author_lous.values()
        for author_lou in author_lous
    }
    initially_matched_target_count = sum(
        author_lou in original_lou_by_author_lou
        for author_lou in target_author_lous
    )
    author_lou_to_pid = {
        author_lou: pid
        for pid, author_lous in pid_to_author_lous.items()
        for author_lou in author_lous
    }

    next_scanned_pages = set(scanned_pages)
    next_seen_original_lous = set(seen_original_lous)
    next_original_posts_by_lou = dict(original_posts_by_lou)
    next_original_lou_by_author_lou = dict(original_lou_by_author_lou)
    sequential_pages = 0
    zero_hit_pages = 0
    pid_requests = 0
    pid_hits = 0
    skipped_pages = 0
    recovered_author_lous = 0
    peak_page_recovery = 0
    fallback_reason = "none"
    expected_pid: int | None = None
    next_index = 0
    scan_complete = False

    report_progress(
        f"准备顺序扫描原帖{len(pages_to_scan)}页，"
        f"匹配{len(target_author_lous)}个未映射楼层",
        completed=0,
        total=len(pages_to_scan),
    )

    try:
        with time_section("原帖页面PID跳页扫描"):
            while next_index < len(pages_to_scan) and not scan_complete:
                restart_from_redirect = False
                page_iterator = client.iter_pages(
                    tid,
                    None,
                    pages_to_scan[next_index:],
                )
                try:
                    for page_number, page_data in page_iterator:
                        current_index = page_index[page_number]
                        next_index = current_index + 1
                        next_scanned_pages.add(page_number)
                        sequential_pages += 1
                        application = _apply_original_page(
                            page_data,
                            page_number,
                            next_seen_original_lous,
                            next_original_posts_by_lou,
                            pid_to_author_lous,
                            next_original_lou_by_author_lou,
                        )
                        newly_matched = application.newly_matched_author_lous
                        recovered_author_lous += newly_matched
                        peak_page_recovery = max(peak_page_recovery, newly_matched)

                        if expected_pid is not None:
                            if expected_pid not in application.post_pids:
                                fallback_reason = "target_pid_missing"
                                break
                            pid_hits += 1
                            expected_pid = None

                        newly_resolved_target_count = sum(
                            author_lou in next_original_lou_by_author_lou
                            for author_lou in target_author_lous
                        )
                        matched_count = (
                            author_post_count
                            - len(target_author_lous)
                            + newly_resolved_target_count
                        )
                        report_progress(
                            f"已处理原帖第{page_number}页，"
                            f"已匹配{matched_count}/{author_post_count}楼",
                            completed=current_index + 1,
                            total=len(pages_to_scan),
                        )
                        unresolved_author_lous = sorted(
                            author_lou
                            for author_lou in target_author_lous
                            if author_lou not in next_original_lou_by_author_lou
                        )
                        if not unresolved_author_lous:
                            scan_complete = True
                            break
                        if newly_matched > 0:
                            continue

                        zero_hit_pages += 1
                        next_author_lou = unresolved_author_lous[0]
                        next_pid = author_lou_to_pid[next_author_lou]
                        pid_requests += 1
                        try:
                            target = client.get_pid_redirect_target(next_pid)
                        except requests.RequestException as error:
                            fallback_reason = f"request_error:{type(error).__name__}"
                            break
                        if target is None:
                            fallback_reason = "no_redirect"
                            break
                        if target.tid != tid:
                            fallback_reason = "wrong_thread"
                            break
                        if target.page_number < 1 or target.page_number > page_count:
                            fallback_reason = "invalid_page"
                            break
                        target_index = page_index.get(target.page_number)
                        if target_index is None:
                            fallback_reason = "target_outside_scan"
                            break
                        if target_index <= current_index:
                            fallback_reason = "non_forward_target"
                            break

                        skipped_pages += target_index - current_index - 1
                        expected_pid = next_pid
                        next_index = target_index
                        restart_from_redirect = True
                        break
                finally:
                    page_iterator.close()

                if scan_complete:
                    break
                if fallback_reason != "none":
                    report_warning(
                        WarningCategory.FLOOR_MAP,
                        "PID定位不可用，回退原帖范围扫描："
                        f"tid={tid}，原因={fallback_reason}。",
                    )
                    _scan_original_pages(
                        client,
                        tid,
                        pages_to_scan,
                        next_scanned_pages,
                        next_seen_original_lous,
                        next_original_posts_by_lou,
                        pid_to_author_lous,
                        next_original_lou_by_author_lou,
                        author_post_count,
                    )
                    scan_complete = True
                    break
                if not restart_from_redirect:
                    break
    finally:
        recovered_author_lous = max(
            recovered_author_lous,
            sum(
                author_lou in next_original_lou_by_author_lou
                for author_lou in target_author_lous
            )
            - initially_matched_target_count,
        )
        _record_pending_scan_metrics(
            sequential_pages=sequential_pages,
            zero_hit_pages=zero_hit_pages,
            pid_requests=pid_requests,
            pid_hits=pid_hits,
            skipped_pages=skipped_pages,
            recovered_author_lous=recovered_author_lous,
            peak_page_recovery=peak_page_recovery,
            fallback_reason=fallback_reason,
        )

    scanned_pages.clear()
    scanned_pages.update(next_scanned_pages)
    seen_original_lous.clear()
    seen_original_lous.update(next_seen_original_lous)
    original_posts_by_lou.clear()
    original_posts_by_lou.update(next_original_posts_by_lou)
    original_lou_by_author_lou.clear()
    original_lou_by_author_lou.update(next_original_lou_by_author_lou)
    report_progress(
        "原帖作者楼层扫描完成",
        completed=min(next_index, len(pages_to_scan)),
        total=len(pages_to_scan),
    )


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
    pages_to_scan = list(
        dict.fromkeys(
            page_number
            for page_number in page_numbers
            if page_number not in scanned_pages
        )
    )
    target_author_lous = {
        author_lou
        for author_lous in pid_to_author_lous.values()
        for author_lou in author_lous
    }

    if target_author_lous:
        matched_count = sum(
            1
            for author_lou in target_author_lous
            if author_lou in original_lou_by_author_lou
        )
        progress_text = f"已匹配{matched_count}/{author_post_count}楼"
    else:
        progress_text = "正在收集原帖楼层信息"

    total = len(pages_to_scan)
    report_progress(
        f"准备扫描原帖{total}页，{progress_text}",
        completed=0,
        total=total,
    )

    def report_page_complete(
        page_number: int,
        completed: int,
        page_total: int,
    ) -> None:
        report_progress(
            f"已获取原帖第{page_number}页，{progress_text}",
            completed=completed,
            total=page_total,
        )

    record_timing_metric("原帖页面抓取页数", total)
    record_timing_metric(
        "原帖页面抓取并发上限",
        min(total, get_api_concurrency()),
    )
    next_scanned_pages = set(scanned_pages)
    next_seen_original_lous = set(seen_original_lous)
    next_original_posts_by_lou = (
        None
        if original_posts_by_lou is None
        else dict(original_posts_by_lou)
    )
    next_original_lou_by_author_lou = dict(original_lou_by_author_lou)

    with time_section("原帖页面并发抓取"):
        page_iterator = client.iter_pages(
            tid,
            None,
            pages_to_scan,
            on_page_complete=report_page_complete,
        )
        for page_number, page_data in page_iterator:
            next_scanned_pages.add(page_number)
            if target_author_lous:
                matched_count = sum(
                    1
                    for author_lou in target_author_lous
                    if author_lou in next_original_lou_by_author_lou
                )
                progress_text = f"已匹配{matched_count}/{author_post_count}楼"
            else:
                progress_text = "正在收集原帖楼层信息"
            _apply_original_page(
                page_data,
                page_number,
                next_seen_original_lous,
                next_original_posts_by_lou,
                pid_to_author_lous,
                next_original_lou_by_author_lou,
            )

    scanned_pages.clear()
    scanned_pages.update(next_scanned_pages)
    seen_original_lous.clear()
    seen_original_lous.update(next_seen_original_lous)
    if original_posts_by_lou is not None and next_original_posts_by_lou is not None:
        original_posts_by_lou.clear()
        original_posts_by_lou.update(next_original_posts_by_lou)
    original_lou_by_author_lou.clear()
    original_lou_by_author_lou.update(next_original_lou_by_author_lou)
    report_progress(
        "原帖扫描完成",
        completed=total,
        total=total,
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
            "raw_post": original_post["raw_post"],
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
            report_warning(
                WarningCategory.FLOOR_MAP,
                f"无法推断第{missing_lou}楼的原帖楼层。",
            )
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
                WarningCategory.FLOOR_MAP,
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

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

from nga_tools import utils
from nga_tools.backup.archive_store import (
    ArchivePagesUpsertResult,
    ThreadArchiveStore,
)
from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    find_missing_author_lous,
    load_floor_map_build_result_if_current,
    load_floor_labels_from_archive,
    read_unresolved_missing_author_lous_from_archive,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    PAGE_JSON_RE,
)
from nga_tools.backup.image_pipeline import download_images as _download_images
from nga_tools.backup.image_reference_cache import (
    IMAGE_REFERENCE_EXTRACTOR_VERSION,
    collect_image_download_tasks_for_records as _collect_image_download_tasks_for_records,
)
from nga_tools.backup.image_store import ImageDownloadTask
from nga_tools.backup.models import PostRecord
from nga_tools.backup.page_store import (
    author_total_lou_count_from_page_data as _author_total_lou_count_from_page_data,
    fetch_backup_pages as _fetch_backup_pages,
    fetch_backup_page as _fetch_backup_page,
    page_count_from_page_data as _page_count_from_page_data,
    write_page_json as _write_page_json,
)
from nga_tools.backup.post_html import (
    fill_missing_post_records as _fill_missing_post_records,
    find_missing_lou as _find_missing_lou,
    merge_missing_lou as _merge_missing_lou,
    post_refs_from_posts as _post_refs_from_posts,
)
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records as _apply_post_overlays_to_records,
    post_overlays_fingerprint,
)
from nga_tools.backup.post_version_selection import selections_fingerprint
from nga_tools.backup.processing_state import (
    BACKUP_PROCESSING_STATE_VERSION,
    BackupProcessingSnapshot,
    BackupProcessingState,
)
from nga_tools.console import report_info, report_progress, report_warning
from nga_tools.core.downloads import DownloadSummary
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import record_timing_label, record_timing_metric, time_section


@dataclass(frozen=True)
class FloorMapProcessingResult:
    build_result: FloorMapBuildResult
    cacheable: bool


@dataclass(frozen=True)
class _RecordProcessingResult:
    records: list[PostRecord]
    unresolved_missing_lous: list[int]


ProcessingStateReuseReason = Literal[
    "hit",
    "forced",
    "local_pages_incomplete",
    "state_invalid",
    "state_missing",
    "processing_version_changed",
    "archive_changed",
    "floor_map_changed",
    "page_count_changed",
    "author_total_changed",
    "post_overlays_changed",
    "post_version_selections_changed",
    "missing_floor_recovered",
    "state_changed_during_image_retry",
]
BackupLocalWorkKind = Literal["refresh", "maintenance"]


@dataclass(frozen=True)
class ProcessingStateReuseResult:
    hit: bool
    reason: ProcessingStateReuseReason


def _upsert_archive_pages(
    store: ThreadArchiveStore,
    page_data_by_page: dict[int, PageData],
) -> ArchivePagesUpsertResult:
    return store.upsert_pages(page_data_by_page)


def _legacy_page_numbers(folder_json: Path) -> set[int]:
    if not folder_json.is_dir():
        return set()
    page_numbers: set[int] = set()
    for path in folder_json.iterdir():
        if not path.is_file():
            continue
        match = PAGE_JSON_RE.fullmatch(path.name)
        if match is not None:
            page_numbers.add(int(match.group(1)))
    return page_numbers


def _archive_migration_command(tid: int, aid: Optional[int]) -> str:
    command = f"backup migrate-store --tid {tid}"
    if aid is not None:
        command += f" --aid {aid}"
    return command


def _ensure_legacy_json_is_migrated(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    archive_page_numbers: set[int],
) -> None:
    folder_json = archive_store.thread_folder / "json"
    legacy_page_numbers = _legacy_page_numbers(folder_json)
    unmigrated_page_numbers = legacy_page_numbers - archive_page_numbers
    if not unmigrated_page_numbers:
        return

    raise RuntimeError(
        f"{archive_store.db_path} 未覆盖旧JSON页："
        f"{', '.join(str(item) for item in sorted(unmigrated_page_numbers))}。"
        "正常备份不再读取旧JSON；请先运行 "
        f"{_archive_migration_command(tid, aid)}。"
    )


def _build_floor_map_for_post_refs(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: Optional[int],
    post_refs: list[AuthorPostRef],
    missing_lou: list[int],
) -> FloorMapProcessingResult:
    if aid is None:
        return FloorMapProcessingResult(
            FloorMapBuildResult(FloorLabels.plain(), {}),
            cacheable=False,
        )

    try:
        if not missing_lou:
            current_result = load_floor_map_build_result_if_current(
                archive_store,
                post_refs,
                missing_lou,
            )
            if current_result is not None:
                report_info("楼层映射输入未变化，复用数据库中的已有映射。")
                return FloorMapProcessingResult(current_result, cacheable=True)
        return FloorMapProcessingResult(
            build_and_save_floor_map(
                client,
                archive_store,
                tid,
                aid,
                post_refs,
                missing_lou,
                strict=False,
            ),
            cacheable=True,
        )
    except Exception as error:
        report_warning(f"楼层映射生成失败，继续生成备份：{error}")
        try:
            floor_labels = load_floor_labels_from_archive(archive_store, aid)
        except Exception as load_error:
            report_warning(f"无法加载已有楼层映射，使用普通楼层标签：{load_error}")
            floor_labels = FloorLabels.plain()
        return FloorMapProcessingResult(
            FloorMapBuildResult(floor_labels, {}),
            cacheable=False,
        )


def _author_post_refs_and_missing_lous(
    archive_store: ThreadArchiveStore,
    author_total_lou_count: int | None,
) -> tuple[list[AuthorPostRef], list[int]]:
    post_refs = archive_store.read_latest_author_post_refs()
    present_lous = {post["author_lou"] for post in post_refs}
    missing_lous = find_missing_author_lous(
        post_refs,
        author_total_lou_count,
    )
    previous_missing_lous = read_unresolved_missing_author_lous_from_archive(
        archive_store,
        present_lous=present_lous,
        total_lou_count=author_total_lou_count,
    )
    return post_refs, _merge_missing_lou(missing_lous, previous_missing_lous)


def _post_refs_and_missing_lous(
    archive_store: ThreadArchiveStore,
    aid: Optional[int],
    author_total_lou_count: int | None,
    records: list[PostRecord],
) -> tuple[list[AuthorPostRef], list[int]]:
    if aid is None:
        return (
            _post_refs_from_posts(records),
            _find_missing_lou(records, author_total_lou_count),
        )
    return _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )


def _retry_unresolved_missing_lous(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: int,
    author_total_lou_count: int | None,
    expected_snapshot: BackupProcessingSnapshot,
) -> bool:
    post_refs, missing_lous = _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )
    record_timing_metric("待恢复缺失楼数", len(missing_lous))
    if not missing_lous:
        record_timing_metric("本次恢复缺失楼数", 0)
        return False

    floor_map_processing = _build_floor_map_for_post_refs(
        client,
        archive_store,
        tid,
        aid,
        post_refs,
        missing_lous,
    )
    recovered_count = archive_store.upsert_recovered_posts(
        floor_map_processing.build_result.recovered_missing_posts_by_author_lou
    )
    record_timing_metric("本次恢复缺失楼数", recovered_count)
    current_snapshot = archive_store.read_backup_processing_snapshot()
    return current_snapshot.change_state != expected_snapshot.change_state


def _records_with_recovered_and_missing_posts(
    archive_store: ThreadArchiveStore,
    floor_map_result: FloorMapBuildResult,
    missing_lous: list[int],
    records: list[PostRecord],
) -> _RecordProcessingResult:
    recovered_count = archive_store.upsert_recovered_posts(
        floor_map_result.recovered_missing_posts_by_author_lou
    )
    record_timing_metric("本次恢复缺失楼数", recovered_count)
    archive_reread_required = recovered_count > 0
    record_timing_metric(
        "恢复正文写入引发归档重读",
        int(archive_reread_required),
    )
    if archive_reread_required:
        with time_section("恢复正文写入后重读完整归档"):
            records = archive_store.read_effective_post_records()
    present_lous = {record["lou"] for record in records}
    unresolved_missing_lous = [
        lou for lou in missing_lous if lou not in present_lous
    ]
    _fill_missing_post_records(
        records,
        unresolved_missing_lous,
        floor_map_result.floor_labels,
    )
    return _RecordProcessingResult(records, unresolved_missing_lous)


def _download_images_for_records(
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    floor_labels: FloorLabels,
    records: list[PostRecord],
) -> DownloadSummary:
    with time_section("Overlay应用"):
        effective_records = _apply_post_overlays_to_records(thread_folder, records)
    collection = _collect_image_download_tasks_for_records(
        archive_store,
        effective_records,
        floor_labels,
    )
    return _download_images(tid, aid, collection.tasks)


def _processing_snapshot_miss_reason(
    snapshot: BackupProcessingSnapshot,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> ProcessingStateReuseReason | None:
    state = snapshot.processing_state
    if state is None:
        return "state_missing"
    if (
        state.format_version != BACKUP_PROCESSING_STATE_VERSION
        or state.floor_map_format_version != FLOOR_MAP_VERSION
        or state.floor_map_generation_version != FLOOR_MAP_GENERATION_VERSION
        or state.floor_map_hash_algorithm != FLOOR_MAP_HASH_ALGORITHM
        or state.image_reference_extractor_version
        != IMAGE_REFERENCE_EXTRACTOR_VERSION
    ):
        return "processing_version_changed"
    if state.processed_archive_revision != snapshot.change_state.archive_revision:
        return "archive_changed"
    if state.processed_floor_map_revision != snapshot.change_state.floor_map_revision:
        return "floor_map_changed"
    if state.page_count != page_count:
        return "page_count_changed"
    if state.author_total_lou_count != author_total_lou_count:
        return "author_total_changed"
    if state.post_overlays_fingerprint != post_overlays_hash:
        return "post_overlays_changed"
    if state.post_version_selections_fingerprint != post_version_selections_hash:
        return "post_version_selections_changed"
    return None


def _failed_image_urls(download_summary: DownloadSummary) -> set[str]:
    return {item["url"] for item in download_summary["failed"]}


def _try_processing_state_reuse(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    local_pages_cover_remote: bool,
) -> ProcessingStateReuseResult:
    if not local_pages_cover_remote:
        return ProcessingStateReuseResult(False, "local_pages_incomplete")

    try:
        snapshot = archive_store.read_backup_processing_snapshot()
    except ValueError as error:
        report_warning(f"处理状态无效，改为完整处理：{error}")
        return ProcessingStateReuseResult(False, "state_invalid")
    if snapshot.processing_state is None:
        return ProcessingStateReuseResult(False, "state_missing")
    post_overlays_hash = post_overlays_fingerprint(thread_folder)
    post_version_selections_hash = selections_fingerprint(thread_folder)
    miss_reason = _processing_snapshot_miss_reason(
        snapshot,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    )
    if miss_reason is not None:
        return ProcessingStateReuseResult(False, miss_reason)
    state = snapshot.processing_state

    with time_section("未完成缺失楼重试"):
        if aid is None:
            record_timing_metric("待恢复缺失楼数", 0)
            record_timing_metric("本次恢复缺失楼数", 0)
            missing_floor_changed = False
        else:
            missing_floor_changed = _retry_unresolved_missing_lous(
                client,
                archive_store,
                tid,
                aid,
                author_total_lou_count,
                snapshot,
            )
    record_timing_metric(
        "缺失楼重试引发完整处理",
        int(missing_floor_changed),
    )
    if missing_floor_changed:
        report_info("缺失楼恢复结果已变化，转为完整处理。")
        return ProcessingStateReuseResult(False, "missing_floor_recovered")

    report_info("归档与派生输入未变化，跳过完整处理。")
    record_timing_metric("待重试图片URL数", len(snapshot.pending_image_urls))
    pending_tasks: list[ImageDownloadTask] = [
        {"url": url} for url in snapshot.pending_image_urls
    ]
    with time_section("未完成图片重试"):
        download_summary = _download_images(tid, aid, pending_tasks)
    if archive_store.replace_backup_pending_images(
        state,
        _failed_image_urls(download_summary),
    ):
        return ProcessingStateReuseResult(True, "hit")

    report_warning("处理状态在图片重试期间发生变化，改为完整处理。")
    return ProcessingStateReuseResult(False, "state_changed_during_image_retry")


def _commit_completed_processing_state(
    archive_store: ThreadArchiveStore,
    thread_folder: Path,
    *,
    aid: Optional[int],
    page_count: int,
    author_total_lou_count: int | None,
    floor_map_processing: FloorMapProcessingResult,
    unresolved_missing_lous: list[int],
    fingerprints_before: tuple[str, str],
    download_summary: DownloadSummary,
) -> None:
    pending_image_urls = _failed_image_urls(download_summary)
    record_timing_metric("待重试图片URL数", len(pending_image_urls))
    if aid is not None and not floor_map_processing.cacheable:
        report_info("楼层映射本次未形成可复用状态，下次继续完整处理。")
        return
    fingerprints_after = (
        post_overlays_fingerprint(thread_folder),
        selections_fingerprint(thread_folder),
    )
    if fingerprints_after != fingerprints_before:
        report_warning("派生输入在处理期间发生变化，未写入线程级处理状态。")
        return

    snapshot = archive_store.read_backup_processing_snapshot()
    state = BackupProcessingState(
        format_version=BACKUP_PROCESSING_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        processed_floor_map_revision=snapshot.change_state.floor_map_revision,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        post_overlays_fingerprint=fingerprints_before[0],
        post_version_selections_fingerprint=fingerprints_before[1],
        floor_map_format_version=FLOOR_MAP_VERSION,
        floor_map_generation_version=FLOOR_MAP_GENERATION_VERSION,
        floor_map_hash_algorithm=FLOOR_MAP_HASH_ALGORITHM,
        image_reference_extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    committed = archive_store.commit_backup_processing_state(
        state,
        pending_image_urls,
    )
    if not committed:
        report_warning("归档在处理期间发生变化，未写入线程级处理状态。")
    elif aid is not None and unresolved_missing_lous:
        report_info(
            f"仍有{len(unresolved_missing_lous)}个缺失楼未恢复，"
            "已保存处理状态，下次仅重试缺失楼。"
        )


def _run_full_processing(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    thread_folder: Path,
    tid: int,
    aid: Optional[int],
    *,
    page_count: int,
    author_total_lou_count: int | None,
) -> None:
    archive_store.clear_backup_processing_state()
    fingerprints_before = (
        post_overlays_fingerprint(thread_folder),
        selections_fingerprint(thread_folder),
    )

    report_info("开始处理")

    with time_section("读取归档与楼层映射"):
        with time_section("读取完整归档记录"):
            records = archive_store.read_effective_post_records()
        with time_section("缺失楼读取与合并"):
            post_refs, missing_lous = _post_refs_and_missing_lous(
                archive_store,
                aid,
                author_total_lou_count,
                records,
            )
            record_timing_metric("待恢复缺失楼数", len(missing_lous))
        with time_section("楼层映射生成/复用"):
            floor_map_processing = _build_floor_map_for_post_refs(
                client,
                archive_store,
                tid,
                aid,
                post_refs,
                missing_lous,
            )
        with time_section("恢复正文写入与缺失楼合并"):
            record_processing = _records_with_recovered_and_missing_posts(
                archive_store,
                floor_map_processing.build_result,
                missing_lous,
                records,
            )

    with time_section("正文解析与图片处理"):
        download_summary = _download_images_for_records(
            tid,
            aid,
            thread_folder,
            archive_store,
            floor_map_processing.build_result.floor_labels,
            record_processing.records,
        )

    _commit_completed_processing_state(
        archive_store,
        thread_folder,
        aid=aid,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        floor_map_processing=floor_map_processing,
        unresolved_missing_lous=record_processing.unresolved_missing_lous,
        fingerprints_before=fingerprints_before,
        download_summary=download_summary,
    )


def _record_archive_upsert_metrics(result: ArchivePagesUpsertResult) -> None:
    record_timing_metric("归档批量写入页数", result.pages_processed)
    record_timing_metric("归档新增页快照数", result.page_snapshots_inserted)
    record_timing_metric("归档新增帖子版本数", result.post_versions_inserted)
    record_timing_metric("归档写入楼层观测数", result.post_observations)
    record_timing_metric("归档有效变更页数", result.effective_changed_pages)


def _reuse_processing_state_after_page_refresh(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    local_pages_cover_remote: bool,
    force_processing: bool,
) -> ProcessingStateReuseResult:
    with time_section("处理状态复用判定"):
        if force_processing:
            report_info("已要求强制重处理，跳过处理状态复用。")
            result = ProcessingStateReuseResult(False, "forced")
        else:
            result = _try_processing_state_reuse(
                client,
                tid,
                aid,
                thread_folder,
                archive_store,
                page_count=page_count,
                author_total_lou_count=author_total_lou_count,
                local_pages_cover_remote=local_pages_cover_remote,
            )
    record_timing_metric("处理状态复用命中", int(result.hit))
    record_timing_label("处理状态复用结果", result.reason)
    return result


def backup_local_work_kind(
    tid: int,
    aid: Optional[int],
) -> BackupLocalWorkKind | None:
    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    if not archive_store.exists():
        return "refresh"

    try:
        snapshot = archive_store.read_backup_processing_snapshot()
    except ValueError:
        return "refresh"
    state = snapshot.processing_state
    if state is None:
        return "refresh"

    miss_reason = _processing_snapshot_miss_reason(
        snapshot,
        page_count=state.page_count,
        author_total_lou_count=state.author_total_lou_count,
        post_overlays_hash=post_overlays_fingerprint(thread_folder),
        post_version_selections_hash=selections_fingerprint(thread_folder),
    )
    if miss_reason is not None or snapshot.pending_image_urls:
        return "maintenance"
    if aid is not None and read_unresolved_missing_author_lous_from_archive(
        archive_store,
        total_lou_count=state.author_total_lou_count,
    ):
        return "maintenance"
    return None


def maintain_thread_backup(tid: int, aid: Optional[int]) -> None:
    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    snapshot = archive_store.read_backup_processing_snapshot()
    state = snapshot.processing_state
    if state is None:
        raise RuntimeError("缺少线程级处理状态，必须先执行增量备份。")

    with time_section("客户端初始化"):
        client = NGAClient()
    reuse_result = _reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        thread_folder,
        archive_store,
        page_count=state.page_count,
        author_total_lou_count=state.author_total_lou_count,
        local_pages_cover_remote=True,
        force_processing=False,
    )
    if reuse_result.hit:
        return

    _run_full_processing(
        client,
        archive_store,
        thread_folder,
        tid,
        aid,
        page_count=state.page_count,
        author_total_lou_count=state.author_total_lou_count,
    )


def backup_thread(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
    force_processing: bool = False,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()

    thread_folder = Path(utils.get_folder(tid, aid))
    archive_store = ThreadArchiveStore(thread_folder)
    with time_section("远端页面抓取"):
        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )

        page_data_by_page = _fetch_backup_pages(
            client,
            tid,
            aid,
            page_count,
            first_page_data,
            write_json=False,
        )

    with time_section("分页JSON导出"):
        if write_json:
            folder_json = Path(utils.get_folder(tid, aid, "debug_json"))
            for page_number, page_data in sorted(page_data_by_page.items()):
                _write_page_json(folder_json, page_number, page_data)
    record_timing_metric(
        "分页JSON导出页数",
        len(page_data_by_page) if write_json else 0,
    )

    with time_section("归档Schema初始化"):
        archive_store.ensure_schema()
    with time_section("归档页面准备与事务写入"):
        upsert_result = _upsert_archive_pages(archive_store, page_data_by_page)
    _record_archive_upsert_metrics(upsert_result)
    with time_section("归档字数回填"):
        refreshed_word_counts = archive_store.refresh_stored_word_counts()
    record_timing_metric("归档字数回填版本数", refreshed_word_counts)

    reuse_result = _reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        thread_folder,
        archive_store,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=(
            set(range(1, page_count + 1)) <= set(page_data_by_page)
        ),
        force_processing=force_processing,
    )
    if reuse_result.hit:
        return

    _run_full_processing(
        client,
        archive_store,
        thread_folder,
        tid,
        aid,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
    )


def backup_thread_sub(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
    force_processing: bool = False,
    allow_unchanged_author_fast_path: bool = False,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()

    thread_folder = Path(utils.get_folder(tid, aid))
    archive_store = ThreadArchiveStore(thread_folder)
    archive_existed = archive_store.exists()
    if archive_existed:
        with time_section("归档Schema初始化"):
            archive_store.ensure_schema()
    with time_section("增量预检查"):
        existing_page_numbers = archive_store.read_page_numbers()
        _ensure_legacy_json_is_migrated(tid, aid, archive_store, existing_page_numbers)
    if not archive_existed:
        with time_section("归档Schema初始化"):
            archive_store.ensure_schema()

    previous_author_total_lou_count = (
        archive_store.read_latest_author_total_lou_count()
        if aid is not None and existing_page_numbers
        else None
    )
    try:
        previous_processing_state = (
            archive_store.read_backup_processing_snapshot().processing_state
            if existing_page_numbers
            else None
        )
    except ValueError:
        previous_processing_state = None

    with time_section("远端页面抓取"):
        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )

        local_pages_cover_remote = set(range(1, page_count + 1)) <= (
            existing_page_numbers
        )
        unchanged_author_fast_path = (
            allow_unchanged_author_fast_path
            and aid is not None
            and previous_processing_state is not None
            and previous_processing_state.page_count == page_count
            and previous_author_total_lou_count == author_total_lou_count
            and local_pages_cover_remote
        )

        if unchanged_author_fast_path:
            report_progress(
                "楼主回复数和分页数未变化，仅校验第一页",
                completed=0,
                total=1,
            )
            page_data_by_page = {1: first_page_data}
            report_progress("第一页校验完成", completed=1, total=1)
        else:
            if existing_page_numbers:
                tail_start = min(max(existing_page_numbers), page_count)
            else:
                tail_start = 1
            missing_page_numbers = (
                set(range(1, page_count + 1)) - existing_page_numbers
            )
            refresh_page_numbers = (
                set(range(tail_start, page_count + 1)) | missing_page_numbers
            )
            report_progress(
                f"准备增量备份：远端{page_count}页，"
                f"本地{len(existing_page_numbers)}页，"
                f"需获取{len(refresh_page_numbers)}页",
                completed=0,
                total=len(refresh_page_numbers),
            )
            sorted_refresh_page_numbers = sorted(refresh_page_numbers)
            page_data_by_page = {}
            for index, page_number in enumerate(
                sorted_refresh_page_numbers,
                start=1,
            ):
                report_progress(
                    f"正在获取第{page_number}页",
                    completed=index - 1,
                    total=len(sorted_refresh_page_numbers),
                )
                page_data = _fetch_backup_page(
                    client,
                    tid,
                    aid,
                    page_number,
                    page_count,
                    first_page_data,
                )
                page_data_by_page[page_number] = page_data
            report_progress(
                "页面获取完成",
                completed=len(sorted_refresh_page_numbers),
                total=len(sorted_refresh_page_numbers),
            )

    with time_section("分页JSON导出"):
        if write_json:
            folder_json = Path(utils.get_folder(tid, aid, "debug_json"))
            for page_number, page_data in sorted(page_data_by_page.items()):
                _write_page_json(folder_json, page_number, page_data)
    record_timing_metric(
        "分页JSON导出页数",
        len(page_data_by_page) if write_json else 0,
    )

    with time_section("归档页面准备与事务写入"):
        upsert_result = _upsert_archive_pages(archive_store, page_data_by_page)
    _record_archive_upsert_metrics(upsert_result)
    record_timing_metric("增量有效变更页数", upsert_result.effective_changed_pages)
    with time_section("归档字数回填"):
        refreshed_word_counts = archive_store.refresh_stored_word_counts()
    record_timing_metric("归档字数回填版本数", refreshed_word_counts)

    if unchanged_author_fast_path and upsert_result.effective_changed_pages > 0:
        with time_section("智能增量尾页回退抓取"):
            tail_start = min(max(existing_page_numbers), page_count)
            fallback_page_numbers = set(range(tail_start, page_count + 1)) - {1}
            for page_number in sorted(fallback_page_numbers):
                page_data_by_page[page_number] = _fetch_backup_page(
                    client,
                    tid,
                    aid,
                    page_number,
                    page_count,
                    first_page_data,
                )
        if write_json:
            folder_json = Path(utils.get_folder(tid, aid, "debug_json"))
            for page_number in sorted(fallback_page_numbers):
                _write_page_json(
                    folder_json,
                    page_number,
                    page_data_by_page[page_number],
                )
        if fallback_page_numbers:
            with time_section("智能增量尾页回退写入"):
                fallback_upsert_result = _upsert_archive_pages(
                    archive_store,
                    {
                        page_number: page_data_by_page[page_number]
                        for page_number in fallback_page_numbers
                    },
                )
            _record_archive_upsert_metrics(fallback_upsert_result)
            with time_section("智能增量尾页回退字数回填"):
                fallback_word_counts = archive_store.refresh_stored_word_counts()
            record_timing_metric(
                "智能增量尾页回退字数回填版本数",
                fallback_word_counts,
            )

    available_page_numbers = existing_page_numbers | set(page_data_by_page)
    reuse_result = _reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        thread_folder,
        archive_store,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=(
            set(range(1, page_count + 1)) <= available_page_numbers
        ),
        force_processing=force_processing,
    )
    if reuse_result.hit:
        return

    _run_full_processing(
        client,
        archive_store,
        thread_folder,
        tid,
        aid,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
    )

from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nga_tools import utils
from nga_tools.backup.archive_store import ThreadArchiveStore
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
from nga_tools.timing import record_timing_metric, time_section


@dataclass(frozen=True)
class FloorMapProcessingResult:
    build_result: FloorMapBuildResult
    cacheable: bool


@dataclass(frozen=True)
class _RecordProcessingResult:
    records: list[PostRecord]
    unresolved_missing_lous: list[int]


def _upsert_archive_pages(
    store: ThreadArchiveStore,
    page_data_by_page: dict[int, PageData],
) -> int:
    effective_changed_pages = 0
    for page_number in sorted(page_data_by_page):
        result = store.upsert_page(page_number, page_data_by_page[page_number])
        if result.effective_processing_inputs_changed:
            effective_changed_pages += 1
    return effective_changed_pages


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


def _records_with_recovered_and_missing_posts(
    archive_store: ThreadArchiveStore,
    floor_map_result: FloorMapBuildResult,
    missing_lous: list[int],
) -> _RecordProcessingResult:
    archive_store.upsert_recovered_posts(
        floor_map_result.recovered_missing_posts_by_author_lou
    )
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


def _processing_snapshot_matches(
    snapshot: BackupProcessingSnapshot,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> bool:
    state = snapshot.processing_state
    if state is None:
        return False
    return (
        state.format_version == BACKUP_PROCESSING_STATE_VERSION
        and state.processed_archive_revision
        == snapshot.change_state.archive_revision
        and state.processed_floor_map_revision
        == snapshot.change_state.floor_map_revision
        and state.page_count == page_count
        and state.author_total_lou_count == author_total_lou_count
        and state.post_overlays_fingerprint == post_overlays_hash
        and state.post_version_selections_fingerprint
        == post_version_selections_hash
        and state.floor_map_format_version == FLOOR_MAP_VERSION
        and state.floor_map_generation_version == FLOOR_MAP_GENERATION_VERSION
        and state.floor_map_hash_algorithm == FLOOR_MAP_HASH_ALGORITHM
        and state.image_reference_extractor_version
        == IMAGE_REFERENCE_EXTRACTOR_VERSION
    )


def _failed_image_urls(download_summary: DownloadSummary) -> set[str]:
    return {item["url"] for item in download_summary["failed"]}


def _try_incremental_fast_path(
    tid: int,
    aid: int,
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    local_pages_cover_remote: bool,
) -> bool:
    if not local_pages_cover_remote:
        return False

    try:
        snapshot = archive_store.read_backup_processing_snapshot()
    except ValueError as error:
        report_warning(f"增量快路径状态无效，改为完整处理：{error}")
        return False
    state = snapshot.processing_state
    if state is None:
        return False
    post_overlays_hash = post_overlays_fingerprint(thread_folder)
    post_version_selections_hash = selections_fingerprint(thread_folder)
    if not _processing_snapshot_matches(
        snapshot,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    ):
        return False

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
        return True

    report_warning("增量状态在图片重试期间发生变化，改为完整处理。")
    return False


def _commit_completed_processing_state(
    archive_store: ThreadArchiveStore,
    thread_folder: Path,
    *,
    aid: Optional[int],
    page_count: int,
    author_total_lou_count: int | None,
    floor_map_processing: FloorMapProcessingResult,
    unresolved_missing_lous: list[int],
    fingerprints_before: tuple[str, str] | None,
    download_summary: DownloadSummary,
) -> None:
    pending_image_urls = _failed_image_urls(download_summary)
    record_timing_metric("待重试图片URL数", len(pending_image_urls))
    if aid is None or fingerprints_before is None:
        return
    if not floor_map_processing.cacheable:
        report_info("楼层映射本次未形成可复用状态，下次继续完整处理。")
        return
    if unresolved_missing_lous:
        report_info(
            f"仍有{len(unresolved_missing_lous)}个缺失楼未恢复，"
            "下次继续完整处理。"
        )
        return

    fingerprints_after = (
        post_overlays_fingerprint(thread_folder),
        selections_fingerprint(thread_folder),
    )
    if fingerprints_after != fingerprints_before:
        report_warning("派生输入在处理期间发生变化，未写入增量快路径状态。")
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
    if not archive_store.commit_backup_processing_state(
        state,
        pending_image_urls,
    ):
        report_warning("归档在处理期间发生变化，未写入增量快路径状态。")


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
    fingerprints_before: tuple[str, str] | None = None
    if aid is not None:
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


def backup_thread(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()
    with time_section("抓取和写入页面"):
        thread_folder = Path(utils.get_folder(tid, aid))
        archive_store = ThreadArchiveStore(thread_folder)
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
            write_json=write_json,
        )
        _upsert_archive_pages(archive_store, page_data_by_page)
        archive_store.refresh_stored_word_counts()

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
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()
    with time_section("增量预检查"):
        thread_folder = Path(utils.get_folder(tid, aid))
        archive_store = ThreadArchiveStore(thread_folder)
        existing_page_numbers = archive_store.read_page_numbers()
        _ensure_legacy_json_is_migrated(tid, aid, archive_store, existing_page_numbers)
        if archive_store.exists():
            archive_store.refresh_stored_word_counts()

        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )

        if existing_page_numbers:
            tail_start = min(max(existing_page_numbers), page_count)
        else:
            tail_start = 1
        missing_page_numbers = set(range(1, page_count + 1)) - existing_page_numbers
        refresh_page_numbers = (
            set(range(tail_start, page_count + 1)) | missing_page_numbers
        )
        folder_json = Path(utils.get_folder(tid, aid, "json")) if write_json else None

    with time_section("抓取和写入页面"):
        report_progress(
            f"准备增量备份：远端{page_count}页，本地{len(existing_page_numbers)}页，"
            f"需获取{len(refresh_page_numbers)}页",
            completed=0,
            total=len(refresh_page_numbers),
        )
        sorted_refresh_page_numbers = sorted(refresh_page_numbers)
        effective_changed_pages = 0
        for index, page_number in enumerate(sorted_refresh_page_numbers, start=1):
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
            if folder_json is not None:
                _write_page_json(folder_json, page_number, page_data)
            upsert_result = archive_store.upsert_page(page_number, page_data)
            if upsert_result.effective_processing_inputs_changed:
                effective_changed_pages += 1
        report_progress(
            "页面获取完成",
            completed=len(sorted_refresh_page_numbers),
            total=len(sorted_refresh_page_numbers),
        )

    record_timing_metric("增量有效变更页数", effective_changed_pages)
    with time_section("增量快路径判定"):
        fast_path_hit = False
        if aid is not None:
            available_page_numbers = existing_page_numbers | refresh_page_numbers
            fast_path_hit = _try_incremental_fast_path(
                tid,
                aid,
                thread_folder,
                archive_store,
                page_count=page_count,
                author_total_lou_count=author_total_lou_count,
                local_pages_cover_remote=(
                    set(range(1, page_count + 1)) <= available_page_numbers
                ),
            )
    record_timing_metric("增量快路径命中", int(fast_path_hit))
    if fast_path_hit:
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

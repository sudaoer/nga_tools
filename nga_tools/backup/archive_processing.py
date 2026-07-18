from __future__ import annotations

import datetime
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Optional

import nga_tools.config as config
from nga_tools.backup import archive_image_processing
from nga_tools.backup.archive_processing_models import ArchiveIncrementalChanges
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import (
    AuthorPostRef,
    FloorMapBuildResult,
    FloorLabels,
    build_and_save_floor_map,
    find_missing_author_lous,
    load_floor_map_build_result_if_current,
    load_floor_labels_from_archive,
    unresolved_missing_author_lous_from_stored_floor_map,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
)
from nga_tools.backup.image_reference_cache import (
    IMAGE_REFERENCE_EXTRACTOR_VERSION,
)
from nga_tools.backup.image_store import ImageDownloadTask
from nga_tools.backup.models import PostRecord
from nga_tools.backup.missing_floor_retry import (
    MissingFloorRetrySelection,
    consecutive_missing_floor_groups,
    pending_missing_floor_retries_after_attempt,
    select_missing_floor_retries,
)
from nga_tools.backup.post_html import (
    fill_missing_post_records as _fill_missing_post_records,
    find_missing_lou as _find_missing_lou,
    merge_missing_lou as _merge_missing_lou,
    post_refs_from_posts as _post_refs_from_posts,
)
from nga_tools.backup.processing_state import (
    FLOOR_PROCESSING_STATE_VERSION,
    IMAGE_REFERENCE_STATE_VERSION,
    BackupProcessingSnapshot,
    FloorProcessingState,
    ImageReferenceManifestPost,
    ImageReferenceState,
    PendingImageRetry,
    PendingMissingFloorRetry,
)
from nga_tools.console import WarningCategory, report_info, report_warning
from nga_tools.ngaclient import NGAClient
from nga_tools.timing import record_timing_label, record_timing_metric, time_section


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
MissingFloorRetryMode = Literal["immediate", "scheduled"]


@dataclass(frozen=True)


class FloorMapProcessingResult:
    build_result: FloorMapBuildResult
    cacheable: bool



@dataclass(frozen=True)


class _RecordProcessingResult:
    records: list[PostRecord]
    unresolved_missing_lous: list[int]


@dataclass(frozen=True)


class _FloorStateRefreshResult:
    succeeded: bool
    snapshot: BackupProcessingSnapshot
    changed_lous: frozenset[int]
    added_lous: frozenset[int]



@dataclass(frozen=True)


class ProcessingStateReuseResult:
    hit: bool
    reason: ProcessingStateReuseReason


def _thread_retry_target_key(tid: int, aid: int) -> str:
    return f"{tid}:{aid}"


def select_missing_floor_retries_for_archive(
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: int,
    missing_lous: list[int],
    retries: Sequence[PendingMissingFloorRetry],
    *,
    now: datetime.datetime,
    mode: MissingFloorRetryMode,
) -> MissingFloorRetrySelection:
    groups = consecutive_missing_floor_groups(missing_lous)
    next_postdates = (
        archive_store.posts.read_next_postdates_after_lous(
            [group[-1] for group in groups]
        )
        if mode == "scheduled"
        else {}
    )
    app_config = config.get_config()
    selection = select_missing_floor_retries(
        missing_lous,
        next_postdates_by_gap_end=next_postdates,
        retries=retries,
        thread_target_key=_thread_retry_target_key(tid, aid),
        now=now,
        immediate_window=datetime.timedelta(
            hours=app_config.ankebak_missing_floor_immediate_retry_hours
        ),
        max_interval=datetime.timedelta(
            hours=app_config.ankebak_missing_floor_retry_max_interval_hours
        ),
        force=mode == "immediate",
    )
    record_timing_metric("缺失楼连续缺口组数", len(selection.gaps))
    record_timing_metric("本次重试缺失楼数", len(selection.due_lous))
    record_timing_metric("本次重试缺口组数", len(selection.due_gaps))
    record_timing_metric("概率延后缺失楼数", len(selection.deferred_lous))
    record_timing_metric("概率延后缺口组数", len(selection.deferred_gaps))
    record_timing_metric("缺失楼重试共享调度组数", int(bool(selection.gaps)))
    record_timing_metric(
        "缺失楼重试共享调度放行组数",
        int(bool(selection.due_gaps)),
    )
    return selection


def _store_missing_floor_attempt_result(
    archive_store: ThreadArchiveStore,
    snapshot: BackupProcessingSnapshot,
    *,
    unresolved_lous: list[int],
    attempted_lous: tuple[int, ...],
    attempted_at: datetime.datetime,
) -> None:
    retries_after = pending_missing_floor_retries_after_attempt(
        snapshot.pending_missing_floor_retries,
        unresolved_lous=unresolved_lous,
        attempted_lous=attempted_lous,
        attempted_at=attempted_at,
    )
    archive_store.state.replace_pending_missing_floor_retries(retries_after)



def _build_floor_map_for_post_refs(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: Optional[int],
    post_refs: list[AuthorPostRef],
    missing_lou: list[int],
    retry_missing_lou: tuple[int, ...] | None = None,
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
                retry_missing_author_lous=retry_missing_lou,
                strict=False,
            ),
            cacheable=True,
        )
    except Exception as error:
        report_warning(
            WarningCategory.FLOOR_MAP,
            f"楼层映射生成失败，继续生成备份：{error}",
        )
        try:
            floor_labels = load_floor_labels_from_archive(archive_store, aid)
        except Exception as load_error:
            report_warning(
                WarningCategory.FLOOR_MAP,
                f"无法加载已有楼层映射，使用普通楼层标签：{load_error}",
            )
            floor_labels = FloorLabels.plain()
        return FloorMapProcessingResult(
            FloorMapBuildResult(floor_labels, {}),
            cacheable=False,
        )



def _author_post_refs_and_missing_lous(
    archive_store: ThreadArchiveStore,
    author_total_lou_count: int | None,
) -> tuple[list[AuthorPostRef], list[int]]:
    inputs = archive_store.posts.read_author_floor_refresh_inputs()
    post_refs = list(inputs.post_refs)
    present_lous = {post["author_lou"] for post in post_refs}
    missing_lous = find_missing_author_lous(
        post_refs,
        author_total_lou_count,
    )
    if inputs.floor_map_error is not None:
        report_warning(
            WarningCategory.FLOOR_MAP,
            "楼层映射缺失楼缓存无效，忽略："
            f"{archive_store.db_path}: {inputs.floor_map_error}",
        )
        previous_missing_lous: list[int] = []
    elif inputs.stored_floor_map is None:
        previous_missing_lous = []
    else:
        previous_missing_lous = unresolved_missing_author_lous_from_stored_floor_map(
            inputs.stored_floor_map,
            present_lous=present_lous,
            total_lou_count=author_total_lou_count,
        )
    return post_refs, _merge_missing_lou(missing_lous, previous_missing_lous)


def _unresolved_missing_lous_from_archive_records(
    archive_store: ThreadArchiveStore,
    missing_lous: Sequence[int],
) -> list[int]:
    if not missing_lous:
        return []
    present_lous = {
        record["lou"]
        for record in archive_store.posts.read_latest_post_record_summaries()
    }
    return [lou for lou in missing_lous if lou not in present_lous]


def read_unresolved_missing_floor_lous(
    archive_store: ThreadArchiveStore,
    author_total_lou_count: int | None,
) -> list[int]:
    _post_refs, missing_lous = _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )
    return _unresolved_missing_lous_from_archive_records(
        archive_store,
        missing_lous,
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
    return _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )



def _records_with_recovered_and_missing_posts(
    archive_store: ThreadArchiveStore,
    floor_map_result: FloorMapBuildResult,
    missing_lous: list[int],
    records: list[PostRecord],
) -> _RecordProcessingResult:
    recovered_result = archive_store.ingest.upsert_recovered_posts(
        floor_map_result.recovered_missing_posts_by_author_lou
    )
    record_timing_metric(
        "本次恢复缺失楼数",
        recovered_result.inserted_count,
    )
    archive_reread_required = bool(recovered_result.effective_changed_lous)
    record_timing_metric(
        "恢复正文写入引发归档重读",
        int(archive_reread_required),
    )
    if archive_reread_required:
        with time_section("恢复正文写入后重读完整归档"):
            records = archive_store.posts.read_effective_post_records()
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


def _new_floor_state(
    snapshot: BackupProcessingSnapshot,
    *,
    page_count: int,
    author_total_lou_count: int | None,
) -> FloorProcessingState:
    return FloorProcessingState(
        format_version=FLOOR_PROCESSING_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        processed_floor_map_revision=snapshot.change_state.floor_map_revision,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        floor_map_format_version=FLOOR_MAP_VERSION,
        floor_map_generation_version=FLOOR_MAP_GENERATION_VERSION,
        floor_map_hash_algorithm=FLOOR_MAP_HASH_ALGORITHM,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )



def floor_state_is_current(
    snapshot: BackupProcessingSnapshot,
    *,
    page_count: int,
    author_total_lou_count: int | None,
) -> bool:
    state = snapshot.floor_state
    return state is not None and (
        state.format_version == FLOOR_PROCESSING_STATE_VERSION
        and state.processed_archive_revision == snapshot.change_state.archive_revision
        and state.processed_floor_map_revision
        == snapshot.change_state.floor_map_revision
        and state.page_count == page_count
        and state.author_total_lou_count == author_total_lou_count
        and state.floor_map_format_version == FLOOR_MAP_VERSION
        and state.floor_map_generation_version == FLOOR_MAP_GENERATION_VERSION
        and state.floor_map_hash_algorithm == FLOOR_MAP_HASH_ALGORITHM
    )


def _refresh_author_floor_state(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: int,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    expected_snapshot: BackupProcessingSnapshot,
    missing_floor_retry_mode: MissingFloorRetryMode,
    commit_even_if_unchanged: bool = True,
) -> _FloorStateRefreshResult:
    post_refs, missing_lous = _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )
    unresolved_missing_lous = _unresolved_missing_lous_from_archive_records(
        archive_store,
        missing_lous,
    )
    record_timing_metric("待恢复缺失楼数", len(unresolved_missing_lous))
    attempted_at = datetime.datetime.now(datetime.timezone.utc)
    retry_selection = select_missing_floor_retries_for_archive(
        archive_store,
        tid,
        aid,
        unresolved_missing_lous,
        expected_snapshot.pending_missing_floor_retries,
        now=attempted_at,
        mode=missing_floor_retry_mode,
    )
    if not unresolved_missing_lous and not commit_even_if_unchanged:
        record_timing_metric("本次恢复缺失楼数", 0)
        _store_missing_floor_attempt_result(
            archive_store,
            expected_snapshot,
            unresolved_lous=[],
            attempted_lous=(),
            attempted_at=attempted_at,
        )
        return _FloorStateRefreshResult(
            True,
            expected_snapshot,
            frozenset(),
            frozenset(),
        )

    with time_section("缺失楼恢复与楼层映射"):
        floor_processing = _build_floor_map_for_post_refs(
            client,
            archive_store,
            tid,
            aid,
            post_refs,
            missing_lous,
            retry_selection.due_lous,
        )
    with time_section("恢复正文事务写入"):
        recovered_result = archive_store.ingest.upsert_recovered_posts(
            floor_processing.build_result.recovered_missing_posts_by_author_lou
        )
    record_timing_metric(
        "本次恢复缺失楼数",
        recovered_result.inserted_count,
    )
    if not floor_processing.cacheable:
        return _FloorStateRefreshResult(
            False,
            expected_snapshot,
            recovered_result.effective_changed_lous,
            recovered_result.effective_added_lous,
        )
    recovered_lous = set(
        floor_processing.build_result.recovered_missing_posts_by_author_lou
    )
    unresolved_after = [
        lou for lou in unresolved_missing_lous if lou not in recovered_lous
    ]
    _store_missing_floor_attempt_result(
        archive_store,
        expected_snapshot,
        unresolved_lous=unresolved_after,
        attempted_lous=retry_selection.due_lous,
        attempted_at=attempted_at,
    )
    with time_section("处理状态快照重读"):
        snapshot = archive_store.state.read_backup_processing_snapshot()
    if (
        not commit_even_if_unchanged
        and snapshot.change_state == expected_snapshot.change_state
    ):
        return _FloorStateRefreshResult(
            True,
            snapshot,
            recovered_result.effective_changed_lous,
            recovered_result.effective_added_lous,
        )
    with time_section("楼层状态提交"):
        committed = archive_store.state.commit_floor_processing_state(
            _new_floor_state(
                snapshot,
                page_count=page_count,
                author_total_lou_count=author_total_lou_count,
            )
        )
    return _FloorStateRefreshResult(
        committed,
        snapshot,
        recovered_result.effective_changed_lous,
        recovered_result.effective_added_lous,
    )


def _try_processing_state_reuse(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    local_pages_cover_remote: bool,
    missing_floor_retry_mode: MissingFloorRetryMode,
    processing_snapshot: BackupProcessingSnapshot | None = None,
    incremental_changes: ArchiveIncrementalChanges | None = None,
) -> ProcessingStateReuseResult:
    if not local_pages_cover_remote:
        return ProcessingStateReuseResult(False, "local_pages_incomplete")

    try:
        if processing_snapshot is None:
            with time_section("处理状态元数据读取"):
                snapshot = archive_store.state.read_backup_processing_snapshot()
        else:
            snapshot = processing_snapshot
    except ValueError as error:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            f"处理状态无效，改为完整处理：{error}",
        )
        archive_store.state.clear_backup_processing_state()
        return ProcessingStateReuseResult(False, "state_invalid")
    post_overlays_hash = archive_store.overlays.post_overlays_fingerprint()
    post_version_selections_hash = (
        archive_store.posts.post_version_selections_fingerprint()
    )
    changes = (
        ArchiveIncrementalChanges(None, frozenset(), frozenset(), 0)
        if incremental_changes is None
        else incremental_changes
    )
    floor_hit = floor_state_is_current(
        snapshot,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
    )
    record_timing_metric("楼层状态复用命中", int(floor_hit))
    if not floor_hit:
        if aid is None or snapshot.floor_state is None:
            record_timing_label("楼层状态复用结果", "rebuild_required")
            return ProcessingStateReuseResult(False, "state_missing")
        with time_section("楼层派生状态刷新"):
            floor_refresh = _refresh_author_floor_state(
                client,
                archive_store,
                tid,
                aid,
                page_count=page_count,
                author_total_lou_count=author_total_lou_count,
                expected_snapshot=snapshot,
                missing_floor_retry_mode=missing_floor_retry_mode,
            )
            snapshot = floor_refresh.snapshot
            if not floor_refresh.succeeded:
                return ProcessingStateReuseResult(False, "floor_map_changed")
            changes = ArchiveIncrementalChanges(
                changes.previous_snapshot,
                changes.changed_lous | floor_refresh.changed_lous,
                changes.added_lous | floor_refresh.added_lous,
                changes.archive_revision_increments
                + int(bool(floor_refresh.changed_lous)),
            )
        record_timing_label("楼层状态复用结果", "floor_only_refresh")
    else:
        record_timing_label("楼层状态复用结果", "hit")
        if aid is not None:
            with time_section("未完成缺失楼重试"):
                before_archive_revision = snapshot.change_state.archive_revision
                floor_refresh = _refresh_author_floor_state(
                    client,
                    archive_store,
                    tid,
                    aid,
                    page_count=page_count,
                    author_total_lou_count=author_total_lou_count,
                    expected_snapshot=snapshot,
                    missing_floor_retry_mode=missing_floor_retry_mode,
                    commit_even_if_unchanged=False,
                )
                snapshot = floor_refresh.snapshot
                if not floor_refresh.succeeded:
                    return ProcessingStateReuseResult(False, "floor_map_changed")
                changes = ArchiveIncrementalChanges(
                    changes.previous_snapshot,
                    changes.changed_lous | floor_refresh.changed_lous,
                    changes.added_lous | floor_refresh.added_lous,
                    changes.archive_revision_increments
                    + int(bool(floor_refresh.changed_lous)),
                )
                record_timing_metric(
                    "缺失楼重试引发完整处理",
                    int(snapshot.change_state.archive_revision != before_archive_revision),
                )

    image_hit = archive_image_processing.image_state_is_current(
        snapshot,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    )
    record_timing_metric("图片引用状态复用命中", int(image_hit))
    if not image_hit or snapshot.image_state is None:
        incremental_mode = archive_image_processing.try_incremental_image_reference_update(
            tid,
            aid,
            archive_store,
            snapshot=snapshot,
            changes=changes,
            post_overlays_hash=post_overlays_hash,
            post_version_selections_hash=post_version_selections_hash,
        )
        if incremental_mode is not None:
            record_timing_label(
                "图片引用状态复用结果",
                f"image_collection_{incremental_mode}",
            )
            record_timing_label("图片引用处理模式", incremental_mode)
            return ProcessingStateReuseResult(True, "hit")
        record_timing_label(
            "图片引用状态复用结果",
            "image_collection_rebuilt",
        )
        record_timing_label("图片引用处理模式", "full")
        if not archive_image_processing.rebuild_image_reference_state(
            tid,
            aid,
            archive_store,
            post_overlays_hash=post_overlays_hash,
            post_version_selections_hash=post_version_selections_hash,
            pending_image_retries=snapshot.pending_image_retries,
        ):
            return ProcessingStateReuseResult(False, "archive_changed")
        return ProcessingStateReuseResult(True, "hit")

    record_timing_label(
        "图片引用状态复用结果",
        "image_collection_hit",
    )
    record_timing_label("图片引用处理模式", "hit")
    report_info("归档与派生输入未变化，跳过完整处理。")
    pending_tasks: list[ImageDownloadTask] = [
        {"url": retry.url} for retry in snapshot.pending_image_retries
    ]
    with time_section("未完成图片重试"):
        download_result = archive_image_processing.download_images_with_retry_policy(
            tid,
            aid,
            pending_tasks,
            snapshot.pending_image_retries,
            force=False,
        )
    if archive_store.state.replace_pending_images_for_image_state(
        snapshot.image_state,
        download_result.pending_image_retries,
    ):
        return ProcessingStateReuseResult(True, "hit")

    report_warning(
        WarningCategory.PROCESSING_STATE,
        "处理状态在图片重试期间发生变化，改为完整处理。",
    )
    return ProcessingStateReuseResult(False, "state_changed_during_image_retry")



def _commit_completed_processing_state(
    archive_store: ThreadArchiveStore,
    *,
    aid: Optional[int],
    page_count: int,
    author_total_lou_count: int | None,
    floor_map_processing: FloorMapProcessingResult,
    unresolved_missing_lous: list[int],
    fingerprints_before: tuple[str, str],
    pending_image_retries: tuple[PendingImageRetry, ...],
    manifest_posts: tuple[ImageReferenceManifestPost, ...],
) -> None:
    if aid is not None and not floor_map_processing.cacheable:
        report_info("楼层映射本次未形成可复用状态，下次继续完整处理。")
        return
    fingerprints_after = (
        archive_store.overlays.post_overlays_fingerprint(),
        archive_store.posts.post_version_selections_fingerprint(),
    )
    if fingerprints_after != fingerprints_before:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            "派生输入在处理期间发生变化，未写入线程级处理状态。",
        )
        return

    snapshot = archive_store.state.read_backup_processing_snapshot()
    floor_state = _new_floor_state(
        snapshot,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
    )
    image_state = ImageReferenceState(
        format_version=IMAGE_REFERENCE_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint=fingerprints_before[0],
        post_version_selections_fingerprint=fingerprints_before[1],
        image_reference_extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    floor_committed = archive_store.state.commit_floor_processing_state(floor_state)
    image_committed = archive_store.state.commit_image_reference_state(
        image_state,
        pending_image_retries,
        manifest_posts=manifest_posts,
    )
    if not floor_committed or not image_committed:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            "归档在处理期间发生变化，未写入线程级处理状态。",
        )
    elif aid is not None and unresolved_missing_lous:
        report_info(
            f"仍有{len(unresolved_missing_lous)}个缺失楼未恢复，"
            "已保存处理状态，下次仅重试缺失楼。"
        )



def run_full_processing(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: Optional[int],
    *,
    page_count: int,
    author_total_lou_count: int | None,
    force_image_retries: bool,
    missing_floor_retry_mode: MissingFloorRetryMode = "immediate",
) -> None:
    record_timing_label("图片引用处理模式", "full")
    fingerprints_before = (
        archive_store.overlays.post_overlays_fingerprint(),
        archive_store.posts.post_version_selections_fingerprint(),
    )

    report_info("开始处理")

    with time_section("读取归档与楼层映射"):
        with time_section("读取完整归档记录"):
            records = archive_store.posts.read_effective_post_records()
        with time_section("缺失楼读取与合并"):
            post_refs, missing_lous = _post_refs_and_missing_lous(
                archive_store,
                aid,
                author_total_lou_count,
                records,
            )
            present_lous = {record["lou"] for record in records}
            unresolved_missing_lous = [
                lou for lou in missing_lous if lou not in present_lous
            ]
            record_timing_metric(
                "待恢复缺失楼数",
                len(unresolved_missing_lous),
            )
        retry_snapshot = archive_store.state.read_backup_processing_snapshot()
        attempted_at = datetime.datetime.now(datetime.timezone.utc)
        if aid is None:
            retry_missing_lous: tuple[int, ...] | None = None
        else:
            retry_selection = select_missing_floor_retries_for_archive(
                archive_store,
                tid,
                aid,
                unresolved_missing_lous,
                retry_snapshot.pending_missing_floor_retries,
                now=attempted_at,
                mode=missing_floor_retry_mode,
            )
            retry_missing_lous = retry_selection.due_lous
        with time_section("楼层映射生成/复用"):
            floor_map_processing = _build_floor_map_for_post_refs(
                client,
                archive_store,
                tid,
                aid,
                post_refs,
                missing_lous,
                retry_missing_lous,
            )
        with time_section("恢复正文写入与缺失楼合并"):
            record_processing = _records_with_recovered_and_missing_posts(
                archive_store,
                floor_map_processing.build_result,
                missing_lous,
                records,
            )
        if aid is not None and floor_map_processing.cacheable:
            _store_missing_floor_attempt_result(
                archive_store,
                retry_snapshot,
                unresolved_lous=record_processing.unresolved_missing_lous,
                attempted_lous=retry_missing_lous or (),
                attempted_at=attempted_at,
            )

    with time_section("正文解析与图片处理"):
        processing_snapshot = archive_store.state.read_backup_processing_snapshot()
        image_processing = archive_image_processing.download_images_for_records(
            tid,
            aid,
            archive_store,
            floor_map_processing.build_result.floor_labels,
            record_processing.records,
            processing_snapshot.pending_image_retries,
            force_image_retries=force_image_retries,
        )

    _commit_completed_processing_state(
        archive_store,
        aid=aid,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        floor_map_processing=floor_map_processing,
        unresolved_missing_lous=record_processing.unresolved_missing_lous,
        fingerprints_before=fingerprints_before,
        pending_image_retries=image_processing.pending_image_retries,
        manifest_posts=image_processing.manifest_posts,
    )



def reuse_processing_state_after_page_refresh(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    local_pages_cover_remote: bool,
    force_processing: bool,
    missing_floor_retry_mode: MissingFloorRetryMode = "immediate",
    processing_snapshot: BackupProcessingSnapshot | None = None,
    incremental_changes: ArchiveIncrementalChanges | None = None,
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
                archive_store,
                page_count=page_count,
                author_total_lou_count=author_total_lou_count,
                local_pages_cover_remote=local_pages_cover_remote,
                missing_floor_retry_mode=missing_floor_retry_mode,
                processing_snapshot=processing_snapshot,
                incremental_changes=incremental_changes,
            )
    record_timing_metric("处理状态复用命中", int(result.hit))
    record_timing_label("处理状态复用结果", result.reason)
    return result

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Literal, Optional

from nga_tools.backup import archive_image_processing, archive_processing
from nga_tools.backup.archive_processing_models import ArchiveIncrementalChanges
from nga_tools.core.paths import get_folder
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.archive_store_models import ArchivePagesUpsertResult
from nga_tools.backup.audio_pipeline import (
    audio_state_is_current,
    maintain_archived_audio,
    pending_audio_retry_is_due,
)
from nga_tools.backup.page_store import (
    author_total_lou_count_from_page_data as _author_total_lou_count_from_page_data,
    fetch_backup_pages as _fetch_backup_pages,
    fetch_backup_page as _fetch_backup_page,
    page_count_from_page_data as _page_count_from_page_data,
    probe_incremental_backup_pages as _probe_incremental_backup_pages,
    write_page_json as _write_page_json,
)
from nga_tools.backup.processing_state import BackupProcessingSnapshot
from nga_tools.backup.processing_state import CurrentPaginationState
from nga_tools.console import report_progress
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import PageData
from nga_tools.timing import record_timing_metric, time_section
from nga_tools.storage import UnsupportedStorageFormatError


BackupLocalWorkKind = Literal["refresh", "maintenance"]


def _upsert_archive_pages(
    store: ThreadArchiveStore,
    page_data_by_page: dict[int, PageData],
    *,
    observed_at: datetime.datetime | None = None,
) -> ArchivePagesUpsertResult:
    return store.ingest.upsert_pages(
        page_data_by_page,
        observed_at=(
            None
            if observed_at is None
            else observed_at.astimezone(datetime.timezone.utc).isoformat(
                timespec="microseconds"
            )
        ),
    )


def _current_pagination_state(
    *,
    page_count: int,
    author_total_lou_count: int | None,
    source_page_number: int,
    observed_at: datetime.datetime,
) -> CurrentPaginationState:
    return CurrentPaginationState(
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        source_page_number=source_page_number,
        observed_at=observed_at,
    )


def _commit_current_pagination_state(
    archive_store: ThreadArchiveStore,
    pagination_state: CurrentPaginationState,
) -> None:
    with time_section("当前分页水位提交"):
        committed = archive_store.state.commit_current_pagination_state(
            pagination_state
        )
    record_timing_metric(
        "当前分页水位来源页",
        pagination_state.source_page_number,
    )
    if not committed:
        raise RuntimeError("当前分页水位在备份期间发生变化，拒绝提交旧状态。")


def _record_archive_upsert_metrics(result: ArchivePagesUpsertResult) -> None:
    record_timing_metric("归档批量写入页数", result.pages_processed)
    record_timing_metric("归档新增帖子版本数", result.post_versions_inserted)
    record_timing_metric("归档有效变更页数", result.effective_changed_pages)
    record_timing_metric(
        "归档有效变更楼层数",
        len(result.effective_changed_lous),
    )


def backup_local_work_kind(
    tid: int,
    aid: Optional[int],
    *,
    now: datetime.datetime | None = None,
) -> BackupLocalWorkKind | None:
    thread_folder = Path(get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    with archive_store.connection_session():
        return _backup_local_work_kind(
            tid,
            aid,
            archive_store,
            now=now,
        )


def _backup_local_work_kind(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    now: datetime.datetime | None,
) -> BackupLocalWorkKind | None:
    schedule_now = (
        datetime.datetime.now(datetime.timezone.utc) if now is None else now
    )
    if not archive_store.exists():
        return "refresh"
    archive_store.state.ensure_schema()
    archive_store.cache.ensure_schema()

    try:
        snapshot = archive_store.state.read_backup_processing_snapshot()
    except ValueError:
        return "refresh"
    if snapshot.floor_state is None or snapshot.image_state is None:
        return "refresh"

    pagination_state = snapshot.current_pagination_state
    if pagination_state is None:
        return "refresh"
    author_total_lou_count = (
        pagination_state.author_total_lou_count if aid is not None else None
    )

    if (
        aid is not None
        and archive_store.floor_maps.read_repairable_recovered_missing_floor_entries()
    ):
        return "maintenance"

    floor_current = archive_processing.floor_state_is_current(
        snapshot,
        page_count=pagination_state.page_count,
        author_total_lou_count=author_total_lou_count,
    )
    image_current = archive_image_processing.image_state_is_current(
        snapshot,
        post_overlays_hash=archive_store.overlays.post_overlays_fingerprint(),
        post_version_selections_hash=(
            archive_store.posts.post_version_selections_fingerprint()
        ),
    )
    if not floor_current or not image_current:
        return "maintenance"
    if not audio_state_is_current(
        snapshot,
        max_post_version_id=archive_store.posts.max_post_version_id(),
    ):
        return "maintenance"
    if snapshot.pending_audio_retries and pending_audio_retry_is_due(
        tid,
        aid,
        snapshot.pending_audio_retries,
        now=schedule_now,
    ):
        return "maintenance"
    if snapshot.pending_image_retries:
        retry_selection = archive_image_processing.select_pending_image_retries(
            tid,
            aid,
            snapshot.pending_image_retries,
            now=schedule_now,
            force=False,
        )
        if retry_selection.due:
            return "maintenance"
    if aid is not None:
        missing_lous = [
            retry.author_lou
            for retry in snapshot.pending_missing_floor_retries
        ]
        if missing_lous:
            retry_selection = (
                archive_processing.select_missing_floor_retries_for_archive(
                    archive_store,
                    tid,
                    aid,
                    missing_lous,
                    snapshot.pending_missing_floor_retries,
                    now=schedule_now,
                    mode="scheduled",
                )
            )
            if retry_selection.due_lous:
                return "maintenance"
    return None


def _read_incremental_base_snapshot(
    archive_store: ThreadArchiveStore,
) -> BackupProcessingSnapshot | None:
    try:
        return archive_store.state.read_backup_processing_snapshot()
    except UnsupportedStorageFormatError:
        with time_section("处理状态Schema兼容检查"):
            archive_store.state.ensure_schema()
        try:
            return archive_store.state.read_backup_processing_snapshot()
        except ValueError:
            return None
    except ValueError:
        return None


def maintain_thread_backup(
    tid: int,
    aid: Optional[int],
    *,
    schedule_missing_floor_retries: bool = False,
) -> None:
    thread_folder = Path(get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    with archive_store.connection_session():
        _maintain_thread_backup(
            tid,
            aid,
            archive_store,
            schedule_missing_floor_retries=schedule_missing_floor_retries,
        )


def _maintain_thread_backup(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    schedule_missing_floor_retries: bool,
) -> None:
    with time_section("处理状态Schema兼容检查"):
        archive_store.state.ensure_schema()
        archive_store.cache.ensure_schema()
    with time_section("处理状态元数据读取"):
        snapshot = archive_store.state.read_backup_processing_snapshot()
    if snapshot.floor_state is None or snapshot.image_state is None:
        raise RuntimeError("缺少线程级处理状态，必须先执行增量备份。")
    pagination_state = snapshot.current_pagination_state
    if pagination_state is None:
        raise RuntimeError("缺少当前分页水位，必须先执行增量备份。")
    author_total_lou_count = (
        pagination_state.author_total_lou_count if aid is not None else None
    )
    existing_page_numbers = archive_store.posts.read_page_numbers()
    local_pages_cover_remote = set(
        range(1, pagination_state.page_count + 1)
    ) <= existing_page_numbers

    with time_section("客户端初始化"):
        client = NGAClient()
    reuse_result = archive_processing.reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        archive_store,
        page_count=pagination_state.page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=local_pages_cover_remote,
        force_processing=False,
        missing_floor_retry_mode=(
            "scheduled" if schedule_missing_floor_retries else "immediate"
        ),
        processing_snapshot=snapshot,
        incremental_changes=ArchiveIncrementalChanges(
            snapshot,
            frozenset(),
            frozenset(),
            0,
        ),
    )
    if not reuse_result.hit:
        archive_processing.run_full_processing(
            client,
            archive_store,
            tid,
            aid,
            page_count=pagination_state.page_count,
            author_total_lou_count=author_total_lou_count,
            force_image_retries=False,
            missing_floor_retry_mode=(
                "scheduled" if schedule_missing_floor_retries else "immediate"
            ),
        )
    maintain_archived_audio(
        tid,
        aid,
        archive_store,
        force=False,
        processing_snapshot=snapshot,
    )


def backup_thread(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
    force_processing: bool = False,
    schedule_missing_floor_retries: bool = False,
) -> None:
    thread_folder = Path(get_folder(tid, aid))
    archive_store = ThreadArchiveStore(thread_folder)
    with archive_store.connection_session():
        _backup_thread(
            tid,
            aid,
            archive_store,
            write_json=write_json,
            force_processing=force_processing,
            schedule_missing_floor_retries=schedule_missing_floor_retries,
        )


def _backup_thread(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    write_json: bool,
    force_processing: bool,
    schedule_missing_floor_retries: bool,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()

    with time_section("远端页面抓取"):
        first_page_data = client.get_page(tid, aid, 1)
        page_count = _page_count_from_page_data(first_page_data)
        author_total_lou_count = _author_total_lou_count_from_page_data(
            first_page_data,
            aid,
        )
        pagination_observed_at = datetime.datetime.now(datetime.timezone.utc)

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
            folder_json = Path(get_folder(tid, aid, "debug_json"))
            for page_number, page_data in sorted(page_data_by_page.items()):
                _write_page_json(folder_json, page_number, page_data)
    record_timing_metric(
        "分页JSON导出页数",
        len(page_data_by_page) if write_json else 0,
    )

    with time_section("归档Schema初始化"):
        archive_store.ensure_schema()
    previous_processing_snapshot = _read_incremental_base_snapshot(
        archive_store
    )
    with time_section("归档页面准备与事务写入"):
        upsert_result = _upsert_archive_pages(
            archive_store,
            page_data_by_page,
            observed_at=pagination_observed_at,
        )
    _record_archive_upsert_metrics(upsert_result)
    _commit_current_pagination_state(
        archive_store,
        _current_pagination_state(
            page_count=page_count,
            author_total_lou_count=author_total_lou_count,
            source_page_number=1,
            observed_at=pagination_observed_at,
        ),
    )
    with time_section("归档字数回填"):
        refreshed_word_counts = archive_store.ingest.refresh_stored_word_counts()
    record_timing_metric("归档字数回填版本数", refreshed_word_counts)

    local_pages_cover_remote = set(range(1, page_count + 1)) <= set(
        page_data_by_page
    )
    archived_page_data_count = len(page_data_by_page)
    page_data_by_page.clear()
    del page_data_by_page, first_page_data
    cleared_client_page_count = client.clear_page_cache()
    record_timing_metric(
        "归档页面内存释放页数",
        max(archived_page_data_count, cleared_client_page_count),
    )

    reuse_result = archive_processing.reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        archive_store,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=local_pages_cover_remote,
        force_processing=force_processing,
        missing_floor_retry_mode=(
            "scheduled"
            if schedule_missing_floor_retries and not force_processing
            else "immediate"
        ),
        incremental_changes=ArchiveIncrementalChanges(
            previous_processing_snapshot,
            upsert_result.effective_changed_lous,
            upsert_result.effective_added_lous,
            int(bool(upsert_result.effective_changed_lous)),
        ),
    )
    if not reuse_result.hit:
        archive_processing.run_full_processing(
            client,
            archive_store,
            tid,
            aid,
            page_count=page_count,
            author_total_lou_count=author_total_lou_count,
            force_image_retries=force_processing,
            missing_floor_retry_mode=(
                "scheduled"
                if schedule_missing_floor_retries and not force_processing
                else "immediate"
            ),
        )
    maintain_archived_audio(
        tid,
        aid,
        archive_store,
        force=force_processing,
        processing_snapshot=previous_processing_snapshot,
    )


def backup_thread_sub(
    tid: int,
    aid: Optional[int],
    *,
    write_json: bool = False,
    force_processing: bool = False,
    allow_unchanged_author_fast_path: bool = False,
    schedule_missing_floor_retries: bool = False,
) -> None:
    thread_folder = Path(get_folder(tid, aid))
    archive_store = ThreadArchiveStore(thread_folder)
    with archive_store.connection_session():
        _backup_thread_sub(
            tid,
            aid,
            archive_store,
            write_json=write_json,
            force_processing=force_processing,
            allow_unchanged_author_fast_path=(
                allow_unchanged_author_fast_path
            ),
            schedule_missing_floor_retries=schedule_missing_floor_retries,
        )


def _backup_thread_sub(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    write_json: bool,
    force_processing: bool,
    allow_unchanged_author_fast_path: bool,
    schedule_missing_floor_retries: bool,
) -> None:
    with time_section("客户端初始化"):
        client = NGAClient()

    archive_existed = archive_store.exists()
    if archive_existed:
        with time_section("归档Schema初始化"):
            archive_store.ensure_schema()
    with time_section("增量预检查"):
        existing_page_numbers = archive_store.posts.read_page_numbers()
    if not archive_existed:
        with time_section("归档Schema初始化"):
            archive_store.ensure_schema()

    previous_processing_snapshot = _read_incremental_base_snapshot(
        archive_store
    )
    previous_floor_state = (
        previous_processing_snapshot.floor_state
        if previous_processing_snapshot is not None and existing_page_numbers
        else None
    )
    previous_author_total_lou_count = (
        previous_floor_state.author_total_lou_count
        if aid is not None and previous_floor_state is not None
        else None
    )

    with time_section("远端页面抓取"):
        incremental_probe = _probe_incremental_backup_pages(
            client,
            tid,
            aid,
            existing_page_numbers,
        )
        page_count = incremental_probe.page_count
        author_total_lou_count = incremental_probe.author_total_lou_count
        pagination_source_page = incremental_probe.page_number
        pagination_observed_at = datetime.datetime.now(datetime.timezone.utc)
        page_data_by_page = dict(incremental_probe.page_data_by_page)
        reference_page_data = page_data_by_page[incremental_probe.page_number]

        local_pages_cover_remote = set(range(1, page_count + 1)) <= (
            existing_page_numbers
        )
        unchanged_author_fast_path = (
            allow_unchanged_author_fast_path
            and aid is not None
            and previous_floor_state is not None
            and previous_floor_state.page_count == page_count
            and previous_author_total_lou_count == author_total_lou_count
            and local_pages_cover_remote
        )

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

        def fetch_refresh_pages() -> None:
            sorted_refresh_page_numbers = sorted(refresh_page_numbers)
            completed = len(refresh_page_numbers & set(page_data_by_page))
            report_progress(
                f"准备增量备份：远端{page_count}页，"
                f"本地{len(existing_page_numbers)}页，"
                f"需获取{len(refresh_page_numbers)}页",
                completed=completed,
                total=len(sorted_refresh_page_numbers),
            )
            for page_number in sorted_refresh_page_numbers:
                if page_number in page_data_by_page:
                    continue
                report_progress(
                    f"正在获取第{page_number}页",
                    completed=completed,
                    total=len(sorted_refresh_page_numbers),
                )
                page_data_by_page[page_number] = _fetch_backup_page(
                    client,
                    tid,
                    aid,
                    page_number,
                    page_count,
                    reference_page_data,
                )
                completed += 1
            report_progress(
                "页面获取完成",
                completed=len(sorted_refresh_page_numbers),
                total=len(sorted_refresh_page_numbers),
            )

        if unchanged_author_fast_path:
            report_progress(
                "楼主回复数和分页数未变化，仅校验尾页",
                completed=0,
                total=1,
            )
            report_progress("尾页校验完成", completed=1, total=1)
            with time_section("智能增量尾页变更预检"):
                probe_page_changed = (
                    archive_store.ingest.page_effective_processing_inputs_changed(
                        incremental_probe.page_number,
                        reference_page_data,
                    )
                )
            if probe_page_changed:
                with time_section("智能增量尾页刷新"):
                    fetch_refresh_pages()
        else:
            fetch_refresh_pages()

    with time_section("分页JSON导出"):
        if write_json:
            folder_json = Path(get_folder(tid, aid, "debug_json"))
            for page_number, page_data in sorted(page_data_by_page.items()):
                _write_page_json(folder_json, page_number, page_data)
    record_timing_metric(
        "分页JSON导出页数",
        len(page_data_by_page) if write_json else 0,
    )

    with time_section("归档页面准备与事务写入"):
        upsert_result = _upsert_archive_pages(
            archive_store,
            page_data_by_page,
            observed_at=pagination_observed_at,
        )
    _record_archive_upsert_metrics(upsert_result)
    _commit_current_pagination_state(
        archive_store,
        _current_pagination_state(
            page_count=page_count,
            author_total_lou_count=author_total_lou_count,
            source_page_number=pagination_source_page,
            observed_at=pagination_observed_at,
        ),
    )
    record_timing_metric("增量有效变更页数", upsert_result.effective_changed_pages)
    with time_section("归档字数回填"):
        refreshed_word_counts = archive_store.ingest.refresh_stored_word_counts()
    record_timing_metric("归档字数回填版本数", refreshed_word_counts)

    available_page_numbers = existing_page_numbers | set(page_data_by_page)
    archived_page_data_count = len(page_data_by_page)
    page_data_by_page.clear()
    del page_data_by_page, incremental_probe
    cleared_client_page_count = client.clear_page_cache()
    record_timing_metric(
        "归档页面内存释放页数",
        max(archived_page_data_count, cleared_client_page_count),
    )
    reuse_result = archive_processing.reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        archive_store,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=(
            set(range(1, page_count + 1)) <= available_page_numbers
        ),
        force_processing=force_processing,
        missing_floor_retry_mode=(
            "scheduled"
            if schedule_missing_floor_retries and not force_processing
            else "immediate"
        ),
        incremental_changes=ArchiveIncrementalChanges(
            previous_processing_snapshot,
            upsert_result.effective_changed_lous,
            upsert_result.effective_added_lous,
            int(bool(upsert_result.effective_changed_lous)),
        ),
    )
    if not reuse_result.hit:
        archive_processing.run_full_processing(
            client,
            archive_store,
            tid,
            aid,
            page_count=page_count,
            author_total_lou_count=author_total_lou_count,
            force_image_retries=force_processing,
            missing_floor_retry_mode=(
                "scheduled"
                if schedule_missing_floor_retries and not force_processing
                else "immediate"
            ),
        )
    maintain_archived_audio(
        tid,
        aid,
        archive_store,
        force=force_processing,
        processing_snapshot=previous_processing_snapshot,
    )

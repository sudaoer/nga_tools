from __future__ import annotations

import datetime
from dataclasses import dataclass
from pathlib import Path

import nga_tools.config as config
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.audio_retry import (
    pending_audio_retries_after_attempt,
    select_audio_retries,
)
from nga_tools.backup.audio_store import (
    AudioDownloadTask,
    audio_mappings_for_urls,
    download_audio_tasks,
)
from nga_tools.backup.processing_state import (
    AUDIO_PROCESSING_STATE_VERSION,
    AUDIO_REFERENCE_EXTRACTOR_VERSION,
    AudioProcessingState,
    BackupProcessingSnapshot,
    PendingAudioRetry,
)
from nga_tools.console import WarningCategory, report_info, report_progress, report_warning
from nga_tools.core.nga_audio import extract_nga_audio_urls
from nga_tools.core.download_types import DownloadFileResult
from nga_tools.timing import record_timing_label, record_timing_metric, time_section


@dataclass(frozen=True)
class AudioArchiveMaintenanceResult:
    scanned_post_versions: int
    discovered_urls: int
    attempted_urls: int
    failed_urls: int
    full_scan: bool
    committed: bool


def audio_state_is_current(
    snapshot: BackupProcessingSnapshot,
    *,
    max_post_version_id: int,
) -> bool:
    state = snapshot.audio_state
    return state is not None and (
        state.format_version == AUDIO_PROCESSING_STATE_VERSION
        and state.extractor_version == AUDIO_REFERENCE_EXTRACTOR_VERSION
        and state.processed_max_post_version_id == max_post_version_id
    )


def _audio_state_is_compatible(
    snapshot: BackupProcessingSnapshot,
    *,
    max_post_version_id: int,
) -> bool:
    state = snapshot.audio_state
    return state is not None and (
        state.format_version == AUDIO_PROCESSING_STATE_VERSION
        and state.extractor_version == AUDIO_REFERENCE_EXTRACTOR_VERSION
        and 0 <= state.processed_max_post_version_id <= max_post_version_id
    )


def _select_pending_audio_retries(
    tid: int,
    aid: int | None,
    retries: tuple[PendingAudioRetry, ...],
    *,
    now: datetime.datetime,
    force: bool,
):
    return select_audio_retries(
        retries,
        thread_target_key=f"{tid}:{'all' if aid is None else aid}",
        now=now,
        max_interval=datetime.timedelta(
            hours=config.get_config().backup_audio_retry_max_interval_hours
        ),
        force=force,
    )


def pending_audio_retry_is_due(
    tid: int,
    aid: int | None,
    retries: tuple[PendingAudioRetry, ...],
    *,
    now: datetime.datetime,
) -> bool:
    return bool(
        _select_pending_audio_retries(
            tid,
            aid,
            retries,
            now=now,
            force=False,
        ).due
    )


def _audio_download_progress(
    current: int,
    total: int,
    _result: DownloadFileResult,
) -> None:
    report_progress(
        "正在保存帖子音频",
        completed=current,
        total=total,
    )


def _record_audio_noop() -> None:
    record_timing_label("音频引用扫描模式", "hit")
    record_timing_metric("音频扫描帖子版本数", 0)
    record_timing_metric("音频扫描唯一URL数", 0)
    record_timing_metric("历史待重试音频URL数", 0)
    record_timing_metric("本次重试音频URL数", 0)
    record_timing_metric("概率延后音频URL数", 0)
    record_timing_metric("本次新增音频URL数", 0)
    record_timing_metric("待重试音频URL数", 0)


def maintain_archived_audio(
    tid: int,
    aid: int | None,
    archive_store: ThreadArchiveStore,
    *,
    force: bool,
    processing_snapshot: BackupProcessingSnapshot | None = None,
) -> AudioArchiveMaintenanceResult:
    with time_section("音频处理状态读取"):
        snapshot = (
            archive_store.state.read_backup_processing_snapshot()
            if processing_snapshot is None
            else processing_snapshot
        )
        max_post_version_id = archive_store.posts.max_post_version_id()
    if (
        not force
        and not snapshot.pending_audio_retries
        and audio_state_is_current(
            snapshot,
            max_post_version_id=max_post_version_id,
        )
    ):
        _record_audio_noop()
        return AudioArchiveMaintenanceResult(
            scanned_post_versions=0,
            discovered_urls=0,
            attempted_urls=0,
            failed_urls=0,
            full_scan=False,
            committed=True,
        )
    state_compatible = _audio_state_is_compatible(
        snapshot,
        max_post_version_id=max_post_version_id,
    )
    full_scan = force or not state_compatible
    after_id = (
        0
        if full_scan or snapshot.audio_state is None
        else snapshot.audio_state.processed_max_post_version_id
    )
    if after_id > max_post_version_id:
        full_scan = True
        after_id = 0

    with time_section("历史帖子版本音频引用扫描"):
        version_contents = archive_store.posts.read_post_version_contents(
            after_id=after_id,
            through_id=max_post_version_id,
        )
        discovered_urls = {
            url
            for _version_id, content in version_contents
            for url in extract_nga_audio_urls(content)
        }
    record_timing_label(
        "音频引用扫描模式",
        "full" if full_scan else "delta",
    )
    record_timing_metric("音频扫描帖子版本数", len(version_contents))
    record_timing_metric("音频扫描唯一URL数", len(discovered_urls))

    attempted_at = datetime.datetime.now(datetime.timezone.utc)
    pending_urls = {retry.url for retry in snapshot.pending_audio_retries}
    candidate_urls = discovered_urls | pending_urls
    with time_section("音频本地映射校验"):
        mapped_urls = set(
            audio_mappings_for_urls(
                Path(config.get_config().output_dir),
                candidate_urls,
            )
        )
    retained_pending = tuple(
        retry
        for retry in snapshot.pending_audio_retries
        if retry.url not in mapped_urls
    )
    selection = _select_pending_audio_retries(
        tid,
        aid,
        retained_pending,
        now=attempted_at,
        force=force,
    )
    due_urls = {retry.url for retry in selection.due}
    new_urls = discovered_urls - pending_urls - mapped_urls
    selected_urls = new_urls | due_urls
    tasks: list[AudioDownloadTask] = [
        {"url": url} for url in sorted(selected_urls)
    ]
    record_timing_metric("历史待重试音频URL数", len(snapshot.pending_audio_retries))
    record_timing_metric("本次重试音频URL数", len(selection.due))
    record_timing_metric("概率延后音频URL数", len(selection.deferred))
    record_timing_metric("本次新增音频URL数", len(new_urls))

    if tasks:
        report_progress("准备保存帖子音频", completed=0, total=len(tasks))
    with time_section("音频下载与内容寻址存储"):
        download_summary = download_audio_tasks(
            tasks,
            output_root=Path(config.get_config().output_dir),
            on_download_progress=_audio_download_progress,
        )
    retries_after = pending_audio_retries_after_attempt(
        selection.deferred,
        download_summary["failed"],
        attempted_at=attempted_at,
    )
    record_timing_metric("待重试音频URL数", len(retries_after))

    new_state = AudioProcessingState(
        format_version=AUDIO_PROCESSING_STATE_VERSION,
        extractor_version=AUDIO_REFERENCE_EXTRACTOR_VERSION,
        processed_max_post_version_id=max_post_version_id,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    with time_section("音频处理状态提交"):
        committed = archive_store.state.commit_audio_processing_state(
            new_state,
            retries_after,
        )
    if not committed:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            "归档在音频处理期间新增了帖子版本，音频水位将在下次继续推进。",
        )
    elif full_scan or version_contents or tasks or snapshot.pending_audio_retries:
        report_info(
            "音频归档完成："
            f"扫描版本{len(version_contents)}个，"
            f"发现URL{len(discovered_urls)}个，"
            f"本次尝试{len(tasks)}个，"
            f"待重试{len(retries_after)}个。"
        )

    return AudioArchiveMaintenanceResult(
        scanned_post_versions=len(version_contents),
        discovered_urls=len(discovered_urls),
        attempted_urls=len(tasks),
        failed_urls=len(download_summary["failed"]),
        full_scan=full_scan,
        committed=committed,
    )

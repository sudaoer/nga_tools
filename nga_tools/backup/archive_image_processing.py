from __future__ import annotations

import datetime
from collections import Counter
from dataclasses import dataclass
from typing import Literal, Optional

import nga_tools.config as config
from nga_tools.backup.archive_processing_models import ArchiveIncrementalChanges
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels, load_floor_labels_from_archive
from nga_tools.backup.image_pipeline import download_images_compact as _download_images
from nga_tools.backup.image_reference_cache import (
    IMAGE_REFERENCE_EXTRACTOR_VERSION,
    collect_image_download_tasks_for_records as _collect_image_download_tasks_for_records,
)
from nga_tools.backup.image_retry import (
    ImageRetrySelection,
    pending_image_retries_after_attempt,
    select_image_retries,
    uses_probabilistic_backoff,
)
from nga_tools.backup.image_store import ImageDownloadTask
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records as _apply_post_overlays_to_records,
)
from nga_tools.backup.processing_state import (
    IMAGE_REFERENCE_MANIFEST_VERSION,
    IMAGE_REFERENCE_STATE_VERSION,
    BackupProcessingSnapshot,
    ImageReferenceManifestPost,
    ImageReferenceState,
    PendingImageRetry,
)
from nga_tools.console import WarningCategory, report_warning
from nga_tools.timing import record_timing_metric, time_section


@dataclass(frozen=True)


class ImageRecordProcessingResult:
    pending_image_retries: tuple[PendingImageRetry, ...]
    manifest_posts: tuple[ImageReferenceManifestPost, ...]



@dataclass(frozen=True)


class _ScheduledImageDownloadResult:
    pending_image_retries: tuple[PendingImageRetry, ...]



def _thread_retry_target_key(tid: int, aid: Optional[int]) -> str:
    return f"{tid}:{'all' if aid is None else aid}"



def select_pending_image_retries(
    tid: int,
    aid: Optional[int],
    retries: tuple[PendingImageRetry, ...],
    *,
    now: datetime.datetime,
    force: bool,
) -> ImageRetrySelection:
    return select_image_retries(
        retries,
        thread_target_key=_thread_retry_target_key(tid, aid),
        now=now,
        max_interval=datetime.timedelta(
            hours=config.get_config().backup_image_retry_max_interval_hours
        ),
        force=force,
    )



def download_images_with_retry_policy(
    tid: int,
    aid: Optional[int],
    tasks: list[ImageDownloadTask],
    pending_image_retries: tuple[PendingImageRetry, ...],
    *,
    force: bool,
) -> _ScheduledImageDownloadResult:
    attempted_at = datetime.datetime.now(datetime.timezone.utc)
    selection = select_pending_image_retries(
        tid,
        aid,
        pending_image_retries,
        now=attempted_at,
        force=force,
    )
    pending_urls = {retry.url for retry in pending_image_retries}
    due_urls = {retry.url for retry in selection.due}
    selected_tasks = [
        task
        for task in tasks
        if task["url"] not in pending_urls or task["url"] in due_urls
    ]
    record_timing_metric("历史待重试图片URL数", len(pending_image_retries))
    record_timing_metric("本次重试图片URL数", len(selection.due))
    record_timing_metric("概率延后图片URL数", len(selection.deferred))
    persistent_retry_count = sum(
        uses_probabilistic_backoff(retry)
        for retry in pending_image_retries
    )
    due_persistent_retry_count = sum(
        uses_probabilistic_backoff(retry)
        for retry in selection.due
    )
    record_timing_metric("持久性图片重试URL数", persistent_retry_count)
    record_timing_metric(
        "图片重试共享调度组数",
        int(persistent_retry_count > 0),
    )
    record_timing_metric(
        "图片重试共享调度放行组数",
        int(due_persistent_retry_count > 0),
    )
    download_summary = _download_images(tid, aid, selected_tasks)
    retries_after = pending_image_retries_after_attempt(
        selection.deferred,
        download_summary.failed,
        attempted_at=attempted_at,
    )
    record_timing_metric("待重试图片URL数", len(retries_after))
    return _ScheduledImageDownloadResult(retries_after)



def download_images_for_records(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    floor_labels: FloorLabels,
    records: list[PostRecord],
    pending_image_retries: tuple[PendingImageRetry, ...],
    *,
    force_image_retries: bool,
) -> ImageRecordProcessingResult:
    with time_section("Overlay应用"):
        effective_records = _apply_post_overlays_to_records(
            archive_store.overlays.read_post_overlays(),
            records,
            output_dir=archive_store.thread_folder.parent,
        )
    collection = _collect_image_download_tasks_for_records(
        archive_store,
        effective_records,
        floor_labels,
    )
    current_urls = {task["url"] for task in collection.tasks}
    retained_pending = tuple(
        retry
        for retry in pending_image_retries
        if retry.url in current_urls
    )
    download_result = download_images_with_retry_policy(
        tid,
        aid,
        collection.tasks,
        retained_pending,
        force=force_image_retries,
    )
    return ImageRecordProcessingResult(
        pending_image_retries=download_result.pending_image_retries,
        manifest_posts=collection.manifest_posts,
    )



def image_state_is_current(
    snapshot: BackupProcessingSnapshot,
    *,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> bool:
    state = snapshot.image_state
    return state is not None and (
        state.format_version == IMAGE_REFERENCE_STATE_VERSION
        and state.processed_archive_revision == snapshot.change_state.archive_revision
        and state.post_overlays_fingerprint == post_overlays_hash
        and state.post_version_selections_fingerprint
        == post_version_selections_hash
        and state.image_reference_extractor_version
        == IMAGE_REFERENCE_EXTRACTOR_VERSION
    )



def rebuild_image_reference_state(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    post_overlays_hash: str,
    post_version_selections_hash: str,
    pending_image_retries: tuple[PendingImageRetry, ...],
) -> bool:
    with time_section("图片引用集合重建"):
        records = archive_store.posts.read_effective_post_records()
        if aid is None:
            floor_labels = FloorLabels.plain()
        else:
            try:
                floor_labels = load_floor_labels_from_archive(archive_store, aid)
            except Exception as error:
                report_warning(
                    WarningCategory.FLOOR_MAP,
                    f"无法加载楼层映射，使用普通楼层标签：{error}",
                )
                floor_labels = FloorLabels.plain()
        image_processing = download_images_for_records(
            tid,
            aid,
            archive_store,
            floor_labels,
            records,
            pending_image_retries,
            force_image_retries=False,
        )
    fingerprints_after = (
        archive_store.overlays.post_overlays_fingerprint(),
        archive_store.posts.post_version_selections_fingerprint(),
    )
    if fingerprints_after != (
        post_overlays_hash,
        post_version_selections_hash,
    ):
        return False
    snapshot = archive_store.state.read_backup_processing_snapshot()
    state = ImageReferenceState(
        format_version=IMAGE_REFERENCE_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint=post_overlays_hash,
        post_version_selections_fingerprint=post_version_selections_hash,
        image_reference_extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    return archive_store.state.commit_image_reference_state(
        state,
        image_processing.pending_image_retries,
        manifest_posts=image_processing.manifest_posts,
    )



def _new_image_reference_state(
    snapshot: BackupProcessingSnapshot,
    *,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> ImageReferenceState:
    return ImageReferenceState(
        format_version=IMAGE_REFERENCE_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint=post_overlays_hash,
        post_version_selections_fingerprint=post_version_selections_hash,
        image_reference_extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )



def _manifest_reference_summary(
    posts: tuple[ImageReferenceManifestPost, ...],
) -> tuple[Counter[str], dict[str, bool]]:
    counts: Counter[str] = Counter()
    validity_by_url: dict[str, bool] = {}
    for post in posts:
        for reference in post.references:
            previous_validity = validity_by_url.setdefault(
                reference.url,
                reference.valid,
            )
            if previous_validity != reference.valid:
                raise ValueError(
                    f"图片引用URL合法性冲突：{reference.url}"
                )
            counts[reference.url] += 1
    return counts, validity_by_url



def _image_floor_labels(
    archive_store: ThreadArchiveStore,
    aid: Optional[int],
) -> FloorLabels:
    if aid is None:
        return FloorLabels.plain()
    try:
        return load_floor_labels_from_archive(archive_store, aid)
    except Exception as error:
        report_warning(
            WarningCategory.FLOOR_MAP,
            f"无法加载楼层映射，使用普通楼层标签：{error}",
        )
        return FloorLabels.plain()



def _image_tasks_for_urls(urls: set[str]) -> list[ImageDownloadTask]:
    return [{"url": url} for url in sorted(urls)]



def _incremental_image_reference_prerequisites_hold(
    changes: ArchiveIncrementalChanges,
    snapshot: BackupProcessingSnapshot,
    *,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> bool:
    previous_snapshot = changes.previous_snapshot
    if (
        previous_snapshot is None
        or previous_snapshot.image_state is None
        or not changes.changed_lous
        or not changes.added_lous <= changes.changed_lous
    ):
        return False
    if not image_state_is_current(
        previous_snapshot,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    ):
        return False
    expected_revision = (
        previous_snapshot.change_state.archive_revision
        + changes.archive_revision_increments
    )
    return snapshot.change_state.archive_revision == expected_revision



def try_incremental_image_reference_update(
    tid: int,
    aid: Optional[int],
    archive_store: ThreadArchiveStore,
    *,
    snapshot: BackupProcessingSnapshot,
    changes: ArchiveIncrementalChanges,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> Literal["delta", "bootstrap"] | None:
    if not _incremental_image_reference_prerequisites_hold(
        changes,
        snapshot,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    ):
        return None
    previous_snapshot = changes.previous_snapshot
    assert previous_snapshot is not None
    expected_image_state = previous_snapshot.image_state
    assert expected_image_state is not None

    try:
        manifest_state = archive_store.state.read_image_reference_manifest_state()
        floor_labels = _image_floor_labels(archive_store, aid)
        if manifest_state is None:
            with time_section("图片引用清单懒初始化"):
                with time_section("清单初始化归档读取"):
                    records = archive_store.posts.read_effective_post_records()
                with time_section("清单初始化Overlay应用"):
                    effective_records = _apply_post_overlays_to_records(
                        archive_store.overlays.read_post_overlays(),
                        records,
                        output_dir=archive_store.thread_folder.parent,
                    )
                collection = _collect_image_download_tasks_for_records(
                    archive_store,
                    effective_records,
                    floor_labels,
                    task_lous=set(changes.changed_lous),
                    include_cache_misses_in_tasks=True,
                )
                manifest_counts, manifest_validity = _manifest_reference_summary(
                    collection.manifest_posts
                )
                retained_pending = tuple(
                    retry
                    for retry in snapshot.pending_image_retries
                    if manifest_counts[retry.url] > 0
                    and manifest_validity.get(retry.url, False)
                )
                candidate_urls = {
                    task["url"] for task in collection.tasks
                } | {retry.url for retry in retained_pending}
                record_timing_metric(
                    "图片引用增量楼层数",
                    len(changes.changed_lous),
                )
                record_timing_metric(
                    "图片引用清单楼层数",
                    len(collection.manifest_posts),
                )
                record_timing_metric(
                    "图片引用清单记录数",
                    sum(
                        len(post.references)
                        for post in collection.manifest_posts
                    ),
                )
                record_timing_metric(
                    "图片增量候选URL数",
                    len(candidate_urls),
                )
                download_result = download_images_with_retry_policy(
                    tid,
                    aid,
                    _image_tasks_for_urls(candidate_urls),
                    retained_pending,
                    force=False,
                )
            fingerprints_after = (
                archive_store.overlays.post_overlays_fingerprint(),
                archive_store.posts.post_version_selections_fingerprint(),
            )
            latest_snapshot = archive_store.state.read_backup_processing_snapshot()
            if (
                fingerprints_after
                != (post_overlays_hash, post_version_selections_hash)
                or latest_snapshot.change_state != snapshot.change_state
            ):
                return None
            new_state = _new_image_reference_state(
                latest_snapshot,
                post_overlays_hash=post_overlays_hash,
                post_version_selections_hash=post_version_selections_hash,
            )
            if not archive_store.state.commit_bootstrapped_image_reference_state(
                expected_image_state,
                new_state,
                download_result.pending_image_retries,
                collection.manifest_posts,
            ):
                return None
            return "bootstrap"

        if (
            manifest_state.format_version != IMAGE_REFERENCE_MANIFEST_VERSION
            or manifest_state.processed_archive_revision
            != expected_image_state.processed_archive_revision
        ):
            return None

        with time_section("图片引用清单增量更新"):
            old_posts_by_lou = (
                archive_store.state.read_image_reference_manifest_posts(
                    set(changes.changed_lous)
                )
            )
            missing_old_lous = (
                changes.changed_lous
                - changes.added_lous
                - old_posts_by_lou.keys()
            )
            unexpected_old_lous = changes.added_lous & old_posts_by_lou.keys()
            if missing_old_lous or unexpected_old_lous:
                raise ValueError(
                    "图片引用清单楼层集与归档变更不一致："
                    f"missing={sorted(missing_old_lous)}, "
                    f"unexpected={sorted(unexpected_old_lous)}"
                )
            with time_section("增量图片引用归档读取"):
                records = archive_store.posts.read_effective_post_records(
                    set(changes.changed_lous)
                )
            with time_section("增量图片引用Overlay应用"):
                effective_records = _apply_post_overlays_to_records(
                    archive_store.overlays.read_post_overlays(),
                    records,
                    output_dir=archive_store.thread_folder.parent,
                )
            collection = _collect_image_download_tasks_for_records(
                archive_store,
                effective_records,
                floor_labels,
            )
            new_posts_by_lou = {
                post.lou: post for post in collection.manifest_posts
            }
            if new_posts_by_lou.keys() != changes.changed_lous:
                raise ValueError(
                    "增量图片引用读取的楼层集与归档变更不一致。"
                )

            old_posts = tuple(old_posts_by_lou.values())
            new_posts = tuple(
                new_posts_by_lou[lou] for lou in sorted(new_posts_by_lou)
            )
            old_counts, old_validity = _manifest_reference_summary(old_posts)
            new_counts, new_validity = _manifest_reference_summary(new_posts)
            queried_urls = (
                set(old_counts)
                | set(new_counts)
                | {retry.url for retry in snapshot.pending_image_retries}
            )
            stored_counts = (
                archive_store.state.read_image_reference_manifest_url_counts(
                    queried_urls
                )
            )
            validity_by_url = {
                url: valid for url, (_count, valid) in stored_counts.items()
            }
            for source in (old_validity, new_validity):
                for url, valid in source.items():
                    previous_validity = validity_by_url.setdefault(url, valid)
                    if previous_validity != valid:
                        raise ValueError(
                            f"图片引用URL合法性冲突：{url}"
                        )
            for url, removed_count in old_counts.items():
                stored = stored_counts.get(url)
                if stored is None or stored[0] < removed_count:
                    raise ValueError(
                        f"图片引用清单URL计数无效：{url}"
                    )
            for retry in snapshot.pending_image_retries:
                if retry.url not in stored_counts and retry.url not in new_counts:
                    raise ValueError(
                        f"待重试图片URL不在引用清单中：{retry.url}"
                    )

            def updated_reference_count(url: str) -> int:
                return (
                    stored_counts.get(url, (0, False))[0]
                    - old_counts[url]
                    + new_counts[url]
                )

            newly_referenced_urls = {
                url
                for url in new_counts
                if url not in stored_counts and new_validity[url]
            }
            retained_pending = tuple(
                retry
                for retry in snapshot.pending_image_retries
                if updated_reference_count(retry.url) > 0
                and validity_by_url.get(retry.url, False)
            )
            candidate_urls = newly_referenced_urls | {
                retry.url for retry in retained_pending
            }
            record_timing_metric(
                "图片引用增量楼层数",
                len(changes.changed_lous),
            )
            record_timing_metric(
                "图片引用清单记录数",
                sum(len(post.references) for post in new_posts),
            )
            record_timing_metric(
                "图片增量候选URL数",
                len(candidate_urls),
            )
            download_result = download_images_with_retry_policy(
                tid,
                aid,
                _image_tasks_for_urls(candidate_urls),
                retained_pending,
                force=False,
            )

        fingerprints_after = (
            archive_store.overlays.post_overlays_fingerprint(),
            archive_store.posts.post_version_selections_fingerprint(),
        )
        latest_snapshot = archive_store.state.read_backup_processing_snapshot()
        if (
            fingerprints_after
            != (post_overlays_hash, post_version_selections_hash)
            or latest_snapshot.change_state != snapshot.change_state
        ):
            return None
        new_state = _new_image_reference_state(
            latest_snapshot,
            post_overlays_hash=post_overlays_hash,
            post_version_selections_hash=post_version_selections_hash,
        )
        if not archive_store.state.commit_incremental_image_reference_state(
            expected_image_state,
            new_state,
            download_result.pending_image_retries,
            new_posts,
        ):
            return None
        return "delta"
    except ValueError as error:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            f"图片引用清单无法增量更新，改为完整重建：{error}",
        )
        return None

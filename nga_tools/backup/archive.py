from __future__ import annotations

import datetime
from collections import Counter
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
    unresolved_missing_author_lous_from_stored_floor_map,
)
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    PAGE_JSON_RE,
)
from nga_tools.backup.image_pipeline import (
    ImageDownloadOutcome,
    download_images_compact as _download_images,
)
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
)
from nga_tools.backup.post_version_selection import selections_fingerprint
from nga_tools.backup.processing_state import (
    FLOOR_PROCESSING_STATE_VERSION,
    IMAGE_REFERENCE_STATE_VERSION,
    IMAGE_REFERENCE_MANIFEST_VERSION,
    BackupProcessingSnapshot,
    FloorProcessingState,
    ImageReferenceManifestPost,
    ImageReferenceState,
)
from nga_tools.console import (
    WarningCategory,
    report_info,
    report_progress,
    report_warning,
)
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


@dataclass(frozen=True)
class _ImageRecordProcessingResult:
    download_summary: ImageDownloadOutcome
    manifest_posts: tuple[ImageReferenceManifestPost, ...]


@dataclass(frozen=True)
class _ArchiveIncrementalChanges:
    previous_snapshot: BackupProcessingSnapshot | None
    changed_lous: frozenset[int]
    added_lous: frozenset[int]
    archive_revision_increments: int


@dataclass(frozen=True)
class _FloorStateRefreshResult:
    succeeded: bool
    snapshot: BackupProcessingSnapshot
    changed_lous: frozenset[int]
    added_lous: frozenset[int]


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
    inputs = archive_store.read_author_floor_refresh_inputs()
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
    recovered_result = archive_store.upsert_recovered_posts(
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
    archive_store: ThreadArchiveStore,
    floor_labels: FloorLabels,
    records: list[PostRecord],
) -> _ImageRecordProcessingResult:
    with time_section("Overlay应用"):
        effective_records = _apply_post_overlays_to_records(
            archive_store.read_post_overlays(),
            records,
            output_dir=archive_store.thread_folder.parent,
        )
    collection = _collect_image_download_tasks_for_records(
        archive_store,
        effective_records,
        floor_labels,
    )
    return _ImageRecordProcessingResult(
        download_summary=_download_images(tid, aid, collection.tasks),
        manifest_posts=collection.manifest_posts,
    )


def _failed_image_urls(download_summary: ImageDownloadOutcome) -> set[str]:
    return {item["url"] for item in download_summary.failed}


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


def _floor_state_is_current(
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


def _image_state_is_current(
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


def _refresh_author_floor_state(
    client: NGAClient,
    archive_store: ThreadArchiveStore,
    tid: int,
    aid: int,
    *,
    page_count: int,
    author_total_lou_count: int | None,
    expected_snapshot: BackupProcessingSnapshot,
    commit_even_if_unchanged: bool = True,
) -> _FloorStateRefreshResult:
    post_refs, missing_lous = _author_post_refs_and_missing_lous(
        archive_store,
        author_total_lou_count,
    )
    record_timing_metric("待恢复缺失楼数", len(missing_lous))
    if not missing_lous and not commit_even_if_unchanged:
        record_timing_metric("本次恢复缺失楼数", 0)
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
        )
    with time_section("恢复正文事务写入"):
        recovered_result = archive_store.upsert_recovered_posts(
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
    with time_section("处理状态快照重读"):
        snapshot = archive_store.read_backup_processing_snapshot()
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
        committed = archive_store.commit_floor_processing_state(
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


def _rebuild_image_reference_state(
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    *,
    post_overlays_hash: str,
    post_version_selections_hash: str,
) -> bool:
    with time_section("图片引用集合重建"):
        records = archive_store.read_effective_post_records()
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
        image_processing = _download_images_for_records(
            tid,
            aid,
            archive_store,
            floor_labels,
            records,
        )
    fingerprints_after = (
        archive_store.post_overlays_fingerprint(),
        selections_fingerprint(thread_folder),
    )
    if fingerprints_after != (
        post_overlays_hash,
        post_version_selections_hash,
    ):
        return False
    snapshot = archive_store.read_backup_processing_snapshot()
    state = ImageReferenceState(
        format_version=IMAGE_REFERENCE_STATE_VERSION,
        processed_archive_revision=snapshot.change_state.archive_revision,
        post_overlays_fingerprint=post_overlays_hash,
        post_version_selections_fingerprint=post_version_selections_hash,
        image_reference_extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
        completed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
    )
    return archive_store.commit_image_reference_state(
        state,
        _failed_image_urls(image_processing.download_summary),
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
    changes: _ArchiveIncrementalChanges,
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
    if not _image_state_is_current(
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


def _try_incremental_image_reference_update(
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
    archive_store: ThreadArchiveStore,
    *,
    snapshot: BackupProcessingSnapshot,
    changes: _ArchiveIncrementalChanges,
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
        manifest_state = archive_store.read_image_reference_manifest_state()
        floor_labels = _image_floor_labels(archive_store, aid)
        if manifest_state is None:
            with time_section("图片引用清单懒初始化"):
                with time_section("清单初始化归档读取"):
                    records = archive_store.read_effective_post_records()
                with time_section("清单初始化Overlay应用"):
                    effective_records = _apply_post_overlays_to_records(
                        archive_store.read_post_overlays(),
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
                retained_pending = {
                    url
                    for url in snapshot.pending_image_urls
                    if manifest_counts[url] > 0 and manifest_validity.get(url, False)
                }
                candidate_urls = {
                    task["url"] for task in collection.tasks
                } | retained_pending
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
                download_summary = _download_images(
                    tid,
                    aid,
                    _image_tasks_for_urls(candidate_urls),
                )
            fingerprints_after = (
                archive_store.post_overlays_fingerprint(),
                selections_fingerprint(thread_folder),
            )
            latest_snapshot = archive_store.read_backup_processing_snapshot()
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
            if not archive_store.commit_bootstrapped_image_reference_state(
                expected_image_state,
                new_state,
                _failed_image_urls(download_summary),
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
                archive_store.read_image_reference_manifest_posts(
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
                records = archive_store.read_effective_post_records(
                    set(changes.changed_lous)
                )
            with time_section("增量图片引用Overlay应用"):
                effective_records = _apply_post_overlays_to_records(
                    archive_store.read_post_overlays(),
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
                | set(snapshot.pending_image_urls)
            )
            stored_counts = (
                archive_store.read_image_reference_manifest_url_counts(
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
            for url in snapshot.pending_image_urls:
                if url not in stored_counts and url not in new_counts:
                    raise ValueError(
                        f"待重试图片URL不在引用清单中：{url}"
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
            retained_pending = {
                url
                for url in snapshot.pending_image_urls
                if updated_reference_count(url) > 0
                and validity_by_url.get(url, False)
            }
            candidate_urls = newly_referenced_urls | retained_pending
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
            download_summary = _download_images(
                tid,
                aid,
                _image_tasks_for_urls(candidate_urls),
            )

        fingerprints_after = (
            archive_store.post_overlays_fingerprint(),
            selections_fingerprint(thread_folder),
        )
        latest_snapshot = archive_store.read_backup_processing_snapshot()
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
        if not archive_store.commit_incremental_image_reference_state(
            expected_image_state,
            new_state,
            _failed_image_urls(download_summary),
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
    processing_snapshot: BackupProcessingSnapshot | None = None,
    incremental_changes: _ArchiveIncrementalChanges | None = None,
) -> ProcessingStateReuseResult:
    if not local_pages_cover_remote:
        return ProcessingStateReuseResult(False, "local_pages_incomplete")

    try:
        if processing_snapshot is None:
            with time_section("处理状态元数据读取"):
                snapshot = archive_store.read_backup_processing_snapshot()
        else:
            snapshot = processing_snapshot
    except ValueError as error:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            f"处理状态无效，改为完整处理：{error}",
        )
        archive_store.clear_backup_processing_state()
        return ProcessingStateReuseResult(False, "state_invalid")
    post_overlays_hash = archive_store.post_overlays_fingerprint()
    post_version_selections_hash = selections_fingerprint(thread_folder)
    changes = (
        _ArchiveIncrementalChanges(None, frozenset(), frozenset(), 0)
        if incremental_changes is None
        else incremental_changes
    )
    floor_hit = _floor_state_is_current(
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
            )
            snapshot = floor_refresh.snapshot
            if not floor_refresh.succeeded:
                return ProcessingStateReuseResult(False, "floor_map_changed")
            changes = _ArchiveIncrementalChanges(
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
                    commit_even_if_unchanged=False,
                )
                snapshot = floor_refresh.snapshot
                if not floor_refresh.succeeded:
                    return ProcessingStateReuseResult(False, "floor_map_changed")
                changes = _ArchiveIncrementalChanges(
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

    image_hit = _image_state_is_current(
        snapshot,
        post_overlays_hash=post_overlays_hash,
        post_version_selections_hash=post_version_selections_hash,
    )
    record_timing_metric("图片引用状态复用命中", int(image_hit))
    if not image_hit or snapshot.image_state is None:
        incremental_mode = _try_incremental_image_reference_update(
            tid,
            aid,
            thread_folder,
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
        if not _rebuild_image_reference_state(
            tid,
            aid,
            thread_folder,
            archive_store,
            post_overlays_hash=post_overlays_hash,
            post_version_selections_hash=post_version_selections_hash,
        ):
            return ProcessingStateReuseResult(False, "archive_changed")
        return ProcessingStateReuseResult(True, "hit")

    record_timing_label(
        "图片引用状态复用结果",
        "image_collection_hit",
    )
    record_timing_label("图片引用处理模式", "hit")
    report_info("归档与派生输入未变化，跳过完整处理。")
    record_timing_metric("待重试图片URL数", len(snapshot.pending_image_urls))
    pending_tasks: list[ImageDownloadTask] = [
        {"url": url} for url in snapshot.pending_image_urls
    ]
    with time_section("未完成图片重试"):
        download_summary = _download_images(tid, aid, pending_tasks)
    if archive_store.replace_pending_images_for_image_state(
        snapshot.image_state,
        _failed_image_urls(download_summary),
    ):
        return ProcessingStateReuseResult(True, "hit")

    report_warning(
        WarningCategory.PROCESSING_STATE,
        "处理状态在图片重试期间发生变化，改为完整处理。",
    )
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
    download_summary: ImageDownloadOutcome,
    manifest_posts: tuple[ImageReferenceManifestPost, ...],
) -> None:
    pending_image_urls = _failed_image_urls(download_summary)
    record_timing_metric("待重试图片URL数", len(pending_image_urls))
    if aid is not None and not floor_map_processing.cacheable:
        report_info("楼层映射本次未形成可复用状态，下次继续完整处理。")
        return
    fingerprints_after = (
        archive_store.post_overlays_fingerprint(),
        selections_fingerprint(thread_folder),
    )
    if fingerprints_after != fingerprints_before:
        report_warning(
            WarningCategory.PROCESSING_STATE,
            "派生输入在处理期间发生变化，未写入线程级处理状态。",
        )
        return

    snapshot = archive_store.read_backup_processing_snapshot()
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
    floor_committed = archive_store.commit_floor_processing_state(floor_state)
    image_committed = archive_store.commit_image_reference_state(
        image_state,
        pending_image_urls,
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
    record_timing_label("图片引用处理模式", "full")
    fingerprints_before = (
        archive_store.post_overlays_fingerprint(),
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
        image_processing = _download_images_for_records(
            tid,
            aid,
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
        download_summary=image_processing.download_summary,
        manifest_posts=image_processing.manifest_posts,
    )


def _record_archive_upsert_metrics(result: ArchivePagesUpsertResult) -> None:
    record_timing_metric("归档批量写入页数", result.pages_processed)
    record_timing_metric("归档新增页快照数", result.page_snapshots_inserted)
    record_timing_metric("归档新增帖子版本数", result.post_versions_inserted)
    record_timing_metric("归档写入楼层观测数", result.post_observations)
    record_timing_metric("归档有效变更页数", result.effective_changed_pages)
    record_timing_metric(
        "归档有效变更楼层数",
        len(result.effective_changed_lous),
    )


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
    processing_snapshot: BackupProcessingSnapshot | None = None,
    incremental_changes: _ArchiveIncrementalChanges | None = None,
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
                processing_snapshot=processing_snapshot,
                incremental_changes=incremental_changes,
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
    archive_store.ensure_backup_processing_schema()

    try:
        snapshot = archive_store.read_backup_processing_snapshot()
    except ValueError:
        return "refresh"
    if snapshot.floor_state is None or snapshot.image_state is None:
        return "refresh"

    try:
        pagination = archive_store.read_latest_page_one_pagination()
    except ValueError:
        return "refresh"
    if pagination is None:
        return "refresh"
    author_total_lou_count = pagination.vrows if aid is not None else None

    floor_current = _floor_state_is_current(
        snapshot,
        page_count=pagination.page_count,
        author_total_lou_count=author_total_lou_count,
    )
    image_current = _image_state_is_current(
        snapshot,
        post_overlays_hash=archive_store.post_overlays_fingerprint(),
        post_version_selections_hash=selections_fingerprint(thread_folder),
    )
    if not floor_current or not image_current or snapshot.pending_image_urls:
        return "maintenance"
    if aid is not None and read_unresolved_missing_author_lous_from_archive(
        archive_store,
        total_lou_count=author_total_lou_count,
    ):
        return "maintenance"
    return None


def _read_incremental_base_snapshot(
    archive_store: ThreadArchiveStore,
) -> BackupProcessingSnapshot | None:
    try:
        return archive_store.read_backup_processing_snapshot()
    except ValueError:
        return None


def maintain_thread_backup(tid: int, aid: Optional[int]) -> None:
    thread_folder = Path(utils.get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    with time_section("处理状态Schema兼容检查"):
        archive_store.ensure_backup_processing_schema()
    with time_section("处理状态元数据读取"):
        snapshot = archive_store.read_backup_processing_snapshot()
    if snapshot.floor_state is None or snapshot.image_state is None:
        raise RuntimeError("缺少线程级处理状态，必须先执行增量备份。")
    pagination = archive_store.read_latest_page_one_pagination()
    if pagination is None:
        raise RuntimeError("归档缺少第一页分页元数据，必须先执行增量备份。")
    author_total_lou_count = pagination.vrows if aid is not None else None
    existing_page_numbers = archive_store.read_page_numbers()
    local_pages_cover_remote = set(
        range(1, pagination.page_count + 1)
    ) <= existing_page_numbers

    with time_section("客户端初始化"):
        client = NGAClient()
    reuse_result = _reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        thread_folder,
        archive_store,
        page_count=pagination.page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=local_pages_cover_remote,
        force_processing=False,
        processing_snapshot=snapshot,
        incremental_changes=_ArchiveIncrementalChanges(
            snapshot,
            frozenset(),
            frozenset(),
            0,
        ),
    )
    if reuse_result.hit:
        return

    _run_full_processing(
        client,
        archive_store,
        thread_folder,
        tid,
        aid,
        page_count=pagination.page_count,
        author_total_lou_count=author_total_lou_count,
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
    previous_processing_snapshot = _read_incremental_base_snapshot(
        archive_store
    )
    with time_section("归档页面准备与事务写入"):
        upsert_result = _upsert_archive_pages(archive_store, page_data_by_page)
    _record_archive_upsert_metrics(upsert_result)
    with time_section("归档字数回填"):
        refreshed_word_counts = archive_store.refresh_stored_word_counts()
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

    reuse_result = _reuse_processing_state_after_page_refresh(
        client,
        tid,
        aid,
        thread_folder,
        archive_store,
        page_count=page_count,
        author_total_lou_count=author_total_lou_count,
        local_pages_cover_remote=local_pages_cover_remote,
        force_processing=force_processing,
        incremental_changes=_ArchiveIncrementalChanges(
            previous_processing_snapshot,
            upsert_result.effective_changed_lous,
            upsert_result.effective_added_lous,
            int(bool(upsert_result.effective_changed_lous)),
        ),
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
    previous_processing_snapshot = _read_incremental_base_snapshot(
        archive_store
    )
    previous_floor_state = (
        previous_processing_snapshot.floor_state
        if previous_processing_snapshot is not None and existing_page_numbers
        else None
    )

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
            and previous_floor_state is not None
            and previous_floor_state.page_count == page_count
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
            with time_section("智能增量第一页变更预检"):
                first_page_changed = (
                    archive_store.page_effective_processing_inputs_changed(
                        1,
                        first_page_data,
                    )
                )
            if first_page_changed:
                with time_section("智能增量尾页回退抓取"):
                    tail_start = min(max(existing_page_numbers), page_count)
                    fallback_page_numbers = (
                        set(range(tail_start, page_count + 1)) - {1}
                    )
                    for page_number in sorted(fallback_page_numbers):
                        page_data_by_page[page_number] = _fetch_backup_page(
                            client,
                            tid,
                            aid,
                            page_number,
                            page_count,
                            first_page_data,
                        )
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

    available_page_numbers = existing_page_numbers | set(page_data_by_page)
    archived_page_data_count = len(page_data_by_page)
    page_data_by_page.clear()
    del page_data_by_page, first_page_data
    cleared_client_page_count = client.clear_page_cache()
    record_timing_metric(
        "归档页面内存释放页数",
        max(archived_page_data_count, cleared_client_page_count),
    )
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
        incremental_changes=_ArchiveIncrementalChanges(
            previous_processing_snapshot,
            upsert_result.effective_changed_lous,
            upsert_result.effective_added_lous,
            int(bool(upsert_result.effective_changed_lous)),
        ),
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

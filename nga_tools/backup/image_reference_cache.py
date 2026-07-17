from __future__ import annotations

import json
from dataclasses import dataclass
from typing import cast

from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.backup.archive_store import (
    PostImageReferenceCacheEntry,
    ThreadArchiveStore,
)
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.image_pipeline import (
    PostImageReference,
    PostImageReferenceScan,
    collect_image_download_tasks_from_scans,
    parse_post_htmls_for_images,
    scan_post_image_references,
)
from nga_tools.backup.image_store import ImageDownloadTask
from nga_tools.backup.models import PostRecord
from nga_tools.backup.processing_state import (
    ImageReferenceManifestEntry,
    ImageReferenceManifestPost,
)
from nga_tools.backup.post_html import load_post_htmls_for_records
from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.hashing import hash_object, hash_text
from nga_tools.timing import record_timing_metric, time_section


IMAGE_REFERENCE_EXTRACTOR_VERSION = 2


@dataclass(frozen=True)
class ImageReferenceCollectionResult:
    tasks: list[ImageDownloadTask]
    manifest_posts: tuple[ImageReferenceManifestPost, ...]
    record_count: int
    cache_hit_count: int
    cache_miss_count: int
    cache_miss_lous: frozenset[int]


@dataclass(frozen=True)
class ReadOnlyImageReferenceScanResult:
    scans: list[PostImageReferenceScan]
    cache_hit_count: int
    cache_miss_count: int


@dataclass(frozen=True)
class _RecordCacheTarget:
    record: PostRecord
    cache_key: str


def image_reference_cache_key(record: PostRecord) -> str:
    identity: dict[str, object] = {
        "extractor_version": IMAGE_REFERENCE_EXTRACTOR_VERSION,
        "source_hash": record["source_hash"],
    }
    post = record["post"]
    if post is not None:
        identity["render_kind"] = "post"
    else:
        rendered_html = record["html"]
        if rendered_html is None:
            raise RuntimeError(f"缺少第{record['lou']}楼的可缓存HTML。")
        identity["render_kind"] = "html"
        identity["rendered_html_hash"] = hash_text(rendered_html)
    return hash_object(identity)


def serialize_image_references(
    references: tuple[PostImageReference, ...],
) -> str:
    return json.dumps(
        [
            {
                "image_index": reference.image_index,
                "url": reference.url,
                "valid": reference.valid,
            }
            for reference in references
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def deserialize_image_references(value: str) -> tuple[PostImageReference, ...]:
    try:
        raw_references: object = json.loads(value)
    except json.JSONDecodeError as error:
        raise ValueError("图片引用缓存不是有效JSON") from error
    if not isinstance(raw_references, list):
        raise ValueError("图片引用缓存顶层必须是数组")

    references: list[PostImageReference] = []
    previous_image_index = 0
    for raw_reference in cast(list[object], raw_references):
        if not isinstance(raw_reference, dict):
            raise ValueError("图片引用缓存项必须是对象")
        reference = cast(dict[object, object], raw_reference)
        image_index = reference.get("image_index")
        url = reference.get("url")
        valid = reference.get("valid")
        if (
            type(image_index) is not int
            or image_index <= previous_image_index
            or not isinstance(url, str)
            or type(valid) is not bool
            or valid != NGA_img_link_verify(url)
        ):
            raise ValueError("图片引用缓存项字段无效")
        references.append(
            PostImageReference(
                image_index=image_index,
                url=url,
                valid=valid,
            )
        )
        previous_image_index = image_index
    return tuple(references)


def _read_cached_references(
    archive_store: ThreadArchiveStore,
    targets: list[_RecordCacheTarget],
) -> tuple[dict[str, tuple[PostImageReference, ...]], bool]:
    with time_section("图片引用缓存批量查询"):
        try:
            cached_entries = archive_store.read_post_image_reference_cache(
                {target.cache_key for target in targets}
            )
        except Exception as error:
            report_warning(
                WarningCategory.CACHE,
                f"图片引用缓存读取失败，改为完整解析：{error}",
            )
            return {}, False

    references_by_key: dict[str, tuple[PostImageReference, ...]] = {}
    source_hash_by_key = {
        target.cache_key: target.record["source_hash"] for target in targets
    }
    with time_section("图片引用缓存反序列化"):
        for cache_key, entry in cached_entries.items():
            if (
                entry.extractor_version != IMAGE_REFERENCE_EXTRACTOR_VERSION
                or entry.source_hash != source_hash_by_key.get(cache_key)
            ):
                continue
            try:
                references_by_key[cache_key] = deserialize_image_references(
                    entry.references_json
                )
            except ValueError as error:
                report_warning(
                    WarningCategory.CACHE,
                    f"图片引用缓存损坏，重新解析并覆盖：{cache_key}：{error}"
                )
    return references_by_key, True


def _first_missing_target_by_key(
    targets: list[_RecordCacheTarget],
    references_by_key: dict[str, tuple[PostImageReference, ...]],
) -> dict[str, _RecordCacheTarget]:
    missing_targets: dict[str, _RecordCacheTarget] = {}
    for target in targets:
        if target.cache_key not in references_by_key:
            missing_targets.setdefault(target.cache_key, target)
    return missing_targets


def scan_image_references_for_records_readonly(
    archive_store: ThreadArchiveStore,
    records: list[PostRecord],
) -> ReadOnlyImageReferenceScanResult:
    """Read or derive per-occurrence image references without writing caches."""
    targets = [
        _RecordCacheTarget(
            record=record,
            cache_key=image_reference_cache_key(record),
        )
        for record in records
    ]
    references_by_key, _cache_read_succeeded = _read_cached_references(
        archive_store,
        targets,
    )
    cache_hit_count = sum(
        target.cache_key in references_by_key for target in targets
    )
    missing_targets = list(
        _first_missing_target_by_key(targets, references_by_key).values()
    )
    missing_htmls = load_post_htmls_for_records(
        [target.record for target in missing_targets]
    )
    parsed_htmls = (
        parse_post_htmls_for_images(missing_htmls) if missing_htmls else []
    )
    for target, parsed_html in zip(missing_targets, parsed_htmls, strict=True):
        references_by_key[target.cache_key] = scan_post_image_references(
            parsed_html
        ).references

    return ReadOnlyImageReferenceScanResult(
        scans=[
            PostImageReferenceScan(
                lou=target.record["lou"],
                references=references_by_key[target.cache_key],
            )
            for target in targets
        ],
        cache_hit_count=cache_hit_count,
        cache_miss_count=len(targets) - cache_hit_count,
    )


def collect_image_download_tasks_for_records(
    archive_store: ThreadArchiveStore,
    records: list[PostRecord],
    floor_labels: FloorLabels,
    *,
    task_lous: set[int] | None = None,
    include_cache_misses_in_tasks: bool = False,
) -> ImageReferenceCollectionResult:
    with time_section("图片引用缓存读取"):
        with time_section("图片引用缓存键构造"):
            targets = [
                _RecordCacheTarget(
                    record=record,
                    cache_key=image_reference_cache_key(record),
                )
                for record in records
            ]
        references_by_key, cache_read_succeeded = _read_cached_references(
            archive_store,
            targets,
        )

    cache_hit_count = sum(
        target.cache_key in references_by_key for target in targets
    )
    cache_miss_count = len(targets) - cache_hit_count
    missing_targets_by_key = _first_missing_target_by_key(
        targets,
        references_by_key,
    )
    missing_targets = list(missing_targets_by_key.values())

    with time_section("BBCode转临时HTML"):
        missing_htmls = load_post_htmls_for_records(
            [target.record for target in missing_targets]
        )

    new_entries: list[PostImageReferenceCacheEntry] = []
    with time_section("图片解析与任务收集"):
        with time_section("图片引用未命中解析"):
            parsed_htmls = (
                parse_post_htmls_for_images(missing_htmls) if missing_htmls else []
            )
            for target, parsed_html in zip(
                missing_targets,
                parsed_htmls,
                strict=True,
            ):
                scan = scan_post_image_references(parsed_html)
                references_by_key[target.cache_key] = scan.references
                new_entries.append(
                    PostImageReferenceCacheEntry(
                        cache_key=target.cache_key,
                        source_hash=target.record["source_hash"],
                        extractor_version=IMAGE_REFERENCE_EXTRACTOR_VERSION,
                        references_json=serialize_image_references(scan.references),
                    )
                )

        with time_section("图片下载任务重建"):
            scans = [
                PostImageReferenceScan(
                    lou=target.record["lou"],
                    references=references_by_key[target.cache_key],
                )
                for target in targets
            ]
            cache_miss_lous = frozenset(
                target.record["lou"]
                for target in targets
                if target.cache_key in missing_targets_by_key
            )
            effective_task_lous = None if task_lous is None else set(task_lous)
            if (
                effective_task_lous is not None
                and include_cache_misses_in_tasks
            ):
                effective_task_lous.update(cache_miss_lous)
            task_scans = (
                scans
                if effective_task_lous is None
                else [scan for scan in scans if scan.lou in effective_task_lous]
            )
            tasks = collect_image_download_tasks_from_scans(
                task_scans,
                floor_labels,
            )

    with time_section("图片引用缓存写入"):
        if cache_read_succeeded and new_entries:
            try:
                archive_store.upsert_post_image_reference_cache(new_entries)
            except Exception as error:
                report_warning(
                    WarningCategory.CACHE,
                    f"图片引用缓存写入失败，本次继续备份：{error}",
                )

    record_timing_metric("图片引用记录数", len(records))
    record_timing_metric("图片引用缓存命中记录数", cache_hit_count)
    record_timing_metric("图片引用缓存未命中记录数", cache_miss_count)
    return ImageReferenceCollectionResult(
        tasks=tasks,
        manifest_posts=tuple(
            ImageReferenceManifestPost(
                lou=target.record["lou"],
                cache_key=target.cache_key,
                references=tuple(
                    ImageReferenceManifestEntry(
                        image_index=reference.image_index,
                        url=reference.url,
                        valid=reference.valid,
                    )
                    for reference in references_by_key[target.cache_key]
                ),
            )
            for target in targets
        ),
        record_count=len(records),
        cache_hit_count=cache_hit_count,
        cache_miss_count=cache_miss_count,
        cache_miss_lous=cache_miss_lous,
    )

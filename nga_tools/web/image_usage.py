from __future__ import annotations

import datetime
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import quote

from nga_tools.backup import image_store
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.backup.image_reference_cache import (
    scan_image_references_for_records_readonly,
)
from nga_tools.backup.post_overlay import apply_post_overlays_to_records
from nga_tools.core.sqlite import configure_readonly_connection
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)

ImageUsageSort = Literal["usage", "threads"]
PostDate = int | str
_THREAD_DIR_RE = re.compile(r"^(\d+)_(all|\d+)$")


class ImageUsageItem(TypedDict):
    relativePath: str
    fileUrl: str
    sourceUrl: str
    mappingCount: int
    usageCount: int
    replyCount: int
    threadCount: int


class SkippedImageUsageArchive(TypedDict):
    dirName: str
    message: str


class ImageUsageResult(TypedDict):
    items: list[ImageUsageItem]
    total: int
    offset: int
    limit: int
    sort: ImageUsageSort
    computedAt: str
    archiveCount: int
    postCount: int
    referenceCount: int
    mappedReferenceCount: int
    unmappedReferenceCount: int
    skippedArchives: list[SkippedImageUsageArchive]


class ImageUsageThreadGroup(TypedDict):
    tid: int
    title: str
    usageCount: int
    replyCount: int


class ImageUsageDetailResult(TypedDict):
    item: ImageUsageItem
    threads: list[ImageUsageThreadGroup]


class ImageUsageReplyItem(TypedDict):
    tid: int
    aidKey: str
    dirName: str
    pid: int
    lou: int
    floorLabel: str
    authorName: Optional[str]
    postdate: Optional[PostDate]
    occurrenceCount: int
    html: str
    readerUrl: str


class ImageUsageRepliesResult(TypedDict):
    items: list[ImageUsageReplyItem]
    total: int
    offset: int
    limit: int


@dataclass(frozen=True, slots=True)
class ImageReplyReference:
    tid: int
    aid_key: str
    dir_name: str
    pid: int
    lou: int
    occurrence_count: int


@dataclass(frozen=True)
class ImageUsageSnapshot:
    items_by_usage: list[ImageUsageItem]
    items_by_threads: list[ImageUsageItem]
    items_by_path: dict[str, ImageUsageItem]
    references_by_path: dict[str, tuple[ImageReplyReference, ...]]
    thread_titles: dict[int, str]
    computed_at: str
    archive_count: int
    post_count: int
    reference_count: int
    mapped_reference_count: int
    unmapped_reference_count: int
    skipped_archives: list[SkippedImageUsageArchive]


class ImageIndexUnavailableError(Exception):
    pass


class ImageUsageNotFoundError(Exception):
    pass


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _parse_thread_folder(path: Path) -> tuple[int, str] | None:
    match = _THREAD_DIR_RE.fullmatch(path.name)
    if match is None:
        return None
    return int(match.group(1)), match.group(2)


def _thread_folder_groups(output_dir: Path) -> dict[int, list[Path]]:
    groups: dict[int, list[Path]] = defaultdict(list)
    if not output_dir.is_dir():
        return groups
    for path in output_dir.iterdir():
        parsed = _parse_thread_folder(path)
        if (
            parsed is not None
            and path.is_dir()
            and (path / ARCHIVE_DB_FILENAME).is_file()
        ):
            groups[parsed[0]].append(path)
    def sort_key(path: Path) -> tuple[int, str]:
        parsed = _parse_thread_folder(path)
        return (0 if parsed is not None and parsed[1] == "all" else 1, path.name)

    for paths in groups.values():
        paths.sort(key=sort_key)
    return dict(sorted(groups.items()))


def _thread_metadata() -> dict[tuple[int, str], ThreadConfig]:
    metadata: dict[tuple[int, str], ThreadConfig] = {}
    for item in NGAThreadConfigs().get_thread_configs():
        aid = thread_config_aid(item)
        metadata[(thread_config_tid(item), "all" if aid is None else str(aid))] = item
    return metadata


def _thread_title(
    tid: int,
    paths: list[Path],
    metadata: dict[tuple[int, str], ThreadConfig],
) -> str:
    for path in paths:
        parsed = _parse_thread_folder(path)
        if parsed is None:
            continue
        item = metadata.get((tid, parsed[1]))
        if item is None:
            continue
        subject = item.get("subject")
        if isinstance(subject, str) and subject.strip():
            return subject
        try:
            name = thread_config_name(item)
        except ValueError:
            continue
        if name.strip():
            return name
    return f"tid {tid}"


def _image_index_connection(output_dir: Path) -> sqlite3.Connection:
    db_path = (output_dir / image_store.IMAGE_INDEX_FILENAME).resolve()
    if not db_path.is_file():
        raise ImageIndexUnavailableError(f"缺少{image_store.IMAGE_INDEX_FILENAME}。")
    connection = sqlite3.connect(
        f"file:{quote(str(db_path), safe='/:')}?mode=ro",
        uri=True,
    )
    configure_readonly_connection(connection)
    return connection


def _image_paths_for_urls(
    connection: sqlite3.Connection,
    urls: set[str],
) -> dict[str, str]:
    mappings: dict[str, str] = {}
    sorted_urls = sorted(urls)
    for start in range(0, len(sorted_urls), 900):
        chunk = sorted_urls[start : start + 900]
        placeholders = ",".join("?" for _ in chunk)
        rows = connection.execute(
            f"""
            SELECT url, unique_rel_path
            FROM image_mappings
            WHERE url IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        for raw_url, raw_path in rows:
            if isinstance(raw_url, str) and isinstance(raw_path, str):
                mappings[raw_url] = raw_path
    return mappings


@dataclass(frozen=True)
class _ReferenceScanResult:
    usage_counts: Counter[str]
    references_by_path: dict[str, list[ImageReplyReference]]
    archive_count: int
    post_count: int
    reference_count: int
    mapped_reference_count: int
    skipped_archives: list[SkippedImageUsageArchive]


def _scan_references(
    connection: sqlite3.Connection,
    thread_groups: dict[int, list[Path]],
) -> _ReferenceScanResult:
    usage_counts: Counter[str] = Counter()
    references_by_path: dict[str, list[ImageReplyReference]] = defaultdict(list)
    archive_count = 0
    post_count = 0
    reference_count = 0
    mapped_reference_count = 0
    skipped_archives: list[SkippedImageUsageArchive] = []

    for tid, thread_folders in thread_groups.items():
        seen_pids: set[int] = set()
        for thread_folder in thread_folders:
            parsed = _parse_thread_folder(thread_folder)
            if parsed is None:
                continue
            aid_key = parsed[1]
            try:
                archive_store = ThreadArchiveStore(thread_folder)
                records = archive_store.read_effective_post_records()
                records = apply_post_overlays_to_records(thread_folder, records)
                scan_result = scan_image_references_for_records_readonly(
                    archive_store,
                    records,
                )
                valid_urls = {
                    reference.url
                    for scan in scan_result.scans
                    for reference in scan.references
                    if reference.valid
                }
                image_paths = _image_paths_for_urls(connection, valid_urls)
                records_by_lou = {record["lou"]: record for record in records}
                for scan in scan_result.scans:
                    record = records_by_lou.get(scan.lou)
                    if record is None or record["pid"] is None:
                        continue
                    pid = record["pid"]
                    if pid in seen_pids:
                        continue
                    seen_pids.add(pid)
                    post_count += 1
                    valid_references = [
                        reference
                        for reference in scan.references
                        if reference.valid
                    ]
                    reference_count += len(valid_references)
                    path_counts = Counter(
                        image_paths[reference.url]
                        for reference in valid_references
                        if reference.url in image_paths
                    )
                    mapped_reference_count += sum(path_counts.values())
                    for relative_path, occurrence_count in path_counts.items():
                        usage_counts[relative_path] += occurrence_count
                        references_by_path[relative_path].append(
                            ImageReplyReference(
                                tid=tid,
                                aid_key=aid_key,
                                dir_name=thread_folder.name,
                                pid=pid,
                                lou=scan.lou,
                                occurrence_count=occurrence_count,
                            )
                        )
                archive_count += 1
            except Exception as error:
                skipped_archives.append(
                    {
                        "dirName": thread_folder.name,
                        "message": str(error) or type(error).__name__,
                    }
                )

    return _ReferenceScanResult(
        usage_counts=usage_counts,
        references_by_path=references_by_path,
        archive_count=archive_count,
        post_count=post_count,
        reference_count=reference_count,
        mapped_reference_count=mapped_reference_count,
        skipped_archives=skipped_archives,
    )


def _inventory_items(
    connection: sqlite3.Connection,
    scan_result: _ReferenceScanResult,
) -> list[ImageUsageItem]:
    inventory: dict[str, tuple[str, int]] = {}
    cursor = connection.execute("SELECT url, unique_rel_path FROM image_mappings")
    while rows := cursor.fetchmany(10_000):
        for raw_url, raw_relative_path in rows:
            if not isinstance(raw_url, str) or not isinstance(
                raw_relative_path, str
            ):
                continue
            previous = inventory.get(raw_relative_path)
            if previous is None:
                inventory[raw_relative_path] = (raw_url, 1)
            else:
                inventory[raw_relative_path] = (
                    min(previous[0], raw_url),
                    previous[1] + 1,
                )

    items: list[ImageUsageItem] = []
    for relative_path, (source_url, mapping_count) in inventory.items():
        references = scan_result.references_by_path.get(relative_path, [])
        items.append(
            {
                "relativePath": relative_path,
                "fileUrl": "/api/files/" + quote(relative_path, safe="/"),
                "sourceUrl": source_url,
                "mappingCount": mapping_count,
                "usageCount": scan_result.usage_counts.get(relative_path, 0),
                "replyCount": len(references),
                "threadCount": len({reference.tid for reference in references}),
            }
        )
    return items


def build_image_usage_snapshot(output_dir: Path) -> ImageUsageSnapshot:
    thread_groups = _thread_folder_groups(output_dir)
    metadata = _thread_metadata()
    thread_titles = {
        tid: _thread_title(tid, paths, metadata)
        for tid, paths in thread_groups.items()
    }
    try:
        with closing(_image_index_connection(output_dir)) as connection:
            connection.execute(
                "SELECT url, unique_rel_path FROM image_mappings LIMIT 0"
            ).fetchall()
            scan_result = _scan_references(connection, thread_groups)
            items = _inventory_items(connection, scan_result)
    except (sqlite3.Error, OSError) as error:
        raise ImageIndexUnavailableError(
            f"无法读取{image_store.IMAGE_INDEX_FILENAME}：{error}"
        ) from error

    items_by_usage = sorted(
        items,
        key=lambda item: (-item["usageCount"], item["relativePath"]),
    )
    items_by_threads = sorted(
        items,
        key=lambda item: (-item["threadCount"], item["relativePath"]),
    )
    return ImageUsageSnapshot(
        items_by_usage=items_by_usage,
        items_by_threads=items_by_threads,
        items_by_path={item["relativePath"]: item for item in items},
        references_by_path={
            relative_path: tuple(references)
            for relative_path, references in scan_result.references_by_path.items()
        },
        thread_titles=thread_titles,
        computed_at=_now_utc_iso(),
        archive_count=scan_result.archive_count,
        post_count=scan_result.post_count,
        reference_count=scan_result.reference_count,
        mapped_reference_count=scan_result.mapped_reference_count,
        unmapped_reference_count=(
            scan_result.reference_count - scan_result.mapped_reference_count
        ),
        skipped_archives=scan_result.skipped_archives,
    )


def _copy_item(item: ImageUsageItem) -> ImageUsageItem:
    return cast(ImageUsageItem, dict(item))


def image_usage_result(
    snapshot: ImageUsageSnapshot,
    *,
    offset: int,
    limit: int,
    sort: ImageUsageSort,
) -> ImageUsageResult:
    sorted_items = (
        snapshot.items_by_usage if sort == "usage" else snapshot.items_by_threads
    )
    return {
        "items": [
            _copy_item(item) for item in sorted_items[offset : offset + limit]
        ],
        "total": len(sorted_items),
        "offset": offset,
        "limit": limit,
        "sort": sort,
        "computedAt": snapshot.computed_at,
        "archiveCount": snapshot.archive_count,
        "postCount": snapshot.post_count,
        "referenceCount": snapshot.reference_count,
        "mappedReferenceCount": snapshot.mapped_reference_count,
        "unmappedReferenceCount": snapshot.unmapped_reference_count,
        "skippedArchives": [
            cast(SkippedImageUsageArchive, dict(item))
            for item in snapshot.skipped_archives
        ],
    }


def image_usage_detail(
    snapshot: ImageUsageSnapshot,
    relative_path: str,
) -> ImageUsageDetailResult:
    item = snapshot.items_by_path.get(relative_path)
    if item is None:
        raise ImageUsageNotFoundError("未知图片。")
    usage_by_tid: Counter[int] = Counter()
    replies_by_tid: Counter[int] = Counter()
    for reference in snapshot.references_by_path.get(relative_path, ()):
        usage_by_tid[reference.tid] += reference.occurrence_count
        replies_by_tid[reference.tid] += 1
    threads: list[ImageUsageThreadGroup] = [
        {
            "tid": tid,
            "title": snapshot.thread_titles.get(tid, f"tid {tid}"),
            "usageCount": usage_count,
            "replyCount": replies_by_tid[tid],
        }
        for tid, usage_count in usage_by_tid.items()
    ]
    threads.sort(key=lambda group: (-group["usageCount"], group["tid"]))
    return {"item": _copy_item(item), "threads": threads}


def image_reply_references(
    snapshot: ImageUsageSnapshot,
    relative_path: str,
    tid: int,
) -> list[ImageReplyReference]:
    if relative_path not in snapshot.items_by_path:
        raise ImageUsageNotFoundError("未知图片。")
    references = [
        reference
        for reference in snapshot.references_by_path.get(relative_path, ())
        if reference.tid == tid
    ]
    if not references:
        raise ImageUsageNotFoundError("该图片没有来自指定主题的引用。")
    return sorted(references, key=lambda reference: (reference.lou, reference.pid))

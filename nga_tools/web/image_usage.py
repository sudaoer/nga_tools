from __future__ import annotations

import datetime
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import quote

from nga_tools.backup import image_store
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.backup.image_reference_cache import (
    scan_image_references_for_records_readonly,
)
from nga_tools.backup.post_overlay import apply_post_overlays_to_records
from nga_tools.core.sqlite import configure_readonly_connection

_THREAD_DIR_RE = re.compile(r"^\d+_(?:all|\d+)$")


class ImageUsageItem(TypedDict):
    relativePath: str
    fileUrl: str
    sourceUrl: str
    mappingCount: int
    usageCount: int


class SkippedImageUsageArchive(TypedDict):
    dirName: str
    message: str


class ImageUsageResult(TypedDict):
    items: list[ImageUsageItem]
    total: int
    offset: int
    limit: int
    computedAt: str
    archiveCount: int
    postCount: int
    referenceCount: int
    mappedReferenceCount: int
    unmappedReferenceCount: int
    skippedArchives: list[SkippedImageUsageArchive]


@dataclass(frozen=True)
class ImageUsageSnapshot:
    items: list[ImageUsageItem]
    computed_at: str
    archive_count: int
    post_count: int
    reference_count: int
    mapped_reference_count: int
    unmapped_reference_count: int
    skipped_archives: list[SkippedImageUsageArchive]


class ImageIndexUnavailableError(Exception):
    pass


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _thread_folders(output_dir: Path) -> list[Path]:
    if not output_dir.is_dir():
        return []
    return [
        path
        for path in sorted(output_dir.iterdir(), key=lambda item: item.name)
        if path.is_dir()
        and _THREAD_DIR_RE.fullmatch(path.name) is not None
        and (path / ARCHIVE_DB_FILENAME).is_file()
    ]


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


def _scan_reference_counts(
    thread_folders: list[Path],
) -> tuple[Counter[str], int, int, list[SkippedImageUsageArchive]]:
    counts: Counter[str] = Counter()
    post_count = 0
    archive_count = 0
    skipped_archives: list[SkippedImageUsageArchive] = []
    for thread_folder in thread_folders:
        try:
            archive_store = ThreadArchiveStore(thread_folder)
            records = archive_store.read_effective_post_records()
            records = apply_post_overlays_to_records(thread_folder, records)
            scan_result = scan_image_references_for_records_readonly(
                archive_store,
                records,
            )
            for scan in scan_result.scans:
                counts.update(
                    reference.url
                    for reference in scan.references
                    if reference.valid
                )
            post_count += len(records)
            archive_count += 1
        except Exception as error:
            skipped_archives.append(
                {
                    "dirName": thread_folder.name,
                    "message": str(error) or type(error).__name__,
                }
            )
    return counts, archive_count, post_count, skipped_archives


def _inventory_items(
    output_dir: Path,
    reference_counts: Counter[str],
) -> tuple[list[ImageUsageItem], int]:
    inventory: dict[str, tuple[str, int, int]] = {}
    mapped_reference_count = 0
    try:
        with closing(_image_index_connection(output_dir)) as connection:
            cursor = connection.execute(
                "SELECT url, unique_rel_path FROM image_mappings"
            )
            while rows := cursor.fetchmany(10_000):
                for raw_url, raw_relative_path in rows:
                    if not isinstance(raw_url, str) or not isinstance(
                        raw_relative_path, str
                    ):
                        continue
                    usage_count = reference_counts.get(raw_url, 0)
                    previous = inventory.get(raw_relative_path)
                    if previous is None:
                        inventory[raw_relative_path] = (
                            raw_url,
                            1,
                            usage_count,
                        )
                    else:
                        inventory[raw_relative_path] = (
                            min(previous[0], raw_url),
                            previous[1] + 1,
                            previous[2] + usage_count,
                        )
                    mapped_reference_count += usage_count
    except (sqlite3.Error, OSError) as error:
        raise ImageIndexUnavailableError(
            f"无法读取{image_store.IMAGE_INDEX_FILENAME}：{error}"
        ) from error

    items: list[ImageUsageItem] = [
        {
            "relativePath": relative_path,
            "fileUrl": "/api/files/" + quote(relative_path, safe="/"),
            "sourceUrl": source_url,
            "mappingCount": mapping_count,
            "usageCount": usage_count,
        }
        for relative_path, (source_url, mapping_count, usage_count) in inventory.items()
    ]
    items.sort(key=lambda item: (-item["usageCount"], item["relativePath"]))
    return items, mapped_reference_count


def build_image_usage_snapshot(output_dir: Path) -> ImageUsageSnapshot:
    with closing(_image_index_connection(output_dir)) as connection:
        try:
            connection.execute(
                "SELECT url, unique_rel_path FROM image_mappings LIMIT 0"
            ).fetchall()
        except sqlite3.Error as error:
            raise ImageIndexUnavailableError(
                f"无法读取{image_store.IMAGE_INDEX_FILENAME}：{error}"
            ) from error
    thread_folders = _thread_folders(output_dir)
    reference_counts, archive_count, post_count, skipped_archives = (
        _scan_reference_counts(thread_folders)
    )
    items, mapped_reference_count = _inventory_items(
        output_dir,
        reference_counts,
    )
    reference_count = sum(reference_counts.values())
    return ImageUsageSnapshot(
        items=items,
        computed_at=_now_utc_iso(),
        archive_count=archive_count,
        post_count=post_count,
        reference_count=reference_count,
        mapped_reference_count=mapped_reference_count,
        unmapped_reference_count=reference_count - mapped_reference_count,
        skipped_archives=skipped_archives,
    )


def image_usage_result(
    snapshot: ImageUsageSnapshot,
    *,
    offset: int,
    limit: int,
) -> ImageUsageResult:
    return {
        "items": [
            cast(ImageUsageItem, dict(item))
            for item in snapshot.items[offset : offset + limit]
        ],
        "total": len(snapshot.items),
        "offset": offset,
        "limit": limit,
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

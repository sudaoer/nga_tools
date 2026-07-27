from __future__ import annotations

import datetime
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, Optional, TypedDict, cast
from urllib.parse import quote

from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.backup import image_index
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME, ThreadArchiveStore
from nga_tools.backup.image_reference_cache import (
    scan_image_references_for_records_readonly,
)
from nga_tools.backup.image_pipeline import PostImageReference
from nga_tools.backup.models import PostRecord
from nga_tools.backup.post_overlay import apply_post_overlays_to_records
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.web.errors import WebConflict, WebNotFound

ImageUsageSort = Literal["usage", "threads"]


class ImageProblemKind(StrEnum):
    INVALID_URL = "invalid_url"
    UNMAPPED = "unmapped"
    MISSING_FILE = "missing_file"


ImageProblemFilter = Literal[
    "all",
    "invalid_url",
    "unmapped",
    "missing_file",
]
PostDate = int | str
_THREAD_DIR_RE = re.compile(r"^(\d+)_(all|\d+)$")
_IMAGE_PROBLEM_KINDS: tuple[ImageProblemKind, ...] = (
    ImageProblemKind.INVALID_URL,
    ImageProblemKind.UNMAPPED,
    ImageProblemKind.MISSING_FILE,
)
_IMAGE_BBCODE_OPEN_RE = re.compile(r"\[img\]", re.IGNORECASE)
_IMAGE_BBCODE_CLOSE_RE = re.compile(r"\[/img\]", re.IGNORECASE)
_IMAGE_SOURCE_TOKEN_END_RE = re.compile(r"[\s\[<]")


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


class ImageProblemIssueItem(TypedDict):
    kind: ImageProblemKind
    url: str
    occurrenceCount: int
    imageIndexes: list[int]
    sourceIndexes: list[int]
    relativePath: Optional[str]


class ImageProblemPostItem(TypedDict):
    tid: int
    aidKey: str
    dirName: str
    title: str
    pid: int
    lou: int
    floorLabel: str
    authorName: Optional[str]
    postdate: Optional[PostDate]
    issueCount: int
    issues: list[ImageProblemIssueItem]
    html: str
    editUrl: str


class ImageProblemKindCount(TypedDict):
    postCount: int
    occurrenceCount: int


class ImageProblemKindCounts(TypedDict):
    invalid_url: ImageProblemKindCount
    unmapped: ImageProblemKindCount
    missing_file: ImageProblemKindCount


class ImageProblemsResult(TypedDict):
    items: list[ImageProblemPostItem]
    total: int
    offset: int
    limit: int
    kind: ImageProblemFilter
    query: str
    computedAt: str
    archiveCount: int
    scannedPostCount: int
    problemPostCount: int
    problemThreadCount: int
    problemOccurrenceCount: int
    kindCounts: ImageProblemKindCounts
    skippedArchives: list[SkippedImageUsageArchive]


@dataclass(frozen=True, slots=True)
class ImageReplyReference:
    tid: int
    aid_key: str
    dir_name: str
    pid: int
    lou: int
    occurrence_count: int


@dataclass(frozen=True, slots=True)
class ImageProblemIssue:
    kind: ImageProblemKind
    url: str
    occurrence_count: int
    image_indexes: tuple[int, ...]
    source_indexes: tuple[int, ...]
    relative_path: Optional[str]


@dataclass(frozen=True, slots=True)
class _InvalidImageSource:
    source_index: int
    url: str


@dataclass(frozen=True, slots=True)
class ImageProblemPostReference:
    tid: int
    aid_key: str
    dir_name: str
    pid: int
    lou: int
    issues: tuple[ImageProblemIssue, ...]
    search_text: str


@dataclass(frozen=True)
class ImageUsageSnapshot:
    items_by_usage: list[ImageUsageItem]
    items_by_threads: list[ImageUsageItem]
    items_by_path: dict[str, ImageUsageItem]
    references_by_path: dict[str, tuple[ImageReplyReference, ...]]
    problem_references: tuple[ImageProblemPostReference, ...]
    problem_kind_counts: ImageProblemKindCounts
    thread_titles: dict[int, str]
    computed_at: str
    archive_count: int
    post_count: int
    reference_count: int
    mapped_reference_count: int
    unmapped_reference_count: int
    skipped_archives: list[SkippedImageUsageArchive]


class ImageIndexUnavailableError(WebConflict):
    pass


class ImageUsageNotFoundError(WebNotFound):
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


def _open_image_index_connection(output_dir: Path) -> sqlite3.Connection:
    store = image_index.ImageIndexStore(output_dir)
    db_path = store.db_path
    if not db_path.is_file():
        raise ImageIndexUnavailableError(f"缺少{image_index.IMAGE_INDEX_FILENAME}。")
    try:
        return store.open_readonly_connection()
    except FileNotFoundError as error:
        raise ImageIndexUnavailableError(
            f"缺少{image_index.IMAGE_INDEX_FILENAME}。"
        ) from error


def _image_paths_for_urls(
    connection: sqlite3.Connection,
    urls: set[str],
    output_dir: Path,
) -> dict[str, str]:
    mappings = image_index.ImageIndexStore(
        output_dir
    ).mappings_for_urls_in_connection(connection, urls)
    return {
        url: mapping.unique_rel_path for url, mapping in mappings.items()
    }


@dataclass(frozen=True)
class _ReferenceScanResult:
    usage_counts: Counter[str]
    references_by_path: dict[str, list[ImageReplyReference]]
    problem_references: list[ImageProblemPostReference]
    archive_count: int
    post_count: int
    reference_count: int
    mapped_reference_count: int
    skipped_archives: list[SkippedImageUsageArchive]


def _mapped_image_file_exists(
    images_root: Optional[Path],
    relative_path: str,
    availability_cache: dict[str, bool],
) -> bool:
    cached = availability_cache.get(relative_path)
    if cached is not None:
        return cached

    path = Path(relative_path)
    available = False
    if (
        images_root is not None
        and not path.is_absolute()
        and len(path.parts) >= 2
        and path.parts[0] == "images_unique"
        and ".." not in path.parts
    ):
        candidate = images_root.joinpath(*path.parts[1:])
        if len(path.parts) == 2 and not candidate.is_symlink():
            available = candidate.is_file()
        else:
            resolved_candidate = candidate.resolve()
            available = (
                resolved_candidate.is_relative_to(images_root)
                and resolved_candidate.is_file()
            )
    availability_cache[relative_path] = available
    return available


def _image_source_token(value: str) -> str:
    stripped = value.strip()
    token_end = _IMAGE_SOURCE_TOKEN_END_RE.search(stripped)
    if token_end is None:
        return stripped
    return stripped[: token_end.start()]


def _invalid_image_sources(content: str) -> tuple[_InvalidImageSource, ...]:
    """Return invalid or structurally broken ``[img]`` sources in order."""

    invalid_sources: list[_InvalidImageSource] = []
    cursor = 0
    source_index = 0
    while opening := _IMAGE_BBCODE_OPEN_RE.search(content, cursor):
        source_index += 1
        value_start = opening.end()
        closing = _IMAGE_BBCODE_CLOSE_RE.search(content, value_start)
        next_opening = _IMAGE_BBCODE_OPEN_RE.search(content, value_start)
        well_formed = closing is not None and (
            next_opening is None or closing.start() < next_opening.start()
        )

        if well_formed:
            assert closing is not None
            raw_source = content[value_start : closing.start()].strip()
            cursor = closing.end()
        else:
            raw_tail = content[value_start:]
            token_end = _IMAGE_SOURCE_TOKEN_END_RE.search(raw_tail.lstrip())
            stripped_tail = raw_tail.lstrip()
            raw_source = (
                stripped_tail
                if token_end is None
                else stripped_tail[: token_end.start()]
            )
            cursor = value_start

        normalized_source = image_index.normalize_nga_image_url(raw_source)
        if not well_formed or not NGA_img_link_verify(normalized_source):
            invalid_sources.append(
                _InvalidImageSource(
                    source_index=source_index,
                    url=_image_source_token(raw_source),
                )
            )

    return tuple(invalid_sources)


def _problem_issues_for_references(
    images_root: Optional[Path],
    references: tuple[PostImageReference, ...],
    invalid_sources: tuple[_InvalidImageSource, ...],
    image_paths: dict[str, str],
    availability_cache: dict[str, bool],
) -> tuple[ImageProblemIssue, ...]:
    image_indexes_by_issue: dict[
        tuple[ImageProblemKind, str, Optional[str]],
        list[int],
    ] = defaultdict(list)
    source_indexes_by_issue: dict[
        tuple[ImageProblemKind, str, Optional[str]],
        list[int],
    ] = defaultdict(list)
    for reference in references:
        if not reference.valid:
            image_indexes_by_issue[
                (ImageProblemKind.INVALID_URL, reference.url, None)
            ].append(reference.image_index)
            continue

        relative_path = image_paths.get(reference.url)
        if relative_path is None:
            image_indexes_by_issue[
                (ImageProblemKind.UNMAPPED, reference.url, None)
            ].append(reference.image_index)
            continue
        if not _mapped_image_file_exists(
            images_root,
            relative_path,
            availability_cache,
        ):
            image_indexes_by_issue[
                (
                    ImageProblemKind.MISSING_FILE,
                    reference.url,
                    relative_path,
                )
            ].append(reference.image_index)

    for invalid_source in invalid_sources:
        source_indexes_by_issue[
            (ImageProblemKind.INVALID_URL, invalid_source.url, None)
        ].append(invalid_source.source_index)

    kind_order = {kind: index for index, kind in enumerate(_IMAGE_PROBLEM_KINDS)}
    issue_keys = set(image_indexes_by_issue) | set(source_indexes_by_issue)
    return tuple(
        ImageProblemIssue(
            kind=kind,
            url=url,
            occurrence_count=(
                len(image_indexes_by_issue[(kind, url, relative_path)])
                + len(source_indexes_by_issue[(kind, url, relative_path)])
            ),
            image_indexes=tuple(
                sorted(image_indexes_by_issue[(kind, url, relative_path)])
            ),
            source_indexes=tuple(
                sorted(source_indexes_by_issue[(kind, url, relative_path)])
            ),
            relative_path=relative_path,
        )
        for kind, url, relative_path in sorted(
            issue_keys,
            key=lambda key: (
                kind_order[key[0]],
                key[1],
                key[2] or "",
            ),
        )
    )


def _image_problem_search_text(
    *,
    tid: int,
    aid_key: str,
    dir_name: str,
    title: str,
    pid: int,
    lou: int,
    author_name: Optional[str],
    content: str,
    issues: tuple[ImageProblemIssue, ...],
) -> str:
    values = [
        title,
        author_name or "",
        content,
        dir_name,
        str(tid),
        f"tid {tid}",
        f"tid={tid}",
        aid_key,
        f"aid {aid_key}",
        f"aid={aid_key}",
        str(pid),
        f"pid {pid}",
        f"pid={pid}",
        str(lou),
        f"lou {lou}",
        f"lou={lou}",
        f"{lou}楼",
        f"第{lou}楼",
    ]
    if lou == 0:
        values.append("主楼")
    for issue in issues:
        values.append(issue.url)
        if issue.relative_path is not None:
            values.append(issue.relative_path)
    return "\n".join(values).casefold()


def _scan_references(
    connection: sqlite3.Connection,
    thread_groups: dict[int, list[Path]],
    thread_titles: dict[int, str],
    output_dir: Path,
) -> _ReferenceScanResult:
    usage_counts: Counter[str] = Counter()
    references_by_path: dict[str, list[ImageReplyReference]] = defaultdict(list)
    problem_references: list[ImageProblemPostReference] = []
    availability_cache: dict[str, bool] = {}
    output_root = output_dir.resolve()
    resolved_images_root = (output_root / "images_unique").resolve()
    images_root = (
        resolved_images_root
        if resolved_images_root.is_relative_to(output_root)
        else None
    )
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
                rows = archive_store.posts.read_effective_post_rows()
                records: list[PostRecord] = [
                    {
                        "lou": row.lou,
                        "pid": row.pid,
                        "post": {
                            "lou": row.lou,
                            "pid": row.pid,
                            "content": row.content,
                        },
                        "html": None,
                        "source_hash": row.source_hash,
                    }
                    for row in rows
                ]
                overlays = archive_store.overlays.read_post_overlays()
                records = apply_post_overlays_to_records(
                    overlays,
                    records,
                    output_dir=thread_folder.parent,
                )
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
                image_paths = _image_paths_for_urls(
                    connection,
                    valid_urls,
                    output_dir,
                )
                rows_by_lou = {row.lou: row for row in rows}
                for scan in scan_result.scans:
                    row = rows_by_lou.get(scan.lou)
                    if row is None:
                        continue
                    pid = row.pid
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
                    overlay = overlays.get(scan.lou)
                    searchable_content = (
                        overlay["bbcode"] if overlay is not None else row.content
                    )
                    problem_issues = _problem_issues_for_references(
                        images_root,
                        scan.references,
                        _invalid_image_sources(searchable_content),
                        image_paths,
                        availability_cache,
                    )
                    if problem_issues:
                        problem_references.append(
                            ImageProblemPostReference(
                                tid=tid,
                                aid_key=aid_key,
                                dir_name=thread_folder.name,
                                pid=pid,
                                lou=scan.lou,
                                issues=problem_issues,
                                search_text=_image_problem_search_text(
                                    tid=tid,
                                    aid_key=aid_key,
                                    dir_name=thread_folder.name,
                                    title=thread_titles.get(tid, f"tid {tid}"),
                                    pid=pid,
                                    lou=scan.lou,
                                    author_name=row.author_name,
                                    content=searchable_content,
                                    issues=problem_issues,
                                ),
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
        problem_references=problem_references,
        archive_count=archive_count,
        post_count=post_count,
        reference_count=reference_count,
        mapped_reference_count=mapped_reference_count,
        skipped_archives=skipped_archives,
    )


def _inventory_items(
    connection: sqlite3.Connection,
    scan_result: _ReferenceScanResult,
    output_dir: Path,
) -> list[ImageUsageItem]:
    inventory: dict[str, tuple[str, int]] = {}
    for url, relative_path in image_index.ImageIndexStore(
        output_dir
    ).iter_mapping_rows(connection):
        previous = inventory.get(relative_path)
        if previous is None:
            inventory[relative_path] = (url, 1)
        else:
            inventory[relative_path] = (
                min(previous[0], url),
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


def image_problem_kind_counts(
    references: list[ImageProblemPostReference]
    | tuple[ImageProblemPostReference, ...],
) -> ImageProblemKindCounts:
    post_counts: Counter[ImageProblemKind] = Counter()
    occurrence_counts: Counter[ImageProblemKind] = Counter()
    for reference in references:
        kinds_in_post: set[ImageProblemKind] = set()
        for issue in reference.issues:
            kinds_in_post.add(issue.kind)
            occurrence_counts[issue.kind] += issue.occurrence_count
        post_counts.update(kinds_in_post)
    return {
        "invalid_url": {
            "postCount": post_counts[ImageProblemKind.INVALID_URL],
            "occurrenceCount": occurrence_counts[
                ImageProblemKind.INVALID_URL
            ],
        },
        "unmapped": {
            "postCount": post_counts[ImageProblemKind.UNMAPPED],
            "occurrenceCount": occurrence_counts[ImageProblemKind.UNMAPPED],
        },
        "missing_file": {
            "postCount": post_counts[ImageProblemKind.MISSING_FILE],
            "occurrenceCount": occurrence_counts[
                ImageProblemKind.MISSING_FILE
            ],
        },
    }


def build_image_usage_snapshot(output_dir: Path) -> ImageUsageSnapshot:
    thread_groups = _thread_folder_groups(output_dir)
    metadata = _thread_metadata()
    thread_titles = {
        tid: _thread_title(tid, paths, metadata)
        for tid, paths in thread_groups.items()
    }
    try:
        with closing(_open_image_index_connection(output_dir)) as connection:
            scan_result = _scan_references(
                connection,
                thread_groups,
                thread_titles,
                output_dir,
            )
            items = _inventory_items(connection, scan_result, output_dir)
    except (sqlite3.Error, OSError, ValueError) as error:
        raise ImageIndexUnavailableError(
            f"无法读取{image_index.IMAGE_INDEX_FILENAME}：{error}"
        ) from error

    items_by_usage = sorted(
        items,
        key=lambda item: (-item["usageCount"], item["relativePath"]),
    )
    items_by_threads = sorted(
        items,
        key=lambda item: (-item["threadCount"], item["relativePath"]),
    )
    problem_references = sorted(
        scan_result.problem_references,
        key=lambda reference: (
            reference.tid,
            reference.lou,
            reference.pid,
            reference.aid_key,
        ),
    )
    return ImageUsageSnapshot(
        items_by_usage=items_by_usage,
        items_by_threads=items_by_threads,
        items_by_path={item["relativePath"]: item for item in items},
        references_by_path={
            relative_path: tuple(references)
            for relative_path, references in scan_result.references_by_path.items()
        },
        problem_references=tuple(problem_references),
        problem_kind_counts=image_problem_kind_counts(problem_references),
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


def image_problem_references(
    snapshot: ImageUsageSnapshot,
    kind: ImageProblemFilter,
    query: str = "",
) -> tuple[ImageProblemPostReference, ...]:
    normalized_query = query.strip().casefold()
    references = (
        snapshot.problem_references
        if not normalized_query
        else tuple(
            reference
            for reference in snapshot.problem_references
            if normalized_query in reference.search_text
        )
    )
    if kind == "all":
        return references
    return tuple(
        reference
        for reference in references
        if any(issue.kind == kind for issue in reference.issues)
    )


def copy_image_problem_kind_counts(
    counts: ImageProblemKindCounts,
) -> ImageProblemKindCounts:
    def copy_count(kind: ImageProblemKind) -> ImageProblemKindCount:
        if kind is ImageProblemKind.INVALID_URL:
            count = counts["invalid_url"]
        elif kind is ImageProblemKind.UNMAPPED:
            count = counts["unmapped"]
        else:
            count = counts["missing_file"]
        return {
            "postCount": count["postCount"],
            "occurrenceCount": count["occurrenceCount"],
        }

    return {
        "invalid_url": copy_count(ImageProblemKind.INVALID_URL),
        "unmapped": copy_count(ImageProblemKind.UNMAPPED),
        "missing_file": copy_count(ImageProblemKind.MISSING_FILE),
    }

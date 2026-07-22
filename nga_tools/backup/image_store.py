from __future__ import annotations

import filecmp
import os
import tempfile
import threading
from collections import deque
from concurrent.futures import Future
from contextlib import contextmanager
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Iterable, NotRequired, TypedDict
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

import nga_tools.config as config
from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.atomic import (
    replace_file_atomically,
    replace_temp_file,
    temporary_sibling_path,
)
from nga_tools.core import downloads
from nga_tools.core.download_types import (
    DownloadFileResult,
    DownloadProgressCallback,
    DownloadSummary,
    DownloadTask,
)
from nga_tools.core.hashing import sha256
from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.core.image_formats import (
    image_file_is_valid,
    inspect_image_file,
)
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    ImageValidationOutcome,
    canonical_image_path_key,
    current_image_validation_cache,
    invalidate_current_image_validation,
)
from nga_tools.timing import time_section
from nga_tools.backup.image_index import (
    ImageIndexStore,
    ImageMapping,
    normalize_nga_image_url,
)
from nga_tools.backup.image_store_metrics import (
    record_image_hash_source,
    record_image_single_flight_wait,
    record_image_store_attempt,
    record_image_store_completed,
    record_image_store_failed,
    time_image_store_phase,
)
from nga_tools.backup.image_store_runtime import (
    image_store_pending_limit,
    submit_image_store_work,
)


class ImageDownloadTask(TypedDict):
    url: str


class StoredImageResult(TypedDict):
    url: str
    unique_path: str
    reused: bool
    collision: NotRequired[bool]


class CompactImageDownloadSummary(TypedDict):
    succeeded_count: int
    failed: list[DownloadFileResult]


@dataclass(frozen=True)
class NgaImageUrl:
    url: str
    month_dir: str
    day_dir: str
    filename: str


@dataclass(frozen=True)
class ImageLookupCache:
    mappings_by_url: dict[str, ImageMapping]
    validation_cache: ImageValidationCache = field(
        default_factory=ImageValidationCache
    )

    @classmethod
    def for_urls(cls, urls: Iterable[str]) -> ImageLookupCache:
        validation_cache = current_image_validation_cache()
        return cls(
            _image_index_store().mappings_for_urls(urls),
            validation_cache
            if validation_cache is not None
            else ImageValidationCache(),
        )

    @classmethod
    def for_tasks(cls, tasks: Iterable[ImageDownloadTask]) -> ImageLookupCache:
        return cls.for_urls(task["url"] for task in tasks)

    def mapped_image_path_for_url(self, url: str) -> Path | None:
        normalized_url = normalize_nga_image_url(url)
        if not NGA_img_link_verify(normalized_url):
            return None

        mapping = self.mappings_by_url.get(normalized_url)
        if mapping is None:
            return None
        image_path = mapping.unique_path
        if not self.validation_cache.validate(image_path).valid:
            return None
        return image_path

    def unique_image_src_from_html_dir(
        self,
        url: str,
        html_dir: str | Path,
    ) -> str | None:
        image_path = self.mapped_image_path_for_url(url)
        if image_path is None:
            return None
        return os.path.relpath(image_path, html_dir).replace("\\", "/")

    def image_task_is_complete(self, task: ImageDownloadTask) -> bool:
        return self.mapped_image_path_for_url(task["url"]) is not None


@dataclass(frozen=True)
class ImagePreparationStats:
    task_url_count: int
    mapping_hit_url_count: int
    unique_physical_path_count: int
    intra_thread_path_dedup_count: int
    memory_cache_hit_path_count: int
    deep_validation_path_count: int
    persistent_cache_hit_path_count: int
    missing_validation_path_count: int
    persistent_cache_query_path_count: int
    invalid_mapping_count: int
    pending_download_url_count: int


@dataclass(frozen=True)
class ImageDownloadPreparation:
    pending_tasks: list[ImageDownloadTask]
    stats: ImagePreparationStats


PLACEHOLDER_IMAGE_FILENAME = "download_failed_placeholder.png"
_IMAGE_STORE_LOCK = threading.RLock()
_IMAGE_HASH_LOCKS = tuple(threading.Lock() for _index in range(256))
_IMAGE_PREPARATION_SEMAPHORE = threading.BoundedSemaphore(1)


@dataclass(slots=True)
class _ImageURLClaim:
    result: DownloadFileResult | None = None
    error: BaseException | None = None
    completed: bool = False


type _ImageClaimKey = tuple[str, str]


@dataclass(frozen=True)
class _PendingImageMapping:
    result: DownloadFileResult
    mapping: tuple[str, Path]
    claim_key: _ImageClaimKey
    claim: _ImageURLClaim


@dataclass(frozen=True)
class _PendingImageStore:
    download_result: DownloadFileResult
    image_task: ImageDownloadTask
    claim_key: _ImageClaimKey
    claim: _ImageURLClaim
    future: Future[StoredImageResult]


@dataclass(frozen=True)
class _PendingImageMappingBatch:
    items: tuple[_PendingImageMapping, ...]
    future: Future[None]


_IMAGE_URL_CLAIMS_LOCK = threading.RLock()
_IMAGE_URL_CLAIMS_CONDITION = threading.Condition(_IMAGE_URL_CLAIMS_LOCK)
_IMAGE_URL_CLAIMS: dict[_ImageClaimKey, _ImageURLClaim] = {}
_COMPLETED_IMAGE_URL_CLAIMS: dict[_ImageClaimKey, str] = {}
_image_coordination_scope_depth = 0
_MAX_PENDING_IMAGE_MAPPING_RESULTS = 64
_MAX_PENDING_IMAGE_MAPPING_BATCHES = 16


@contextmanager
def use_image_download_coordination() -> Generator[None]:
    global _image_coordination_scope_depth
    with _IMAGE_URL_CLAIMS_LOCK:
        _image_coordination_scope_depth += 1
    try:
        yield
    finally:
        with _IMAGE_URL_CLAIMS_LOCK:
            _image_coordination_scope_depth -= 1
            if _image_coordination_scope_depth == 0:
                _COMPLETED_IMAGE_URL_CLAIMS.clear()


def parse_nga_image_url(url: str) -> NgaImageUrl:
    if not NGA_img_link_verify(url):
        raise ValueError(f"NGA图片链接无效：{url}")

    parts = urlsplit(url)
    path_parts = parts.path.split("/")
    return NgaImageUrl(
        url=url,
        month_dir=path_parts[2],
        day_dir=path_parts[3],
        filename=path_parts[4],
    )


def output_dir() -> Path:
    return Path(config.get_config().output_dir)


def _image_index_store() -> ImageIndexStore:
    return ImageIndexStore(output_dir())


def _enqueue_image_mappings(
    mappings: list[tuple[str, Path]],
) -> tuple[list[ImageMapping], Future[None]]:
    return _image_index_store().enqueue_mappings(mappings)


def _wait_image_mapping(future: Future[None]) -> None:
    _image_index_store().wait_for_mapping(future)


def unique_images_dir() -> Path:
    return output_dir() / "images_unique"


def placeholder_image_path() -> Path:
    placeholder_path = unique_images_dir() / PLACEHOLDER_IMAGE_FILENAME
    with _IMAGE_STORE_LOCK:
        if _image_file_is_valid(placeholder_path):
            return placeholder_path

        placeholder_path.parent.mkdir(parents=True, exist_ok=True)
        image = Image.new("RGB", (320, 180), (242, 244, 247))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 319, 179), outline=(148, 163, 184), width=4)
        draw.line((42, 138, 278, 42), fill=(100, 116, 139), width=6)
        draw.line((42, 42, 278, 138), fill=(100, 116, 139), width=6)
        draw.text((86, 146), "image unavailable", fill=(71, 85, 105))
        temp_path = temporary_sibling_path(placeholder_path)
        try:
            image.save(temp_path, format="PNG")
            replace_temp_file(temp_path, placeholder_path)
            invalidate_current_image_validation(placeholder_path)
        except BaseException:
            temp_path.unlink(missing_ok=True)
            raise
    return placeholder_path


def placeholder_image_src_from_html_dir(html_dir: str | Path) -> str:
    return os.path.relpath(placeholder_image_path(), html_dir).replace("\\", "/")


def mapped_image_path_for_url(url: str) -> Path | None:
    return ImageLookupCache.for_urls([url]).mapped_image_path_for_url(url)


def image_task_is_complete(task: ImageDownloadTask) -> bool:
    return ImageLookupCache.for_tasks([task]).image_task_is_complete(task)


def pending_image_download_tasks(
    image_tasks: list[ImageDownloadTask],
) -> list[ImageDownloadTask]:
    return prepare_image_download_tasks(image_tasks).pending_tasks


def prepare_image_download_tasks(
    image_tasks: list[ImageDownloadTask],
) -> ImageDownloadPreparation:
    with time_section("图片下载准备排队"):
        _IMAGE_PREPARATION_SEMAPHORE.acquire()
    try:
        with time_section("图片下载准备执行"):
            return _prepare_image_download_tasks_uncoordinated(image_tasks)
    finally:
        _IMAGE_PREPARATION_SEMAPHORE.release()


def _prepare_image_download_tasks_uncoordinated(
    image_tasks: list[ImageDownloadTask],
) -> ImageDownloadPreparation:
    with time_section("图片索引批量查询"):
        mappings_by_url = _image_index_store().mappings_for_urls(
            task["url"] for task in image_tasks
        )

    validation_cache = current_image_validation_cache()
    if validation_cache is None:
        validation_cache = ImageValidationCache()

    mapped_path_key_by_task_index: dict[int, str] = {}
    mapped_paths_by_key: dict[str, Path] = {}
    path_key_by_unique_rel_path: dict[str, str] = {}
    for index, task in enumerate(image_tasks):
        normalized_url = normalize_nga_image_url(task["url"])
        mapping = mappings_by_url.get(normalized_url)
        if mapping is None:
            continue
        image_path = mapping.unique_path
        path_key = path_key_by_unique_rel_path.get(mapping.unique_rel_path)
        if path_key is None:
            path_key = canonical_image_path_key(image_path)
            path_key_by_unique_rel_path[mapping.unique_rel_path] = path_key
            mapped_paths_by_key.setdefault(path_key, image_path)
        mapped_path_key_by_task_index[index] = path_key

    validation_by_path_key: dict[str, ImageValidationOutcome] = {}
    with time_section("图片缓存文件校验"):
        persistent_cache_query_path_count = validation_cache.preload(
            set(mapped_paths_by_key.values())
        )
        for path_key, image_path in mapped_paths_by_key.items():
            validation_by_path_key[path_key] = validation_cache.validate(image_path)
        validation_cache.flush_new_entries()

    pending_tasks: list[ImageDownloadTask] = []
    invalid_mapping_count = 0
    for index, task in enumerate(image_tasks):
        mapped_path_key = mapped_path_key_by_task_index.get(index)
        if mapped_path_key is None:
            pending_tasks.append(task)
            continue
        validation = validation_by_path_key[mapped_path_key]
        if not validation.valid:
            invalid_mapping_count += 1
            pending_tasks.append(task)

    mapping_hit_url_count = len(mapped_path_key_by_task_index)
    unique_physical_path_count = len(mapped_paths_by_key)
    stats = ImagePreparationStats(
        task_url_count=len(image_tasks),
        mapping_hit_url_count=mapping_hit_url_count,
        unique_physical_path_count=unique_physical_path_count,
        intra_thread_path_dedup_count=(
            mapping_hit_url_count - unique_physical_path_count
        ),
        memory_cache_hit_path_count=sum(
            outcome.source == "memory" for outcome in validation_by_path_key.values()
        ),
        deep_validation_path_count=sum(
            outcome.source == "deep" for outcome in validation_by_path_key.values()
        ),
        persistent_cache_hit_path_count=sum(
            outcome.source == "persistent"
            for outcome in validation_by_path_key.values()
        ),
        missing_validation_path_count=sum(
            outcome.source == "missing" for outcome in validation_by_path_key.values()
        ),
        persistent_cache_query_path_count=persistent_cache_query_path_count,
        invalid_mapping_count=invalid_mapping_count,
        pending_download_url_count=len(pending_tasks),
    )
    return ImageDownloadPreparation(pending_tasks=pending_tasks, stats=stats)


def link_path_for_image_src(image_src: str) -> Path | None:
    normalized_url = normalize_nga_image_url(image_src)
    if not NGA_img_link_verify(normalized_url):
        return None
    return mapped_image_path_for_url(normalized_url)


def _image_extension_from_url(url: str) -> str:
    filename = parse_nga_image_url(url).filename.lower()
    for extension in (
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "avif",
        "heic",
        "heif",
        "jxl",
    ):
        marker = f".{extension}"
        if marker in filename:
            return "jpg" if extension == "jpeg" else extension
    return "bin"


def _same_file_content(first: Path, second: Path) -> bool:
    with time_image_store_phase("content_compare"):
        if not first.exists() or not second.exists():
            return False
        return filecmp.cmp(first, second, shallow=False)


def _image_file_is_valid(path: Path) -> bool:
    validation_cache = current_image_validation_cache()
    if validation_cache is None:
        return image_file_is_valid(path)
    return validation_cache.validate(path).valid


def _target_path_for_download(
    temp_path: Path,
    image_hash: str,
    extension: str,
) -> tuple[Path, bool, bool]:
    unique_dir = unique_images_dir()
    unique_dir.mkdir(parents=True, exist_ok=True)

    target_path = unique_dir / f"{image_hash}.{extension}"
    if not target_path.exists():
        return target_path, False, False
    if not _image_file_is_valid(target_path):
        return target_path, False, False
    if _same_file_content(target_path, temp_path):
        return target_path, True, False

    collision_index = 1
    while True:
        collision_path = unique_dir / f"{image_hash}-collision-{collision_index}.{extension}"
        if not collision_path.exists():
            report_warning(
                WarningCategory.IMAGE_PROCESSING,
                f"图片SHA-256 hash碰撞，保存为：{collision_path}",
            )
            return collision_path, False, True
        if not _image_file_is_valid(collision_path):
            report_warning(
                WarningCategory.IMAGE_PROCESSING,
                f"图片SHA-256 hash碰撞，保存为：{collision_path}",
            )
            return collision_path, False, True
        if _same_file_content(collision_path, temp_path):
            return collision_path, True, True
        collision_index += 1


def _store_image_file(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
) -> StoredImageResult:
    stored_image, mapping_future = _store_image_file_deferred_mapping(
        source_path,
        task,
        move_source=move_source,
    )
    _wait_image_mapping(mapping_future)
    return stored_image


def _hash_lock(image_hash: str) -> threading.Lock:
    try:
        stripe = int(image_hash[:2], 16)
    except ValueError:
        stripe = hash(image_hash) % len(_IMAGE_HASH_LOCKS)
    return _IMAGE_HASH_LOCKS[stripe]


def _precomputed_content_hash(
    source_path: Path,
    download_result: DownloadFileResult | None,
) -> tuple[str | None, bool]:
    if download_result is None:
        return None, False
    has_identity = (
        "content_sha256" in download_result
        or "content_bytes" in download_result
    )
    content_sha256 = download_result.get("content_sha256")
    content_bytes = download_result.get("content_bytes")
    if (
        not isinstance(content_sha256, str)
        or not isinstance(content_bytes, int)
        or isinstance(content_bytes, bool)
        or content_bytes < 0
    ):
        return None, has_identity
    normalized_hash = content_sha256.lower()
    if len(normalized_hash) != 64:
        return None, True
    try:
        int(normalized_hash, 16)
    except ValueError:
        return None, True
    if source_path.stat().st_size != content_bytes:
        return None, True
    return normalized_hash, False


def _content_hash_for_store(
    source_path: Path,
    download_result: DownloadFileResult | None,
) -> str:
    precomputed_hash, rejected = _precomputed_content_hash(
        source_path,
        download_result,
    )
    if precomputed_hash is not None:
        record_image_hash_source(precomputed=True)
        return precomputed_hash
    record_image_hash_source(precomputed=False, rejected=rejected)
    with time_image_store_phase("fallback_hash"):
        return sha256(str(source_path))


def _store_image_file_deferred_mapping(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
    download_result: DownloadFileResult | None = None,
) -> tuple[StoredImageResult, Future[None]]:
    result = _store_image_file_without_mapping(
        source_path,
        task,
        move_source=move_source,
        download_result=download_result,
    )
    _mappings, mapping_future = _enqueue_image_mappings(
        [(task["url"], Path(result["unique_path"]))]
    )
    return result, mapping_future


def _store_image_file_without_mapping(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
    download_result: DownloadFileResult | None = None,
) -> StoredImageResult:
    record_image_store_attempt()
    try:
        with time_image_store_phase("source_inspection"):
            source_inspection = inspect_image_file(source_path)
        if not source_inspection.valid:
            raise ValueError(f"图片文件无效：{source_path}")
        image_hash = _content_hash_for_store(source_path, download_result)
        extension = source_inspection.extension or _image_extension_from_url(
            task["url"]
        )
        with _hash_lock(image_hash):
            with time_image_store_phase("target_selection"):
                target_path, reused, collision = _target_path_for_download(
                    source_path,
                    image_hash,
                    extension,
                )
            if not reused:
                if target_path.exists():
                    invalidate_current_image_validation(target_path)
                with time_image_store_phase("file_placement"):
                    if move_source:
                        replace_file_atomically(
                            source_path,
                            target_path,
                            move_source=True,
                        )
                    elif source_path.resolve() != target_path.resolve():
                        replace_file_atomically(
                            source_path,
                            target_path,
                            move_source=False,
                        )
                with time_image_store_phase("final_validation"):
                    target_is_valid = _image_file_is_valid(target_path)
                if not target_is_valid:
                    raise ValueError(f"图片保存后无法校验：{target_path}")
        result: StoredImageResult = {
            "url": task["url"],
            "unique_path": str(target_path),
            "reused": reused,
        }
        if collision:
            result["collision"] = True
    except BaseException:
        record_image_store_failed()
        raise
    record_image_store_completed(reused=reused, collision=collision)
    return result


def store_downloaded_image(temp_path: Path, task: ImageDownloadTask) -> StoredImageResult:
    return _store_image_file(temp_path, task, move_source=True)


def store_existing_image(image_path: Path, url: str) -> StoredImageResult:
    return _store_image_file(image_path, {"url": url}, move_source=False)


def _claim_image_url(
    url: str,
) -> tuple[_ImageClaimKey, _ImageURLClaim, bool]:
    claim_key = (
        str(output_dir().resolve()),
        normalize_nga_image_url(url),
    )
    with _IMAGE_URL_CLAIMS_LOCK:
        existing = _IMAGE_URL_CLAIMS.get(claim_key)
        if existing is not None:
            return claim_key, existing, False
        completed_path_text = _COMPLETED_IMAGE_URL_CLAIMS.get(claim_key)
        if completed_path_text is not None:
            completed_path = Path(completed_path_text)
            if image_file_is_valid(completed_path):
                completed_claim = _ImageURLClaim(
                    result={
                        "url": url,
                        "save_path": completed_path_text,
                        "success": True,
                    },
                    completed=True,
                )
                return claim_key, completed_claim, False
            _COMPLETED_IMAGE_URL_CLAIMS.pop(claim_key, None)
        claim = _ImageURLClaim()
        _IMAGE_URL_CLAIMS[claim_key] = claim
        return claim_key, claim, True


def _release_image_url_claim(
    claim_key: _ImageClaimKey,
    claim: _ImageURLClaim,
    *,
    result: DownloadFileResult | None = None,
    error: BaseException | None = None,
) -> None:
    with _IMAGE_URL_CLAIMS_CONDITION:
        claim.result = result
        claim.error = error
        claim.completed = True
        if result is not None and result["success"]:
            _COMPLETED_IMAGE_URL_CLAIMS[claim_key] = result["save_path"]
        else:
            _COMPLETED_IMAGE_URL_CLAIMS.pop(claim_key, None)
        if _IMAGE_URL_CLAIMS.get(claim_key) is claim:
            _IMAGE_URL_CLAIMS.pop(claim_key, None)
        _IMAGE_URL_CLAIMS_CONDITION.notify_all()


def _wait_image_url_claim(claim: _ImageURLClaim) -> None:
    wait_started_at: float | None = None
    with _IMAGE_URL_CLAIMS_CONDITION:
        while not claim.completed:
            if wait_started_at is None:
                wait_started_at = perf_counter()
            _IMAGE_URL_CLAIMS_CONDITION.wait()
    if wait_started_at is not None:
        record_image_single_flight_wait(perf_counter() - wait_started_at)


def _copy_claim_result(
    url: str,
    result: DownloadFileResult,
) -> DownloadFileResult:
    copied = result.copy()
    copied["url"] = url
    return copied


def _run_download_image_tasks(
    image_tasks: list[ImageDownloadTask],
    on_progress: DownloadProgressCallback | None,
    *,
    collect_successes: bool,
) -> tuple[
    int,
    list[DownloadFileResult],
    list[DownloadFileResult],
]:
    if not image_tasks:
        return 0, [], []

    succeeded_count = 0
    succeeded: list[DownloadFileResult] = []
    failed: list[DownloadFileResult] = []
    owners: list[tuple[ImageDownloadTask, _ImageClaimKey, _ImageURLClaim]] = []
    waiters: list[tuple[ImageDownloadTask, _ImageURLClaim]] = []
    for image_task in image_tasks:
        claim_key, claim, is_owner = _claim_image_url(image_task["url"])
        if is_owner:
            owners.append((image_task, claim_key, claim))
        else:
            waiters.append((image_task, claim))

    completed = 0

    def emit_result(result: DownloadFileResult) -> None:
        nonlocal completed, succeeded_count
        completed += 1
        if result["success"]:
            succeeded_count += 1
            if collect_successes:
                succeeded.append(result)
        else:
            failed.append(result)
        if on_progress is not None:
            on_progress(completed, len(image_tasks), result)

    pending_store_results: deque[_PendingImageStore] = deque()
    pending_mapping_results: list[_PendingImageMapping] = []
    pending_mapping_batches: deque[_PendingImageMappingBatch] = deque()

    def release_owner(
        claim_key: _ImageClaimKey,
        claim: _ImageURLClaim,
        result: DownloadFileResult,
    ) -> None:
        _release_image_url_claim(claim_key, claim, result=result)
        emit_result(result)

    def mapping_failure_result(
        pending: _PendingImageMapping,
        error: BaseException,
    ) -> DownloadFileResult:
        return {
            "url": pending.result["url"],
            "save_path": str(unique_images_dir()),
            "success": False,
            "error": str(error),
            "failure_kind": "image_store",
        }

    def store_failure_result(
        pending: _PendingImageStore,
        error: BaseException,
    ) -> DownloadFileResult:
        return {
            "url": pending.image_task["url"],
            "save_path": str(unique_images_dir()),
            "success": False,
            "error": str(error),
            "failure_kind": "image_store",
        }

    def release_remaining_without_progress(
        pending_batch: tuple[_PendingImageMapping, ...],
        *,
        results: tuple[DownloadFileResult, ...] | None = None,
        error: BaseException | None = None,
    ) -> None:
        for index, pending in enumerate(pending_batch):
            _release_image_url_claim(
                pending.claim_key,
                pending.claim,
                result=None if results is None else results[index],
                error=error,
            )

    def finish_mapping_batch(
        pending_batch: _PendingImageMappingBatch,
        *,
        emit_progress: bool,
        suppress_mapping_error: bool = False,
    ) -> None:
        try:
            _wait_image_mapping(pending_batch.future)
        except BaseException as mapping_error:
            if emit_progress and isinstance(mapping_error, Exception):
                failed_results = tuple(
                    mapping_failure_result(pending, mapping_error)
                    for pending in pending_batch.items
                )
                for index, (pending, failed_result) in enumerate(
                    zip(pending_batch.items, failed_results, strict=True)
                ):
                    _release_image_url_claim(
                        pending.claim_key,
                        pending.claim,
                        result=failed_result,
                    )
                    try:
                        emit_result(failed_result)
                    except BaseException:
                        release_remaining_without_progress(
                            pending_batch.items[index + 1 :],
                            results=failed_results[index + 1 :],
                        )
                        raise
                return
            release_remaining_without_progress(
                pending_batch.items,
                error=mapping_error,
            )
            if not suppress_mapping_error:
                raise
            return

        if not emit_progress:
            release_remaining_without_progress(
                pending_batch.items,
                results=tuple(
                    pending.result for pending in pending_batch.items
                ),
            )
            return
        for index, pending in enumerate(pending_batch.items):
            _release_image_url_claim(
                pending.claim_key,
                pending.claim,
                result=pending.result,
            )
            try:
                emit_result(pending.result)
            except BaseException:
                release_remaining_without_progress(
                    pending_batch.items[index + 1 :],
                    results=tuple(
                        remaining.result
                        for remaining in pending_batch.items[index + 1 :]
                    ),
                )
                raise

    def drain_mapping_batches(
        *,
        block: bool,
        emit_progress: bool,
        suppress_mapping_error: bool = False,
        maximum: int | None = None,
    ) -> None:
        drained = 0
        while pending_mapping_batches and (
            maximum is None or drained < maximum
        ):
            pending_batch = pending_mapping_batches[0]
            if not block and not pending_batch.future.done():
                return
            pending_mapping_batches.popleft()
            finish_mapping_batch(
                pending_batch,
                emit_progress=emit_progress,
                suppress_mapping_error=suppress_mapping_error,
            )
            drained += 1

    def submit_pending_mapping_results(*, emit_progress: bool) -> None:
        if not pending_mapping_results:
            return
        pending_batch = tuple(pending_mapping_results)
        pending_mapping_results.clear()
        try:
            _mappings, mapping_future = _enqueue_image_mappings(
                [pending.mapping for pending in pending_batch]
            )
        except BaseException as error:
            mapping_future = Future[None]()
            mapping_future.set_exception(error)
        pending_mapping_batches.append(
            _PendingImageMappingBatch(pending_batch, mapping_future)
        )
        if len(pending_mapping_batches) >= _MAX_PENDING_IMAGE_MAPPING_BATCHES:
            drain_mapping_batches(
                block=True,
                emit_progress=emit_progress,
                maximum=1,
            )
        else:
            drain_mapping_batches(
                block=False,
                emit_progress=emit_progress,
            )

    def flush_all_mappings(
        *,
        emit_progress: bool,
        suppress_mapping_error: bool = False,
    ) -> None:
        submit_pending_mapping_results(emit_progress=emit_progress)
        drain_mapping_batches(
            block=True,
            emit_progress=emit_progress,
            suppress_mapping_error=suppress_mapping_error,
        )

    def finish_store_result(
        pending: _PendingImageStore,
        *,
        emit_progress: bool,
        suppress_store_error: bool,
    ) -> None:
        try:
            stored_image = pending.future.result()
        except BaseException as error:
            flush_all_mappings(
                emit_progress=emit_progress,
                suppress_mapping_error=suppress_store_error,
            )
            if emit_progress and isinstance(error, Exception):
                result = store_failure_result(pending, error)
                release_owner(pending.claim_key, pending.claim, result)
                return
            _release_image_url_claim(
                pending.claim_key,
                pending.claim,
                error=error,
            )
            if not suppress_store_error:
                raise
            return

        result: DownloadFileResult = {
            "url": pending.image_task["url"],
            "save_path": stored_image["unique_path"],
            "success": True,
        }
        pending_mapping_results.append(
            _PendingImageMapping(
                result=result,
                mapping=(
                    pending.image_task["url"],
                    Path(stored_image["unique_path"]),
                ),
                claim_key=pending.claim_key,
                claim=pending.claim,
            )
        )
        if len(pending_mapping_results) >= _MAX_PENDING_IMAGE_MAPPING_RESULTS:
            submit_pending_mapping_results(emit_progress=emit_progress)

    def drain_store_results(
        *,
        block: bool,
        emit_progress: bool,
        suppress_store_error: bool = False,
        maximum: int | None = None,
    ) -> None:
        drained = 0
        while pending_store_results and (
            maximum is None or drained < maximum
        ):
            pending = pending_store_results[0]
            if not block and not pending.future.done():
                return
            pending_store_results.popleft()
            finish_store_result(
                pending,
                emit_progress=emit_progress,
                suppress_store_error=suppress_store_error,
            )
            drained += 1

    with tempfile.TemporaryDirectory(prefix="nga_image_download_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        download_tasks: list[DownloadTask] = []
        owner_by_temp_path: dict[
            str,
            tuple[ImageDownloadTask, _ImageClaimKey, _ImageURLClaim],
        ] = {}
        for index, owner in enumerate(owners):
            image_task, claim_key, claim = owner
            temp_path = temp_dir / f"image_{index}"
            temp_path_str = str(temp_path)
            owner_by_temp_path[temp_path_str] = owner
            download_tasks.append(
                {
                    "url": image_task["url"],
                    "save_path": temp_path_str,
                }
            )
        owners.clear()

        def handle_progress(
            _completed: int,
            _total: int,
            download_result: DownloadFileResult,
        ) -> None:
            image_task, claim_key, claim = owner_by_temp_path[
                download_result["save_path"]
            ]
            if download_result["success"]:
                if len(pending_store_results) >= image_store_pending_limit():
                    drain_store_results(
                        block=True,
                        emit_progress=True,
                        maximum=1,
                    )
                source_path = Path(download_result["save_path"])
                pending_store_results.append(
                    _PendingImageStore(
                        download_result=download_result,
                        image_task=image_task,
                        claim_key=claim_key,
                        claim=claim,
                        future=submit_image_store_work(
                            lambda source_path=source_path,
                            image_task=image_task,
                            download_result=download_result: (
                                _store_image_file_without_mapping(
                                    source_path,
                                    image_task,
                                    move_source=True,
                                    download_result=download_result,
                                )
                            )
                        ),
                    )
                )
                owner_by_temp_path.pop(download_result["save_path"])
                drain_store_results(block=False, emit_progress=True)
                return

            drain_store_results(block=True, emit_progress=True)
            flush_all_mappings(emit_progress=True)
            result: DownloadFileResult = {
                "url": image_task["url"],
                "save_path": str(unique_images_dir()),
                "success": False,
                "error": download_result.get("error", "unknown"),
                "failure_kind": download_result.get(
                    "failure_kind",
                    "unexpected_download",
                ),
            }
            if "http_status" in download_result:
                result["http_status"] = download_result["http_status"]
            owner_by_temp_path.pop(download_result["save_path"])
            release_owner(claim_key, claim, result)

        try:
            if download_tasks:
                downloads.download_files_streaming(
                    download_tasks,
                    on_progress=handle_progress,
                )
            drain_store_results(block=True, emit_progress=True)
            flush_all_mappings(emit_progress=True)
        except BaseException as error:
            drain_store_results(
                block=True,
                emit_progress=False,
                suppress_store_error=True,
            )
            flush_all_mappings(
                emit_progress=False,
                suppress_mapping_error=True,
            )
            for _task, claim_key, claim in owner_by_temp_path.values():
                _release_image_url_claim(claim_key, claim, error=error)
            raise

    for image_task, claim in waiters:
        _wait_image_url_claim(claim)
        if claim.error is not None:
            raise RuntimeError(
                f"共享图片下载失败：{image_task['url']}"
            ) from claim.error
        if claim.result is None:
            raise RuntimeError(f"共享图片下载没有结果：{image_task['url']}")
        emit_result(_copy_claim_result(image_task["url"], claim.result))

    return succeeded_count, succeeded, failed


def download_image_tasks(
    image_tasks: list[ImageDownloadTask],
    on_progress: DownloadProgressCallback | None = None,
) -> DownloadSummary:
    succeeded_count, succeeded, failed = _run_download_image_tasks(
        image_tasks,
        on_progress,
        collect_successes=True,
    )
    if succeeded_count != len(succeeded):
        raise RuntimeError("图片下载成功结果收集不完整。")
    return {"succeeded": succeeded, "failed": failed}


def download_image_tasks_compact(
    image_tasks: list[ImageDownloadTask],
    on_progress: DownloadProgressCallback | None = None,
) -> CompactImageDownloadSummary:
    succeeded_count, _succeeded, failed = _run_download_image_tasks(
        image_tasks,
        on_progress,
        collect_successes=False,
    )
    return {"succeeded_count": succeeded_count, "failed": failed}

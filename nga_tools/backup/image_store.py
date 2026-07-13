from __future__ import annotations

import datetime
import filecmp
import os
import sqlite3
import tempfile
import threading
from concurrent.futures import Future
from contextlib import closing, contextmanager
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Iterable, NotRequired, TypedDict
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

from nga_tools import utils
from nga_tools.config import get_config
from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.atomic import (
    replace_file_atomically,
    replace_temp_file,
    temporary_sibling_path,
)
from nga_tools.core.image_formats import (
    image_extension_from_file as detect_image_extension_from_file,
    image_file_is_valid,
)
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    ImageValidationOutcome,
    PersistentValidationEntry,
    canonical_image_path_key,
    current_image_validation_cache,
    invalidate_current_image_validation,
)
from nga_tools.timing import time_section
from nga_tools.backup.image_index_writer import (
    ImageMappingRow,
    active_image_index_writer,
)
from nga_tools.backup.image_store_metrics import (
    record_image_hash_source,
    record_image_mapping_failure,
    record_image_mapping_submission,
    record_image_single_flight_wait,
    record_image_store_attempt,
    record_image_store_completed,
    record_image_store_failed,
    time_image_store_phase,
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
    failed: list[utils.DownloadFileResult]


@dataclass(frozen=True)
class NgaImageUrl:
    url: str
    month_dir: str
    day_dir: str
    filename: str


@dataclass(frozen=True)
class ImageMapping:
    url: str
    unique_rel_path: str

    @property
    def unique_path(self) -> Path:
        return output_dir() / self.unique_rel_path


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
            image_mappings_for_urls(urls),
            validation_cache
            if validation_cache is not None
            else ImageValidationCache(),
        )

    @classmethod
    def for_tasks(cls, tasks: Iterable[ImageDownloadTask]) -> ImageLookupCache:
        return cls.for_urls(task["url"] for task in tasks)

    def mapped_image_path_for_url(self, url: str) -> Path | None:
        normalized_url = normalize_nga_image_url(url)
        if not utils.NGA_img_link_verify(normalized_url):
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


IMAGE_INDEX_FILENAME = "image_index.sqlite3"
PLACEHOLDER_IMAGE_FILENAME = "download_failed_placeholder.png"
_IMAGE_STORE_LOCK = threading.RLock()
_IMAGE_HASH_LOCKS = tuple(threading.Lock() for _index in range(256))
_IMAGE_PREPARATION_SEMAPHORE = threading.BoundedSemaphore(1)
_INITIALIZED_IMAGE_INDEX_PATHS: set[Path] = set()


@dataclass(slots=True)
class _ImageURLClaim:
    result: utils.DownloadFileResult | None = None
    error: BaseException | None = None
    completed: bool = False


type _ImageClaimKey = tuple[str, str]


@dataclass(frozen=True)
class _PendingImageMapping:
    result: utils.DownloadFileResult
    mapping: tuple[str, Path]
    claim_key: _ImageClaimKey
    claim: _ImageURLClaim


_IMAGE_URL_CLAIMS_LOCK = threading.RLock()
_IMAGE_URL_CLAIMS_CONDITION = threading.Condition(_IMAGE_URL_CLAIMS_LOCK)
_IMAGE_URL_CLAIMS: dict[_ImageClaimKey, _ImageURLClaim] = {}
_COMPLETED_IMAGE_URL_CLAIMS: dict[_ImageClaimKey, str] = {}
_image_coordination_scope_depth = 0
_MAX_PENDING_IMAGE_MAPPING_RESULTS = 64


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


def normalize_nga_image_url(url: str) -> str:
    return url.replace(",", "")


def existing_image_paths_for_urls(
    output_root: Path,
    urls: Iterable[str],
) -> dict[str, Path]:
    """Return valid, already-downloaded NGA images under ``output_root``.

    Unlike the command-oriented image-store helpers, this lookup never creates
    the image index and does not depend on the process-global configured output
    directory.  That makes it safe for the web viewer's explicit output root.
    """
    normalized_urls = sorted(
        {
            normalized_url
            for url in urls
            if utils.NGA_img_link_verify(
                normalized_url := normalize_nga_image_url(url.strip())
            )
        }
    )
    if not normalized_urls:
        return {}

    resolved_output_root = output_root.resolve()
    images_root = (resolved_output_root / "images_unique").resolve()
    db_path = resolved_output_root / IMAGE_INDEX_FILENAME
    if not db_path.is_file():
        return {}

    rows: list[tuple[object, object]] = []
    try:
        database_uri = f"{db_path.as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(
                database_uri,
                timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                uri=True,
            )
        ) as connection:
            configure_readonly_connection(connection)
            for chunk in iter_in_clause_chunks(normalized_urls):
                placeholders = ",".join("?" for _ in chunk)
                rows.extend(
                    connection.execute(
                        f"""
                        SELECT url, unique_rel_path
                        FROM image_mappings
                        WHERE url IN ({placeholders})
                        """,
                        chunk,
                    ).fetchall()
                )
    except sqlite3.Error:
        return {}

    paths_by_url: dict[str, Path] = {}
    for raw_url, raw_relative_path in rows:
        if not isinstance(raw_url, str) or not isinstance(raw_relative_path, str):
            continue
        relative_path = Path(raw_relative_path)
        if relative_path.is_absolute() or not relative_path.parts:
            continue
        if relative_path.parts[0] != "images_unique":
            continue
        image_path = (resolved_output_root / relative_path).resolve()
        if not image_path.is_relative_to(images_root):
            continue
        if not _image_file_is_valid(image_path):
            continue
        paths_by_url[raw_url] = image_path
    return paths_by_url


def parse_nga_image_url(url: str) -> NgaImageUrl:
    if not utils.NGA_img_link_verify(url):
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
    return Path(get_config().output_dir)


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


def image_index_path() -> Path:
    return output_dir() / IMAGE_INDEX_FILENAME


def unique_image_src_from_html_dir(url: str, html_dir: str | Path) -> str | None:
    image_path = mapped_image_path_for_url(url)
    if image_path is None:
        return None
    return os.path.relpath(image_path, html_dir).replace("\\", "/")


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _initialize_image_index() -> Path:
    db_path = image_index_path().resolve()
    with _IMAGE_STORE_LOCK:
        if db_path in _INITIALIZED_IMAGE_INDEX_PATHS and db_path.is_file():
            return db_path

        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        ) as connection:
            configure_connection(connection)
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS image_mappings (
                    url TEXT PRIMARY KEY,
                    unique_rel_path TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS image_validation_cache (
                    canonical_path TEXT PRIMARY KEY,
                    size INTEGER NOT NULL,
                    mtime_ns INTEGER NOT NULL,
                    valid INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
        _INITIALIZED_IMAGE_INDEX_PATHS.add(db_path)
    return db_path


def _connect_image_index_writable() -> sqlite3.Connection:
    connection = sqlite3.connect(
        _initialize_image_index(),
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    configure_connection(connection)
    return connection


def _connect_image_index_readonly() -> sqlite3.Connection:
    db_uri = f"{_initialize_image_index().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        db_uri,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        uri=True,
    )
    configure_readonly_connection(connection)
    return connection


def _unique_rel_path(path: Path) -> str:
    return os.path.relpath(path, output_dir()).replace("\\", "/")


def upsert_image_mapping(url: str, unique_path: Path) -> ImageMapping:
    return upsert_image_mappings([(url, unique_path)])[0]


def upsert_image_mappings(
    mappings: list[tuple[str, Path]],
) -> list[ImageMapping]:
    image_mappings, future = enqueue_image_mappings(mappings)
    _wait_image_mapping(future)
    return image_mappings


def enqueue_image_mappings(
    mappings: list[tuple[str, Path]],
) -> tuple[list[ImageMapping], Future[None]]:
    if not mappings:
        future = Future[None]()
        future.set_result(None)
        return [], future

    record_image_mapping_submission(len(mappings))
    with time_image_store_phase("mapping_submit"):
        now = _now_utc_iso()
        image_mappings = [
            ImageMapping(url=url, unique_rel_path=_unique_rel_path(unique_path))
            for url, unique_path in mappings
        ]
        rows: list[ImageMappingRow] = [
            (mapping.url, mapping.unique_rel_path, now, now)
            for mapping in image_mappings
        ]
        db_path = _initialize_image_index()
        writer = active_image_index_writer(db_path)
        if writer is not None:
            return image_mappings, writer.submit(rows)

        future = Future[None]()
        with _IMAGE_STORE_LOCK:
            try:
                with closing(_connect_image_index_writable()) as connection:
                    with connection:
                        connection.executemany(
                            """
                            INSERT INTO image_mappings (
                                url,
                                unique_rel_path,
                                created_at,
                                updated_at
                            )
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(url) DO UPDATE SET
                                unique_rel_path = excluded.unique_rel_path,
                                updated_at = excluded.updated_at
                            """,
                            rows,
                        )
            except BaseException as error:
                future.set_exception(error)
            else:
                future.set_result(None)
        return image_mappings, future


def _wait_image_mapping(future: Future[None]) -> None:
    with time_image_store_phase("mapping_wait"):
        try:
            future.result()
        except BaseException:
            record_image_mapping_failure()
            raise


def load_persistent_validation_cache(
    canonical_paths: set[str],
) -> dict[str, tuple[int, int, bool]]:
    if not canonical_paths:
        return {}
    entries: dict[str, tuple[int, int, bool]] = {}
    sorted_paths = sorted(canonical_paths)
    try:
        with closing(_connect_image_index_readonly()) as connection:
            for chunk in iter_in_clause_chunks(sorted_paths):
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT canonical_path, size, mtime_ns, valid
                    FROM image_validation_cache
                    WHERE canonical_path IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for path, size, mtime_ns, valid in rows:
                    if (
                        isinstance(path, str)
                        and type(size) is int
                        and type(mtime_ns) is int
                        and type(valid) is int
                    ):
                        entries[path] = (size, mtime_ns, bool(valid))
    except sqlite3.Error:
        return {}
    return entries


def save_persistent_validation_entries(
    entries: list[PersistentValidationEntry],
) -> None:
    if not entries:
        return
    now = _now_utc_iso()
    rows = [
        (entry["canonical_path"], entry["size"], entry["mtime_ns"], int(entry["valid"]), now)
        for entry in entries
    ]
    with _IMAGE_STORE_LOCK:
        try:
            with closing(_connect_image_index_writable()) as connection:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO image_validation_cache (
                            canonical_path,
                            size,
                            mtime_ns,
                            valid,
                            updated_at
                        )
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(canonical_path) DO UPDATE SET
                            size = excluded.size,
                            mtime_ns = excluded.mtime_ns,
                            valid = excluded.valid,
                            updated_at = excluded.updated_at
                        """,
                        rows,
                    )
        except sqlite3.Error:
            pass


def delete_persistent_validation_entry(canonical_path: str) -> None:
    with _IMAGE_STORE_LOCK:
        try:
            with closing(_connect_image_index_writable()) as connection:
                with connection:
                    connection.execute(
                        "DELETE FROM image_validation_cache WHERE canonical_path = ?",
                        (canonical_path,),
                    )
        except sqlite3.Error:
            pass


def image_mapping_for_url(url: str) -> ImageMapping | None:
    normalized_url = normalize_nga_image_url(url)
    if not utils.NGA_img_link_verify(normalized_url):
        return None

    with closing(_connect_image_index_readonly()) as connection:
        row = connection.execute(
            "SELECT unique_rel_path FROM image_mappings WHERE url = ?",
            (normalized_url,),
        ).fetchone()

    if row is None:
        return None
    unique_rel_path = row[0]
    if not isinstance(unique_rel_path, str):
        return None
    return ImageMapping(url=normalized_url, unique_rel_path=unique_rel_path)


def image_mappings_by_url() -> dict[str, ImageMapping]:
    with closing(_connect_image_index_readonly()) as connection:
        rows = connection.execute(
            "SELECT url, unique_rel_path FROM image_mappings"
        ).fetchall()

    mappings: dict[str, ImageMapping] = {}
    for url, unique_rel_path in rows:
        if isinstance(url, str) and isinstance(unique_rel_path, str):
            mappings[url] = ImageMapping(url=url, unique_rel_path=unique_rel_path)
    return mappings


def image_mappings_for_urls(urls: Iterable[str]) -> dict[str, ImageMapping]:
    normalized_urls = sorted(
        {
            normalized_url
            for url in urls
            if utils.NGA_img_link_verify(
                normalized_url := normalize_nga_image_url(url)
            )
        }
    )
    if not normalized_urls:
        return {}

    mappings: dict[str, ImageMapping] = {}
    with closing(_connect_image_index_readonly()) as connection:
        for start in range(0, len(normalized_urls), 900):
            chunk = normalized_urls[start : start + 900]
            placeholders = ",".join("?" for _ in chunk)
            rows = connection.execute(
                f"""
                SELECT url, unique_rel_path
                FROM image_mappings
                WHERE url IN ({placeholders})
                """,
                chunk,
            ).fetchall()
            for url, unique_rel_path in rows:
                if isinstance(url, str) and isinstance(unique_rel_path, str):
                    mappings[url] = ImageMapping(
                        url=url,
                        unique_rel_path=unique_rel_path,
                    )
    return mappings


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
        mappings_by_url = image_mappings_for_urls(
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
    if not utils.NGA_img_link_verify(normalized_url):
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


def _image_extension_from_file(path: Path, url: str) -> str:
    return detect_image_extension_from_file(path) or _image_extension_from_url(url)


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
    download_result: utils.DownloadFileResult | None,
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
    download_result: utils.DownloadFileResult | None,
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
        return utils.sha256(str(source_path))


def _store_image_file_deferred_mapping(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
    download_result: utils.DownloadFileResult | None = None,
) -> tuple[StoredImageResult, Future[None]]:
    result = _store_image_file_without_mapping(
        source_path,
        task,
        move_source=move_source,
        download_result=download_result,
    )
    _mappings, mapping_future = enqueue_image_mappings(
        [(task["url"], Path(result["unique_path"]))]
    )
    return result, mapping_future


def _store_image_file_without_mapping(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
    download_result: utils.DownloadFileResult | None = None,
) -> StoredImageResult:
    record_image_store_attempt()
    try:
        with time_image_store_phase("source_validation"):
            source_is_valid = image_file_is_valid(source_path)
        if not source_is_valid:
            raise ValueError(f"图片文件无效：{source_path}")
        image_hash = _content_hash_for_store(source_path, download_result)
        with time_image_store_phase("format_detection"):
            extension = _image_extension_from_file(source_path, task["url"])
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
    result: utils.DownloadFileResult | None = None,
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
    result: utils.DownloadFileResult,
) -> utils.DownloadFileResult:
    copied = result.copy()
    copied["url"] = url
    return copied


def _run_download_image_tasks(
    image_tasks: list[ImageDownloadTask],
    on_progress: utils.DownloadProgressCallback | None,
    *,
    collect_successes: bool,
) -> tuple[
    int,
    list[utils.DownloadFileResult],
    list[utils.DownloadFileResult],
]:
    if not image_tasks:
        return 0, [], []

    succeeded_count = 0
    succeeded: list[utils.DownloadFileResult] = []
    failed: list[utils.DownloadFileResult] = []
    owners: list[tuple[ImageDownloadTask, _ImageClaimKey, _ImageURLClaim]] = []
    waiters: list[tuple[ImageDownloadTask, _ImageURLClaim]] = []
    for image_task in image_tasks:
        claim_key, claim, is_owner = _claim_image_url(image_task["url"])
        if is_owner:
            owners.append((image_task, claim_key, claim))
        else:
            waiters.append((image_task, claim))

    completed = 0

    def emit_result(result: utils.DownloadFileResult) -> None:
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

    pending_mapping_results: list[_PendingImageMapping] = []

    def release_owner(
        claim_key: _ImageClaimKey,
        claim: _ImageURLClaim,
        result: utils.DownloadFileResult,
    ) -> None:
        _release_image_url_claim(claim_key, claim, result=result)
        emit_result(result)

    def mapping_failure_result(
        pending: _PendingImageMapping,
        error: BaseException,
    ) -> utils.DownloadFileResult:
        return {
            "url": pending.result["url"],
            "save_path": str(unique_images_dir()),
            "success": False,
            "error": str(error),
            "failure_kind": "image_store",
        }

    def release_remaining_without_progress(
        pending_batch: tuple[_PendingImageMapping, ...],
        *,
        results: tuple[utils.DownloadFileResult, ...] | None = None,
        error: BaseException | None = None,
    ) -> None:
        for index, pending in enumerate(pending_batch):
            _release_image_url_claim(
                pending.claim_key,
                pending.claim,
                result=None if results is None else results[index],
                error=error,
            )

    def flush_pending_mapping_results(
        *,
        emit_progress: bool,
        suppress_mapping_error: bool = False,
    ) -> None:
        if not pending_mapping_results:
            return
        pending_batch = tuple(pending_mapping_results)
        pending_mapping_results.clear()
        try:
            _mappings, mapping_future = enqueue_image_mappings(
                [pending.mapping for pending in pending_batch]
            )
            _wait_image_mapping(mapping_future)
        except BaseException as mapping_error:
            if emit_progress and isinstance(mapping_error, Exception):
                failed_results = tuple(
                    mapping_failure_result(pending, mapping_error)
                    for pending in pending_batch
                )
                for index, (pending, failed_result) in enumerate(
                    zip(pending_batch, failed_results, strict=True)
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
                            pending_batch[index + 1 :],
                            results=failed_results[index + 1 :],
                        )
                        raise
                return
            release_remaining_without_progress(
                pending_batch,
                error=mapping_error,
            )
            if not suppress_mapping_error:
                raise
            return

        if not emit_progress:
            release_remaining_without_progress(
                pending_batch,
                results=tuple(pending.result for pending in pending_batch),
            )
            return
        for index, pending in enumerate(pending_batch):
            _release_image_url_claim(
                pending.claim_key,
                pending.claim,
                result=pending.result,
            )
            try:
                emit_result(pending.result)
            except BaseException:
                release_remaining_without_progress(
                    pending_batch[index + 1 :],
                    results=tuple(
                        remaining.result
                        for remaining in pending_batch[index + 1 :]
                    ),
                )
                raise

    with tempfile.TemporaryDirectory(prefix="nga_image_download_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        download_tasks: list[utils.DownloadTask] = []
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
            download_result: utils.DownloadFileResult,
        ) -> None:
            image_task, claim_key, claim = owner_by_temp_path[
                download_result["save_path"]
            ]
            result: utils.DownloadFileResult
            if download_result["success"]:
                try:
                    stored_image = _store_image_file_without_mapping(
                        Path(download_result["save_path"]),
                        image_task,
                        move_source=True,
                        download_result=download_result,
                    )
                    result = {
                        "url": image_task["url"],
                        "save_path": stored_image["unique_path"],
                        "success": True,
                    }
                    owner_by_temp_path.pop(download_result["save_path"])
                    pending_mapping_results.append(
                        _PendingImageMapping(
                            result=result,
                            mapping=(
                                image_task["url"],
                                Path(stored_image["unique_path"]),
                            ),
                            claim_key=claim_key,
                            claim=claim,
                        )
                    )
                    if (
                        len(pending_mapping_results)
                        >= _MAX_PENDING_IMAGE_MAPPING_RESULTS
                    ):
                        flush_pending_mapping_results(
                            emit_progress=True,
                        )
                    return
                except Exception as error:
                    result = {
                        "url": image_task["url"],
                        "save_path": str(unique_images_dir()),
                        "success": False,
                        "error": str(error),
                        "failure_kind": "image_store",
                    }
            else:
                result = {
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
            flush_pending_mapping_results(emit_progress=True)
            owner_by_temp_path.pop(download_result["save_path"])
            release_owner(claim_key, claim, result)

        try:
            if download_tasks:
                utils.download_files_streaming(
                    download_tasks,
                    on_progress=handle_progress,
                )
            flush_pending_mapping_results(emit_progress=True)
        except BaseException as error:
            flush_pending_mapping_results(
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
    on_progress: utils.DownloadProgressCallback | None = None,
) -> utils.DownloadSummary:
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
    on_progress: utils.DownloadProgressCallback | None = None,
) -> CompactImageDownloadSummary:
    succeeded_count, _succeeded, failed = _run_download_image_tasks(
        image_tasks,
        on_progress,
        collect_successes=False,
    )
    return {"succeeded_count": succeeded_count, "failed": failed}

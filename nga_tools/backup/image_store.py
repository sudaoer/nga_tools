from __future__ import annotations

import datetime
import filecmp
import os
import sqlite3
import tempfile
import threading
from contextlib import closing, contextmanager
from collections.abc import Generator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NotRequired, TypedDict
from concurrent.futures import Future
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
from nga_tools.replay.offline import image_request_url
from nga_tools.timing import time_section
from nga_tools.backup.image_index_writer import (
    ImageMappingRow,
    active_image_index_writer,
)


class ImageDownloadTask(TypedDict):
    url: str


class StoredImageResult(TypedDict):
    url: str
    unique_path: str
    reused: bool
    collision: NotRequired[bool]


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
_INITIALIZED_IMAGE_INDEX_PATHS: set[Path] = set()


@dataclass
class _ImageURLClaim:
    event: threading.Event = field(default_factory=threading.Event)
    result: utils.DownloadFileResult | None = None
    error: BaseException | None = None


type _ImageClaimKey = tuple[str, str]

_IMAGE_URL_CLAIMS_LOCK = threading.RLock()
_IMAGE_URL_CLAIMS: dict[_ImageClaimKey, _ImageURLClaim] = {}
_COMPLETED_IMAGE_URL_CLAIMS: dict[
    _ImageClaimKey,
    utils.DownloadFileResult,
] = {}
_image_coordination_scope_depth = 0


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
    future.result()
    return image_mappings


def enqueue_image_mappings(
    mappings: list[tuple[str, Path]],
) -> tuple[list[ImageMapping], Future[None]]:
    if not mappings:
        future = Future[None]()
        future.set_result(None)
        return [], future

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
    mapping_future.result()
    return stored_image


def _hash_lock(image_hash: str) -> threading.Lock:
    try:
        stripe = int(image_hash[:2], 16)
    except ValueError:
        stripe = hash(image_hash) % len(_IMAGE_HASH_LOCKS)
    return _IMAGE_HASH_LOCKS[stripe]


def _store_image_file_deferred_mapping(
    source_path: Path,
    task: ImageDownloadTask,
    *,
    move_source: bool,
) -> tuple[StoredImageResult, Future[None]]:
    if not image_file_is_valid(source_path):
        raise ValueError(f"图片文件无效：{source_path}")
    image_hash = utils.sha256(str(source_path))
    extension = _image_extension_from_file(source_path, task["url"])
    with _hash_lock(image_hash):
        target_path, reused, collision = _target_path_for_download(
            source_path,
            image_hash,
            extension,
        )
        if not reused:
            if target_path.exists():
                invalidate_current_image_validation(target_path)
            if move_source:
                replace_file_atomically(source_path, target_path, move_source=True)
            elif source_path.resolve() != target_path.resolve():
                replace_file_atomically(source_path, target_path, move_source=False)
            if not _image_file_is_valid(target_path):
                raise ValueError(f"图片保存后无法校验：{target_path}")
    result: StoredImageResult = {
        "url": task["url"],
        "unique_path": str(target_path),
        "reused": reused,
    }
    if collision:
        result["collision"] = True
    _mappings, mapping_future = enqueue_image_mappings(
        [(task["url"], target_path)]
    )
    return result, mapping_future


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
        completed_result = _COMPLETED_IMAGE_URL_CLAIMS.get(claim_key)
        if completed_result is not None:
            completed_path = Path(completed_result["save_path"])
            if image_file_is_valid(completed_path):
                completed_claim = _ImageURLClaim(result=completed_result)
                completed_claim.event.set()
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
    claim.result = result
    claim.error = error
    claim.event.set()
    with _IMAGE_URL_CLAIMS_LOCK:
        if result is not None and result["success"]:
            _COMPLETED_IMAGE_URL_CLAIMS[claim_key] = result
        else:
            _COMPLETED_IMAGE_URL_CLAIMS.pop(claim_key, None)
        if _IMAGE_URL_CLAIMS.get(claim_key) is claim:
            _IMAGE_URL_CLAIMS.pop(claim_key, None)


def _copy_claim_result(
    url: str,
    result: utils.DownloadFileResult,
) -> utils.DownloadFileResult:
    copied = result.copy()
    copied["url"] = url
    return copied


def download_image_tasks(
    image_tasks: list[ImageDownloadTask],
    on_progress: utils.DownloadProgressCallback | None = None,
) -> utils.DownloadSummary:
    if not image_tasks:
        return {"succeeded": [], "failed": []}

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
        nonlocal completed
        completed += 1
        if result["success"]:
            succeeded.append(result)
        else:
            failed.append(result)
        if on_progress is not None:
            on_progress(completed, len(image_tasks), result)

    pending_store_results: list[
        tuple[
            utils.DownloadFileResult,
            Future[None],
            _ImageClaimKey,
            _ImageURLClaim,
        ]
    ] = []
    released_owner_claim_ids: set[int] = set()

    def release_owner(
        claim_key: _ImageClaimKey,
        claim: _ImageURLClaim,
        result: utils.DownloadFileResult,
    ) -> None:
        _release_image_url_claim(claim_key, claim, result=result)
        released_owner_claim_ids.add(id(claim))
        emit_result(result)

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
                    "request_url": image_request_url(image_task["url"]),
                    "save_path": temp_path_str,
                }
            )

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
                    stored_image, mapping_future = (
                        _store_image_file_deferred_mapping(
                            Path(download_result["save_path"]),
                            image_task,
                            move_source=True,
                        )
                    )
                    result = {
                        "url": image_task["url"],
                        "save_path": stored_image["unique_path"],
                        "success": True,
                    }
                    pending_store_results.append(
                        (result, mapping_future, claim_key, claim)
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
            release_owner(claim_key, claim, result)

        try:
            if download_tasks:
                utils.download_files(download_tasks, on_progress=handle_progress)
            for result, mapping_future, claim_key, claim in pending_store_results:
                try:
                    mapping_future.result()
                except Exception as error:
                    failed_result: utils.DownloadFileResult = {
                        "url": result["url"],
                        "save_path": str(unique_images_dir()),
                        "success": False,
                        "error": str(error),
                        "failure_kind": "image_store",
                    }
                    release_owner(claim_key, claim, failed_result)
                else:
                    release_owner(claim_key, claim, result)
        except BaseException as error:
            for result, mapping_future, claim_key, claim in pending_store_results:
                if id(claim) in released_owner_claim_ids:
                    continue
                try:
                    mapping_future.result()
                except BaseException as mapping_error:
                    _release_image_url_claim(
                        claim_key,
                        claim,
                        error=mapping_error,
                    )
                else:
                    _release_image_url_claim(
                        claim_key,
                        claim,
                        result=result,
                    )
                released_owner_claim_ids.add(id(claim))
            for _task, claim_key, claim in owners:
                if id(claim) not in released_owner_claim_ids:
                    _release_image_url_claim(claim_key, claim, error=error)
            raise

    for image_task, claim in waiters:
        claim.event.wait()
        if claim.error is not None:
            raise RuntimeError(
                f"共享图片下载失败：{image_task['url']}"
            ) from claim.error
        if claim.result is None:
            raise RuntimeError(f"共享图片下载没有结果：{image_task['url']}")
        emit_result(_copy_claim_result(image_task["url"], claim.result))

    return {"succeeded": succeeded, "failed": failed}

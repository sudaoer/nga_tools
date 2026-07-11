from __future__ import annotations

import datetime
import filecmp
import os
import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, NotRequired, TypedDict
from urllib.parse import urlsplit

from PIL import Image, ImageDraw

from nga_tools import utils
from nga_tools.config import get_config
from nga_tools.console import report_warning
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
)
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    ImageValidationOutcome,
    canonical_image_path_key,
    current_image_validation_cache,
    invalidate_current_image_validation,
)
from nga_tools.timing import time_section


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
    batch_validation_cache_hit_path_count: int
    deep_validation_path_count: int
    invalid_mapping_count: int
    pending_download_url_count: int


@dataclass(frozen=True)
class ImageDownloadPreparation:
    pending_tasks: list[ImageDownloadTask]
    stats: ImagePreparationStats


IMAGE_INDEX_FILENAME = "image_index.sqlite3"
PLACEHOLDER_IMAGE_FILENAME = "download_failed_placeholder.png"
_IMAGE_STORE_LOCK = threading.RLock()


def normalize_nga_image_url(url: str) -> str:
    return url.replace(",", "")


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


def _connect_image_index() -> sqlite3.Connection:
    db_path = image_index_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
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
    connection.commit()
    return connection


def _unique_rel_path(path: Path) -> str:
    return os.path.relpath(path, output_dir()).replace("\\", "/")


def upsert_image_mapping(url: str, unique_path: Path) -> ImageMapping:
    return upsert_image_mappings([(url, unique_path)])[0]


def upsert_image_mappings(
    mappings: list[tuple[str, Path]],
) -> list[ImageMapping]:
    if not mappings:
        return []

    now = _now_utc_iso()
    image_mappings = [
        ImageMapping(url=url, unique_rel_path=_unique_rel_path(unique_path))
        for url, unique_path in mappings
    ]
    rows = [
        (mapping.url, mapping.unique_rel_path, now, now)
        for mapping in image_mappings
    ]
    with _IMAGE_STORE_LOCK:
        with closing(_connect_image_index()) as connection:
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
    return image_mappings


def image_mapping_for_url(url: str) -> ImageMapping | None:
    normalized_url = normalize_nga_image_url(url)
    if not utils.NGA_img_link_verify(normalized_url):
        return None

    with closing(_connect_image_index()) as connection:
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
    with closing(_connect_image_index()) as connection:
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
    with closing(_connect_image_index()) as connection:
        connection.execute(
            "CREATE TEMP TABLE _url_lookup (url TEXT PRIMARY KEY)"
        )
        try:
            connection.executemany(
                "INSERT OR IGNORE INTO _url_lookup (url) VALUES (?)",
                [(url,) for url in normalized_urls],
            )
            rows = connection.execute(
                """
                SELECT m.url, m.unique_rel_path
                FROM image_mappings m
                JOIN _url_lookup l ON m.url = l.url
                """
            ).fetchall()
            for url, unique_rel_path in rows:
                if isinstance(url, str) and isinstance(unique_rel_path, str):
                    mappings[url] = ImageMapping(
                        url=url,
                        unique_rel_path=unique_rel_path,
                    )
        finally:
            connection.execute("DROP TABLE _url_lookup")
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
        for path_key, image_path in mapped_paths_by_key.items():
            validation_by_path_key[path_key] = validation_cache.validate(image_path)

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
        batch_validation_cache_hit_path_count=sum(
            outcome.cache_hit for outcome in validation_by_path_key.values()
        ),
        deep_validation_path_count=sum(
            outcome.deep_validated for outcome in validation_by_path_key.values()
        ),
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
            report_warning(f"图片SHA-256 hash碰撞，保存为：{collision_path}")
            return collision_path, False, True
        if not _image_file_is_valid(collision_path):
            report_warning(f"图片SHA-256 hash碰撞，保存为：{collision_path}")
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
    if not _image_file_is_valid(source_path):
        raise ValueError(f"图片文件无效：{source_path}")
    image_hash = utils.sha256(str(source_path))
    extension = _image_extension_from_file(source_path, task["url"])
    with _IMAGE_STORE_LOCK:
        target_path, reused, collision = _target_path_for_download(
            source_path,
            image_hash,
            extension,
        )
        if not reused:
            if move_source:
                replace_file_atomically(source_path, target_path, move_source=True)
            elif source_path.resolve() != target_path.resolve():
                replace_file_atomically(source_path, target_path, move_source=False)
            invalidate_current_image_validation(source_path)
            invalidate_current_image_validation(target_path)
            if not _image_file_is_valid(target_path):
                raise ValueError(f"图片保存后无法校验：{target_path}")

        upsert_image_mapping(task["url"], target_path)
    result: StoredImageResult = {
        "url": task["url"],
        "unique_path": str(target_path),
        "reused": reused,
    }
    if collision:
        result["collision"] = True
    return result


def store_downloaded_image(temp_path: Path, task: ImageDownloadTask) -> StoredImageResult:
    return _store_image_file(temp_path, task, move_source=True)


def store_existing_image(image_path: Path, url: str) -> StoredImageResult:
    return _store_image_file(image_path, {"url": url}, move_source=False)


def download_image_tasks(
    image_tasks: list[ImageDownloadTask],
    on_progress: utils.DownloadProgressCallback | None = None,
) -> utils.DownloadSummary:
    if not image_tasks:
        return {"succeeded": [], "failed": []}

    succeeded: list[utils.DownloadFileResult] = []
    failed: list[utils.DownloadFileResult] = []
    with tempfile.TemporaryDirectory(prefix="nga_image_download_") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        download_tasks: list[utils.DownloadTask] = []
        task_by_temp_path: dict[str, ImageDownloadTask] = {}
        for index, image_task in enumerate(image_tasks):
            temp_path = temp_dir / f"image_{index}"
            temp_path_str = str(temp_path)
            task_by_temp_path[temp_path_str] = image_task
            download_tasks.append(
                {
                    "url": image_task["url"],
                    "save_path": temp_path_str,
                }
            )

        def handle_progress(
            completed: int,
            total: int,
            download_result: utils.DownloadFileResult,
        ) -> None:
            image_task = task_by_temp_path[download_result["save_path"]]
            result: utils.DownloadFileResult
            if download_result["success"]:
                try:
                    stored_image = store_downloaded_image(
                        Path(download_result["save_path"]),
                        image_task,
                    )
                    result = {
                        "url": image_task["url"],
                        "save_path": stored_image["unique_path"],
                        "success": True,
                    }
                    succeeded.append(result)
                except Exception as error:
                    result = {
                        "url": image_task["url"],
                        "save_path": str(unique_images_dir()),
                        "success": False,
                        "error": str(error),
                    }
                    failed.append(result)
            else:
                result = {
                    "url": image_task["url"],
                    "save_path": str(unique_images_dir()),
                    "success": False,
                    "error": download_result.get("error", "unknown"),
                }
                failed.append(result)

            if on_progress is not None:
                on_progress(completed, total, result)

        utils.download_files(download_tasks, on_progress=handle_progress)

    return {"succeeded": succeeded, "failed": failed}

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import NotRequired, TypedDict
from urllib.parse import urlsplit

from PIL import Image

from nga_tools import utils
from nga_tools.config import get_config


class ImageDownloadTask(TypedDict):
    url: str
    link_path: str


class StoredImageResult(TypedDict):
    url: str
    link_path: str
    unique_path: str
    reused: bool
    collision: NotRequired[bool]


@dataclass(frozen=True)
class NgaImageUrl:
    url: str
    month_dir: str
    day_dir: str
    filename: str


IMAGE_FORMAT_BY_PILLOW_FORMAT = {
    "JPEG": "jpg",
    "PNG": "png",
    "GIF": "gif",
    "WEBP": "webp",
}


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


def image_links_dir() -> Path:
    return output_dir() / "images"


def image_link_path(url: str) -> Path:
    parsed_url = parse_nga_image_url(url)
    return (
        image_links_dir()
        / parsed_url.month_dir
        / parsed_url.day_dir
        / parsed_url.filename
    )


def image_link_src_from_html_dir(url: str, html_dir: str | Path) -> str:
    link_path = image_link_path(url)
    return os.path.relpath(link_path, html_dir).replace("\\", "/")


def image_task_is_complete(task: ImageDownloadTask) -> bool:
    link_path = Path(task["link_path"])
    return link_path.is_symlink() and link_path.exists()


def pending_image_download_tasks(
    image_tasks: list[ImageDownloadTask],
) -> list[ImageDownloadTask]:
    return [task for task in image_tasks if not image_task_is_complete(task)]


def link_path_for_image_src(image_src: str) -> Path | None:
    normalized_url = normalize_nga_image_url(image_src)
    if not utils.NGA_img_link_verify(normalized_url):
        return None
    return image_link_path(normalized_url)


def _image_extension_from_url(url: str) -> str:
    filename = parse_nga_image_url(url).filename.lower()
    for extension in ("jpg", "jpeg", "png", "gif", "webp"):
        marker = f".{extension}"
        if marker in filename:
            return "jpg" if extension == "jpeg" else extension
    return "bin"


def _image_extension_from_file(path: Path, url: str) -> str:
    try:
        with Image.open(path) as image:
            image_format = image.format
    except OSError:
        return _image_extension_from_url(url)

    if image_format is None:
        return _image_extension_from_url(url)
    return IMAGE_FORMAT_BY_PILLOW_FORMAT.get(image_format.upper(), image_format.lower())


def _same_file_content(first: Path, second: Path) -> bool:
    if not first.exists() or not second.exists():
        return False
    return filecmp.cmp(first, second, shallow=False)


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
    if _same_file_content(target_path, temp_path):
        return target_path, True, False

    collision_index = 1
    while True:
        collision_path = unique_dir / f"{image_hash}-collision-{collision_index}.{extension}"
        if not collision_path.exists():
            print(
                "警告：图片SHA-256 hash碰撞，"
                f"保存为：{collision_path}"
            )
            return collision_path, False, True
        if _same_file_content(collision_path, temp_path):
            return collision_path, True, True
        collision_index += 1


def _replace_with_relative_symlink(link_path: Path, target_path: Path) -> None:
    link_path.parent.mkdir(parents=True, exist_ok=True)
    if link_path.exists() or link_path.is_symlink():
        if link_path.is_dir() and not link_path.is_symlink():
            raise RuntimeError(f"图片链接路径被目录占用：{link_path}")
        link_path.unlink()

    relative_target = os.path.relpath(target_path, link_path.parent)
    link_path.symlink_to(relative_target)


def store_downloaded_image(temp_path: Path, task: ImageDownloadTask) -> StoredImageResult:
    image_hash = utils.sha256(str(temp_path))
    extension = _image_extension_from_file(temp_path, task["url"])
    target_path, reused, collision = _target_path_for_download(
        temp_path,
        image_hash,
        extension,
    )
    if not reused:
        shutil.move(str(temp_path), target_path)

    link_path = Path(task["link_path"])
    _replace_with_relative_symlink(link_path, target_path)
    result: StoredImageResult = {
        "url": task["url"],
        "link_path": str(link_path),
        "unique_path": str(target_path),
        "reused": reused,
    }
    if collision:
        result["collision"] = True
    return result


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
                        "save_path": stored_image["link_path"],
                        "success": True,
                    }
                    succeeded.append(result)
                except Exception as error:
                    result = {
                        "url": image_task["url"],
                        "save_path": image_task["link_path"],
                        "success": False,
                        "error": str(error),
                    }
                    failed.append(result)
            else:
                result = {
                    "url": image_task["url"],
                    "save_path": image_task["link_path"],
                    "success": False,
                    "error": download_result.get("error", "unknown"),
                }
                failed.append(result)

            if on_progress is not None:
                on_progress(completed, total, result)

        utils.download_files(download_tasks, on_progress=handle_progress)

    return {"succeeded": succeeded, "failed": failed}

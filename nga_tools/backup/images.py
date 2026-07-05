from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional

from PIL import Image

from nga_tools import utils
from nga_tools.config import get_config


@dataclass(frozen=True)
class ImageVerifyResult:
    folder: str
    total: int
    removed: int


@dataclass(frozen=True)
class ImageFileVerifyResult:
    image_file: str
    removed: bool
    error: str | None


def verify_downloaded_images(tid: int, aid: Optional[int]) -> None:
    folder_images = utils.get_folder(tid, aid, "images")
    _verify_images_in_folder(folder_images)


def verify_all_downloaded_images() -> None:
    image_folders = _list_downloaded_image_folders()
    if not image_folders:
        print("没有找到任何图片目录。")
        return

    total_folders = len(image_folders)
    total_images = 0
    total_removed = 0
    for index, folder_images in enumerate(image_folders, start=1):
        print(f"[{index}/{total_folders}] 正在校验图片目录：{folder_images}")
        result = _verify_images_in_folder(folder_images)
        total_images += result.total
        total_removed += result.removed
        print(
            f"[{index}/{total_folders}] 图片目录校验完成："
            f"{folder_images}，删除{result.removed}个损坏文件。"
        )

    print(
        f"全部图片校验完成：目录{total_folders}个，"
        f"图片{total_images}张，删除{total_removed}个损坏文件。"
    )


def _list_downloaded_image_folders() -> list[str]:
    output_dir = Path(get_config().output_dir)
    if not output_dir.is_dir():
        return []

    image_folders = [
        str(images_dir)
        for backup_dir in output_dir.iterdir()
        if backup_dir.is_dir()
        if (images_dir := backup_dir / "images").is_dir()
    ]
    return sorted(image_folders)


def _verify_images_in_folder(folder_images: str) -> ImageVerifyResult:
    image_files = utils.list_files_in_folder(folder_images)
    print(f"已下载图片文件数：{len(image_files)}")
    if not image_files:
        return ImageVerifyResult(folder=folder_images, total=0, removed=0)

    worker_count = _image_verify_worker_count(len(image_files))
    print(f"并行校验worker数：{worker_count}")

    image_tasks = [(folder_images, image_file) for image_file in image_files]
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_verify_image_file_task, image_tasks))

    removed_count = 0
    for result in results:
        if result.error is None:
            continue
        print(f"图片文件损坏或无法打开：{result.image_file}，错误信息：{result.error}")
        if result.removed:
            removed_count += 1

    return ImageVerifyResult(
        folder=folder_images,
        total=len(image_files),
        removed=removed_count,
    )


def _image_verify_worker_count(image_count: int) -> int:
    return max(1, min(32, image_count))


def _verify_image_file(folder_images: str, image_file: str) -> ImageFileVerifyResult:
    image_path = os.path.join(folder_images, image_file)
    try:
        with Image.open(image_path) as image:
            image.verify()
    except (OSError, SyntaxError) as error:
        os.remove(image_path)
        return ImageFileVerifyResult(
            image_file=image_file,
            removed=True,
            error=str(error),
        )

    return ImageFileVerifyResult(
        image_file=image_file,
        removed=False,
        error=None,
    )


def _verify_image_file_task(task: tuple[str, str]) -> ImageFileVerifyResult:
    folder_images, image_file = task
    return _verify_image_file(folder_images, image_file)

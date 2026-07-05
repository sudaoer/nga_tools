from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag
from PIL import Image

from nga_tools import utils
from nga_tools.backup import image_store
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
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified", create=False))
    image_paths = _list_thread_referenced_image_paths(folder_html_modified)
    result = _verify_image_paths(str(folder_html_modified), image_paths)
    print(
        f"帖子图片校验完成：引用图片{result.total}张，"
        f"删除{result.removed}个损坏文件。"
    )


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

    unique_images = output_dir / "images_unique"
    if not unique_images.is_dir():
        return []
    return [str(unique_images)]


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _image_path_from_src(image_src: str, source_dir: Path) -> Path:
    link_path = image_store.link_path_for_image_src(image_src)
    if link_path is not None:
        return link_path

    path = Path(image_src.split("?", 1)[0])
    if path.is_absolute():
        return path
    return source_dir / path


def _list_thread_referenced_image_paths(folder_html_modified: Path) -> list[Path]:
    if not folder_html_modified.is_dir():
        return []

    image_paths: list[Path] = []
    seen_paths: set[str] = set()
    html_files = utils.list_files_in_folder(str(folder_html_modified), ends_with=".html")
    for html_file in html_files:
        html_path = folder_html_modified / html_file
        soup = BeautifulSoup(html_path.read_text(encoding="utf-8"), "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        for image in images:
            image_src = _tag_attr_str(image, "src")
            if not image_src:
                continue
            image_path = _image_path_from_src(image_src, folder_html_modified)
            target_path = image_path.resolve() if image_path.exists() else image_path
            key = str(target_path)
            if key in seen_paths:
                continue
            seen_paths.add(key)
            image_paths.append(target_path)

    return sorted(image_paths)


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


def _verify_image_paths(folder_label: str, image_paths: list[Path]) -> ImageVerifyResult:
    print(f"已下载图片文件数：{len(image_paths)}")
    if not image_paths:
        return ImageVerifyResult(folder=folder_label, total=0, removed=0)

    worker_count = _image_verify_worker_count(len(image_paths))
    print(f"并行校验worker数：{worker_count}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_verify_image_path, image_paths))

    removed_count = 0
    for result in results:
        if result.error is None:
            continue
        print(f"图片文件损坏或无法打开：{result.image_file}，错误信息：{result.error}")
        if result.removed:
            removed_count += 1

    return ImageVerifyResult(
        folder=folder_label,
        total=len(image_paths),
        removed=removed_count,
    )


def _image_verify_worker_count(image_count: int) -> int:
    return max(1, min(32, image_count))


def _verify_image_file(folder_images: str, image_file: str) -> ImageFileVerifyResult:
    image_path = os.path.join(folder_images, image_file)
    return _verify_image_path(Path(image_path))


def _verify_image_path(image_path: Path) -> ImageFileVerifyResult:
    try:
        with Image.open(image_path) as image:
            image.verify()
    except (OSError, SyntaxError) as error:
        removed = False
        if image_path.exists() or image_path.is_symlink():
            image_path.unlink()
            removed = True
        return ImageFileVerifyResult(
            image_file=str(image_path),
            removed=removed,
            error=str(error),
        )

    return ImageFileVerifyResult(
        image_file=str(image_path),
        removed=False,
        error=None,
    )


def _verify_image_file_task(task: tuple[str, str]) -> ImageFileVerifyResult:
    folder_images, image_file = task
    return _verify_image_file(folder_images, image_file)

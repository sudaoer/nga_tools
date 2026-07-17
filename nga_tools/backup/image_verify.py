from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.core.paths import get_folder, list_files_in_folder
from nga_tools.backup import image_index
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels, load_floor_labels_from_archive
from nga_tools.backup.image_pipeline import (
    collect_image_download_tasks_from_parsed,
    parse_post_htmls_for_images,
)
from nga_tools.backup.post_html import load_post_htmls_for_records
from nga_tools.backup.post_overlay import (
    apply_post_overlays_to_records,
    overlay_image_sources,
)
from nga_tools.config import get_config
from nga_tools.console import WarningCategory, report_info, report_warning
from nga_tools.core.image_formats import image_file_error
from nga_tools.timing import time_section


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
    with time_section("图片引用读取"):
        thread_folder = Path(get_folder(tid, aid, create=False))
        image_paths = _list_thread_referenced_image_paths(tid, aid, thread_folder)
    with time_section("图片校验"):
        result = _verify_image_paths(str(thread_folder), image_paths)
    report_info(
        f"帖子图片校验完成：引用图片{result.total}张，"
        f"删除{result.removed}个损坏文件。"
    )


def verify_all_downloaded_images() -> None:
    image_folders = _list_downloaded_image_folders()
    if not image_folders:
        report_info("没有找到任何图片目录。")
        return

    total_folders = len(image_folders)
    total_images = 0
    total_removed = 0
    for index, folder_images in enumerate(image_folders, start=1):
        report_info(f"[{index}/{total_folders}] 正在校验图片目录：{folder_images}")
        result = _verify_images_in_folder(folder_images)
        total_images += result.total
        total_removed += result.removed
        report_info(
            f"[{index}/{total_folders}] 图片目录校验完成："
            f"{folder_images}，删除{result.removed}个损坏文件。"
        )

    report_info(
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


def _list_thread_referenced_image_paths(
    tid: int,
    aid: Optional[int],
    thread_folder: Path,
) -> list[Path]:
    archive_store = ThreadArchiveStore(thread_folder)
    records = archive_store.read_effective_post_records()
    overlays = archive_store.read_post_overlays()
    records = apply_post_overlays_to_records(
        overlays,
        records,
        output_dir=thread_folder.parent,
    )
    htmls = load_post_htmls_for_records(records)
    parsed_htmls = parse_post_htmls_for_images(htmls)
    if aid is None:
        floor_labels = FloorLabels.plain()
    else:
        try:
            floor_labels = load_floor_labels_from_archive(archive_store, aid)
        except Exception as error:
            report_warning(
                WarningCategory.FLOOR_MAP,
                f"无法加载楼层映射，使用普通楼层标签：{error}",
            )
            floor_labels = FloorLabels.plain()
    tasks = collect_image_download_tasks_from_parsed(parsed_htmls, floor_labels)
    task_urls = {task["url"] for task in tasks}
    for overlay in overlays.values():
        for image_src in overlay_image_sources(overlay["bbcode"]):
            normalized_url = image_index.normalize_nga_image_url(image_src)
            if (
                NGA_img_link_verify(normalized_url)
                and normalized_url not in task_urls
            ):
                task_urls.add(normalized_url)
                tasks.append({"url": normalized_url})
    mappings = image_index.ImageIndexStore(
        thread_folder.parent
    ).mappings_for_urls(task["url"] for task in tasks)
    image_paths: list[Path] = []
    seen_paths: set[str] = set()
    for task in tasks:
        normalized_url = image_index.normalize_nga_image_url(task["url"])
        mapping = mappings.get(normalized_url)
        if mapping is None:
            report_warning(
                WarningCategory.IMAGE_PROCESSING,
                f"帖子图片未找到本地映射：{normalized_url}",
            )
            continue
        image_path = mapping.unique_path
        target_path = image_path.resolve() if image_path.exists() else image_path
        key = str(target_path)
        if key in seen_paths:
            continue
        seen_paths.add(key)
        image_paths.append(target_path)

    return sorted(image_paths)


def _verify_images_in_folder(folder_images: str) -> ImageVerifyResult:
    image_files = list_files_in_folder(folder_images)
    report_info(f"已下载图片文件数：{len(image_files)}")
    if not image_files:
        return ImageVerifyResult(folder=folder_images, total=0, removed=0)

    worker_count = _image_verify_worker_count(len(image_files))
    report_info(f"并行校验worker数：{worker_count}")

    image_tasks = [(folder_images, image_file) for image_file in image_files]
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_verify_image_file_task, image_tasks))

    removed_count = 0
    for result in results:
        if result.error is None:
            continue
        report_warning(
            WarningCategory.IMAGE_PROCESSING,
            f"图片文件损坏或无法打开：{result.image_file}，错误信息：{result.error}"
        )
        if result.removed:
            removed_count += 1

    return ImageVerifyResult(
        folder=folder_images,
        total=len(image_files),
        removed=removed_count,
    )


def _verify_image_paths(folder_label: str, image_paths: list[Path]) -> ImageVerifyResult:
    report_info(f"已下载图片文件数：{len(image_paths)}")
    if not image_paths:
        return ImageVerifyResult(folder=folder_label, total=0, removed=0)

    worker_count = _image_verify_worker_count(len(image_paths))
    report_info(f"并行校验worker数：{worker_count}")

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        results = list(executor.map(_verify_image_path, image_paths))

    removed_count = 0
    for result in results:
        if result.error is None:
            continue
        report_warning(
            WarningCategory.IMAGE_PROCESSING,
            f"图片文件损坏或无法打开：{result.image_file}，错误信息：{result.error}"
        )
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
    error = image_file_error(image_path)
    if error is not None:
        removed = False
        if image_path.exists() or image_path.is_symlink():
            image_path.unlink()
            removed = True
        return ImageFileVerifyResult(
            image_file=str(image_path),
            removed=removed,
            error=error,
        )

    return ImageFileVerifyResult(
        image_file=str(image_path),
        removed=False,
        error=None,
    )


def _verify_image_file_task(task: tuple[str, str]) -> ImageFileVerifyResult:
    folder_images, image_file = task
    return _verify_image_file(folder_images, image_file)

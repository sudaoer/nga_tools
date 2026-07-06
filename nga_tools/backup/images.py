from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import os
import shutil
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag
from PIL import Image

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.config import get_config
from nga_tools.console import report_info, report_warning


@dataclass(frozen=True)
class ImageVerifyResult:
    folder: str
    total: int
    removed: int


@dataclass(frozen=True)
class ImageMigrationResult:
    mappings: int
    broken_links: int
    html_files: int
    updated_html_files: int
    updated_image_refs: int


@dataclass(frozen=True)
class ImagePruneResult:
    removed_links: int
    removed_directory: str | None


@dataclass(frozen=True)
class ImageFileVerifyResult:
    image_file: str
    removed: bool
    error: str | None


_NGA_IMAGE_BASE_URL = "https://img.nga.178.com/attachments/"


def verify_downloaded_images(tid: int, aid: Optional[int]) -> None:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified", create=False))
    image_paths = _list_thread_referenced_image_paths(folder_html_modified)
    result = _verify_image_paths(str(folder_html_modified), image_paths)
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


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _legacy_image_url_from_relative_path(relative_path: Path) -> str | None:
    url = _NGA_IMAGE_BASE_URL + relative_path.as_posix()
    normalized_url = image_store.normalize_nga_image_url(url)
    if not utils.NGA_img_link_verify(normalized_url):
        return None
    return normalized_url


def _legacy_image_url_from_link_path(link_path: Path) -> str | None:
    try:
        relative_path = link_path.relative_to(image_store.image_links_dir())
    except ValueError:
        return None
    return _legacy_image_url_from_relative_path(relative_path)


def _html_modified_files() -> list[Path]:
    output_dir = Path(get_config().output_dir)
    if not output_dir.is_dir():
        return []

    html_paths: list[Path] = []
    for thread_dir in sorted(output_dir.iterdir()):
        if not thread_dir.is_dir():
            continue
        folder_html_modified = thread_dir / "html_modified"
        if not folder_html_modified.is_dir():
            continue
        html_paths.extend(sorted(folder_html_modified.glob("post_*.html")))
    return html_paths


def _path_from_src(image_src: str, source_dir: Path) -> Path:
    path = Path(image_src.split("?", 1)[0])
    if path.is_absolute():
        return path
    return source_dir / path


def _legacy_image_url_from_src(image_src: str, source_dir: Path) -> str | None:
    normalized_url = image_store.normalize_nga_image_url(image_src)
    if utils.NGA_img_link_verify(normalized_url):
        return normalized_url

    candidate_path = Path(os.path.abspath(_path_from_src(image_src, source_dir)))
    legacy_dir = Path(os.path.abspath(image_store.image_links_dir()))
    try:
        relative_path = candidate_path.relative_to(legacy_dir)
    except ValueError:
        return None
    return _legacy_image_url_from_relative_path(relative_path)


def _src_points_to_legacy_image_dir(image_src: str, source_dir: Path) -> bool:
    normalized_url = image_store.normalize_nga_image_url(image_src)
    if utils.NGA_img_link_verify(normalized_url):
        return False

    candidate_path = Path(os.path.abspath(_path_from_src(image_src, source_dir)))
    legacy_dir = Path(os.path.abspath(image_store.image_links_dir()))
    try:
        candidate_path.relative_to(legacy_dir)
    except ValueError:
        return False
    return True


def _rewrite_html_file_image_refs(
    html_path: Path,
    unique_path_by_url: dict[str, Path],
) -> int:
    html_text = html_path.read_text(encoding="utf-8")
    if "images/" not in html_text and "img.nga.178.com/attachments" not in html_text:
        return 0

    soup = BeautifulSoup(html_text, "html.parser")
    updated_refs = 0
    images = cast(list[Tag], soup.find_all("img"))
    for image in images:
        image_src = _tag_attr_str(image, "src")
        if not image_src:
            continue

        image_url = _legacy_image_url_from_src(image_src, html_path.parent)
        if image_url is None:
            continue

        unique_path = unique_path_by_url.get(image_url)
        if unique_path is None or not unique_path.exists():
            continue

        new_src = os.path.relpath(unique_path, html_path.parent).replace("\\", "/")
        if new_src == image_src:
            continue

        image["src"] = new_src
        updated_refs += 1

    if updated_refs:
        html_path.write_text(str(soup), encoding="utf-8")
    return updated_refs


def _count_legacy_html_image_refs() -> int:
    ref_count = 0
    for html_path in _html_modified_files():
        html_text = html_path.read_text(encoding="utf-8")
        if "images/" not in html_text:
            continue

        soup = BeautifulSoup(html_text, "html.parser")
        images = cast(list[Tag], soup.find_all("img"))
        for image in images:
            image_src = _tag_attr_str(image, "src")
            if image_src and _src_points_to_legacy_image_dir(
                image_src,
                html_path.parent,
            ):
                ref_count += 1
    return ref_count


def migrate_image_index() -> ImageMigrationResult:
    legacy_dir = image_store.image_links_dir()
    mappings = 0
    broken_links = 0
    unique_path_by_url: dict[str, Path] = {}
    mapping_batch: list[tuple[str, Path]] = []

    def flush_mapping_batch() -> None:
        nonlocal mappings
        if not mapping_batch:
            return
        image_store.upsert_image_mappings(mapping_batch)
        mappings += len(mapping_batch)
        mapping_batch.clear()

    if legacy_dir.is_dir():
        output_root = image_store.output_dir().resolve()
        for link_path in sorted(legacy_dir.rglob("*")):
            if not link_path.is_symlink():
                continue
            image_url = _legacy_image_url_from_link_path(link_path)
            if image_url is None or not link_path.exists():
                broken_links += 1
                continue
            target_path = link_path.resolve()
            try:
                target_path.relative_to(output_root)
            except ValueError:
                broken_links += 1
                continue
            if not target_path.is_file():
                broken_links += 1
                continue
            unique_path_by_url[image_url] = target_path
            mapping_batch.append((image_url, target_path))
            if len(mapping_batch) >= 1000:
                flush_mapping_batch()
        flush_mapping_batch()

    for url, mapping in image_store.image_mappings_by_url().items():
        unique_path = mapping.unique_path
        if unique_path.exists():
            unique_path_by_url.setdefault(url, unique_path)

    html_files = _html_modified_files()
    updated_html_files = 0
    updated_image_refs = 0
    for html_path in html_files:
        updated_refs = _rewrite_html_file_image_refs(html_path, unique_path_by_url)
        if updated_refs:
            updated_html_files += 1
            updated_image_refs += updated_refs

    return ImageMigrationResult(
        mappings=mappings,
        broken_links=broken_links,
        html_files=len(html_files),
        updated_html_files=updated_html_files,
        updated_image_refs=updated_image_refs,
    )


def prune_legacy_image_links() -> ImagePruneResult:
    legacy_ref_count = _count_legacy_html_image_refs()
    if legacy_ref_count:
        raise RuntimeError(
            "仍有html_modified图片引用指向旧软链接目录，"
            f"请先运行 image migrate。引用数：{legacy_ref_count}"
        )

    legacy_dir = image_store.image_links_dir()
    if not legacy_dir.exists():
        return ImagePruneResult(removed_links=0, removed_directory=None)
    if not legacy_dir.is_dir():
        raise RuntimeError(f"旧图片软链接路径不是目录：{legacy_dir}")

    removed_links = 0
    for path in legacy_dir.rglob("*"):
        if path.is_symlink():
            removed_links += 1
            continue
        if path.is_dir():
            continue
        raise RuntimeError(f"旧图片目录包含非软链接文件，拒绝删除：{path}")

    shutil.rmtree(legacy_dir)
    return ImagePruneResult(
        removed_links=removed_links,
        removed_directory=str(legacy_dir),
    )


def _image_path_from_src(image_src: str, source_dir: Path) -> Path:
    link_path = image_store.link_path_for_image_src(image_src)
    if link_path is not None:
        return link_path

    return _path_from_src(image_src, source_dir)


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

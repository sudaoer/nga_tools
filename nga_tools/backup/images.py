from __future__ import annotations

from dataclasses import dataclass
import os
import shutil
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.config import get_config


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


_NGA_IMAGE_BASE_URL = "https://img.nga.178.com/attachments/"


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

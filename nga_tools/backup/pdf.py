from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from PIL import Image

from nga_tools.core.hashing import sha256
from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.core.paths import get_folder
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import (
    FloorLabels,
    load_floor_labels_from_archive,
    validate_floor_labels,
)
from nga_tools.backup import image_store
from nga_tools.backup.html_images import (
    effective_image_src,
    image_src_path,
    tag_attr_str,
)
from nga_tools.backup.post_html import (
    fill_missing_post_records,
    find_missing_lou,
    post_html_from_content,
)
from nga_tools.backup.post_overlay import (
    make_existing_overlay_image_src_resolver,
    render_overlay_html,
)
from nga_tools.backup.pdf_plan import (
    PdfRenderTask,
    build_render_tasks as _build_render_tasks,
    pdf_file_is_usable,
    write_pdf_hashes as _write_pdf_hashes,
)
from nga_tools.config import get_config
from nga_tools.console import WarningCategory, report_info, report_warning
from nga_tools.core.atomic import replace_temp_file, temporary_sibling_path
from nga_tools.core.image_formats import (
    image_extension_from_file,
    open_image_for_processing,
)
from nga_tools.timing import time_section

SPEAKER_LINE_RE = re.compile(r"^([^\s：:][^：:]{0,15})[：:]")
PDF_CONVERTED_IMAGE_DIRNAME = "converted_images"
_PDF_CONVERTED_IMAGE_EXTENSIONS = {"avif", "jxl"}


@dataclass(frozen=True)
class PdfHtmlSource:
    html: str
    source_name: str
    source_dir: Path


@dataclass(frozen=True)
class PdfRenderResult:
    task: PdfRenderTask
    returncode: int
    output_lines: tuple[str, ...]


class PdfRenderPool:
    def __init__(self, pdf_workers: Optional[int]) -> None:
        if pdf_workers is not None and pdf_workers <= 0:
            raise ValueError("--pdf_workers必须大于0。")
        self._pdf_workers = pdf_workers
        self._executor: concurrent.futures.ProcessPoolExecutor | None = None
        self._lock = threading.Lock()
        self._closed = False

    @property
    def pdf_workers(self) -> Optional[int]:
        return self._pdf_workers

    @property
    def worker_desc(self) -> str:
        return _pdf_worker_desc(self._pdf_workers)

    def _ensure_executor(self) -> concurrent.futures.ProcessPoolExecutor:
        with self._lock:
            if self._closed:
                raise RuntimeError("PDF渲染池已关闭。")
            if self._executor is None:
                self._executor = concurrent.futures.ProcessPoolExecutor(
                    max_workers=self._pdf_workers
                )
            return self._executor

    def render(self, tasks: list[PdfRenderTask]) -> list[PdfRenderResult]:
        if not tasks:
            return []
        executor = self._ensure_executor()
        return list(executor.map(_run_pdf_renderer, tasks))

    def close(self) -> None:
        executor: concurrent.futures.ProcessPoolExecutor | None = None
        with self._lock:
            if self._closed:
                return
            self._closed = True
            executor = self._executor
            self._executor = None

        if executor is not None:
            executor.shutdown()

    def __enter__(self) -> "PdfRenderPool":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.close()


def _normalize_img_classes(img: Tag) -> list[str]:
    classes = img.get("class")
    if isinstance(classes, str):
        return [classes]
    if isinstance(classes, list):
        return list(classes)
    return []


def _get_following_visible_text(node: Tag, max_chars: int = 48) -> str:
    for sibling in node.next_siblings:
        if isinstance(sibling, NavigableString):
            text = str(sibling).strip()
        elif isinstance(sibling, Tag):
            if sibling.name == "br":
                continue
            text = sibling.get_text(" ", strip=True)
        else:
            text = ""

        if text:
            return text[:max_chars]
    return ""


def _remove_leading_breaks_after(node: Tag) -> None:
    for sibling in list(node.next_siblings):
        if isinstance(sibling, NavigableString):
            if str(sibling).strip():
                return
            continue
        if isinstance(sibling, Tag) and sibling.name == "br":
            sibling.decompose()
            continue
        return


def _get_image_size(
    image_path: str,
    image_size_cache: dict[str, tuple[int, int]],
) -> tuple[int, int]:
    if image_path not in image_size_cache:
        image = open_image_for_processing(Path(image_path))
        try:
            image_size_cache[image_path] = image.size
        finally:
            image.close()
    return image_size_cache[image_path]


def _looks_like_speaker_name(speaker_name: str) -> bool:
    trimmed_name = speaker_name.strip().strip('"\'“”‘’')
    if not trimmed_name or trimmed_name.startswith(("[", "<")):
        return False

    if set(trimmed_name) <= {"?", "？", "!", "！"}:
        return True

    canonical_name = re.sub(r"[（(][^）)]*[）)]", "", trimmed_name)
    canonical_name = canonical_name.replace("/", "").replace("／", "").strip()
    if not canonical_name:
        return False

    if any("\u4e00" <= char <= "\u9fff" for char in canonical_name):
        return True

    if canonical_name.isascii():
        return not canonical_name.isupper()

    return True


def _is_speaker_portrait(img: Tag, width: int, height: int) -> bool:
    app_config = get_config()
    max_dimension = app_config.pdf_speaker_portrait_max_dimension
    max_aspect_ratio = app_config.pdf_speaker_portrait_max_ratio
    aspect_ratio = height / max(width, 1)
    if max(width, height) > max_dimension or not 0.45 <= aspect_ratio <= max_aspect_ratio:
        return False

    following_text = _get_following_visible_text(img)
    match = SPEAKER_LINE_RE.match(following_text)
    if not match:
        return False

    return _looks_like_speaker_name(match.group(1))


def _is_long_image(width: int, height: int) -> bool:
    app_config = get_config()
    min_width = app_config.pdf_long_image_min_width
    min_ratio = app_config.pdf_long_image_min_ratio
    return width >= min_width and (height / max(width, 1)) >= min_ratio


def _save_slice_image(image: Image.Image, output_path: str) -> None:
    final_path = Path(output_path)
    temp_path = temporary_sibling_path(final_path)
    try:
        if "A" in image.getbands():
            image.save(temp_path, format="PNG", optimize=True)
        else:
            image.convert("RGB").save(
                temp_path,
                format="JPEG",
                quality=92,
                optimize=True,
            )
        replace_temp_file(temp_path, final_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise


def _slice_image_file_is_valid(path: str) -> bool:
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError, SyntaxError):
        return False
    return True


def _slice_long_image_for_pdf(
    image_path: str,
    slice_output_dir: str,
    slice_cache: dict[str, list[str]],
) -> list[str]:
    if image_path in slice_cache:
        return slice_cache[image_path]

    max_slice_ratio = get_config().pdf_long_image_slice_ratio
    safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", Path(image_path).stem)
    slice_paths: list[str] = []

    image = open_image_for_processing(Path(image_path))
    try:
        width, height = image.size
        slice_height = max(1, int(width * max_slice_ratio))
        if height <= slice_height:
            slice_cache[image_path] = []
            return []

        for index, start in enumerate(range(0, height, slice_height)):
            end = min(height, start + slice_height)
            segment = image.crop((0, start, width, end))
            extension = ".png" if "A" in segment.getbands() else ".jpg"
            output_path = os.path.join(
                slice_output_dir,
                f"{safe_stem}_slice_{index:03d}{extension}",
            )
            if not os.path.exists(output_path) or not _slice_image_file_is_valid(
                output_path
            ):
                _save_slice_image(segment, output_path)
            slice_paths.append(output_path)
    finally:
        image.close()

    slice_cache[image_path] = slice_paths
    return slice_paths


def _image_needs_pdf_conversion(image_path: Path) -> bool:
    return image_extension_from_file(image_path) in _PDF_CONVERTED_IMAGE_EXTENSIONS


def _converted_pdf_image_path(image_path: Path, folder_pdf: str) -> Path:
    image = open_image_for_processing(image_path)
    try:
        extension = "png" if "A" in image.getbands() else "jpg"
        converted_dir = Path(folder_pdf) / PDF_CONVERTED_IMAGE_DIRNAME
        converted_dir.mkdir(parents=True, exist_ok=True)
        converted_path = converted_dir / f"{sha256(str(image_path))}.{extension}"
        if converted_path.exists() and _slice_image_file_is_valid(str(converted_path)):
            return converted_path
        _save_slice_image(image, str(converted_path))
        return converted_path
    finally:
        image.close()


def _prepare_image_for_pdf(
    image_path: Path,
    folder_pdf: str,
    converted_image_cache: dict[str, Path],
) -> Path:
    if not _image_needs_pdf_conversion(image_path):
        return image_path

    cache_key = str(image_path)
    converted_path = converted_image_cache.get(cache_key)
    if converted_path is None:
        converted_path = _converted_pdf_image_path(image_path, folder_pdf)
        converted_image_cache[cache_key] = converted_path
    return converted_path


def _relative_dir_path(from_dir: str, to_path: str) -> str:
    return os.path.relpath(to_path, from_dir).replace("\\", "/")


def _replace_long_image_with_slices(
    soup: BeautifulSoup,
    img: Tag,
    slice_paths: list[str],
    html_dir: str,
) -> None:
    wrapper = soup.new_tag("div")
    wrapper["class"] = "long-image-slices"
    alt_text = tag_attr_str(img, "alt") or ""
    for slice_path in slice_paths:
        slice_img = soup.new_tag("img")
        slice_img["src"] = _relative_dir_path(html_dir, slice_path)
        slice_img["alt"] = alt_text
        slice_img["class"] = "long-image-slice"
        wrapper.append(slice_img)
    img.replace_with(wrapper)


def _split_weasyprint_output(output: str | None) -> tuple[str, ...]:
    if not output:
        return ()
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _pdf_worker_desc(pdf_workers: Optional[int]) -> str:
    if pdf_workers is None:
        return "默认"
    return str(pdf_workers)


def _run_pdf_renderer(task: PdfRenderTask) -> PdfRenderResult:
    output_path = Path(task.output_path)
    temp_output_path = temporary_sibling_path(output_path)
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "nga_tools.backup.pdf_renderer",
                task.html_path,
                str(temp_output_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        temp_output_path.unlink(missing_ok=True)
        return PdfRenderResult(
            task=task,
            returncode=1,
            output_lines=(f"无法启动PDF渲染子进程，请确认通过pixi环境运行：{error}",),
        )
    output_lines = _split_weasyprint_output(result.stdout)
    if result.returncode != 0:
        temp_output_path.unlink(missing_ok=True)
        return PdfRenderResult(
            task=task,
            returncode=result.returncode,
            output_lines=output_lines,
        )
    if not pdf_file_is_usable(temp_output_path):
        temp_output_path.unlink(missing_ok=True)
        return PdfRenderResult(
            task=task,
            returncode=1,
            output_lines=(*output_lines, f"PDF输出无效：{task.output_path}"),
        )
    try:
        replace_temp_file(temp_output_path, output_path)
    except OSError as error:
        temp_output_path.unlink(missing_ok=True)
        return PdfRenderResult(
            task=task,
            returncode=1,
            output_lines=(*output_lines, f"无法写入PDF：{task.output_path}: {error}"),
        )
    return PdfRenderResult(
        task=task,
        returncode=result.returncode,
        output_lines=output_lines,
    )


def _report_weasyprint_output(render_results: list[PdfRenderResult]) -> None:
    for render_result in render_results:
        pdf_name = Path(render_result.task.output_path).name
        for line in render_result.output_lines:
            report_warning(
                WarningCategory.PDF,
                f"WeasyPrint {pdf_name}: {line}",
            )


def _image_path_for_pdf(
    image_src: str,
    source_dir: Path,
    image_lookup: image_store.ImageLookupCache | None = None,
) -> Path:
    normalized_src = image_store.normalize_nga_image_url(image_src)
    if NGA_img_link_verify(normalized_src):
        lookup = (
            image_store.ImageLookupCache.for_urls([normalized_src])
            if image_lookup is None
            else image_lookup
        )
        image_path = lookup.mapped_image_path_for_url(normalized_src)
        return (
            image_store.placeholder_image_path()
            if image_path is None
            else image_path
        )

    path = image_src_path(image_src, source_dir)
    if path is None:
        raise RuntimeError(f"不支持的图片链接：{image_src}")

    return path


def _read_pdf_html(
    tid: int,
    aid: Optional[int],
) -> tuple[dict[int, str], str, FloorLabels]:
    thread_folder = Path(get_folder(tid, aid, create=False))
    archive_store = ThreadArchiveStore(thread_folder)
    records = archive_store.read_effective_post_records()
    floor_labels = load_floor_labels_from_archive(archive_store, aid)
    author_total_lou_count = (
        archive_store.read_latest_author_total_lou_count()
        if aid is not None
        else None
    )
    missing_lous = find_missing_lou(records, author_total_lou_count)
    present_lous = {record["lou"] for record in records}
    fill_missing_post_records(
        records,
        [lou for lou in missing_lous if lou not in present_lous],
        floor_labels,
    )

    folder_pdf = get_folder(tid, aid, "pdf")
    slice_output_dir = os.path.join(folder_pdf, "long_image_slices")
    os.makedirs(slice_output_dir, exist_ok=True)

    overlays_by_lou = archive_store.read_post_overlays()
    applied_overlay_lous = set(overlays_by_lou) & {
        record["lou"] for record in records
    }
    if applied_overlay_lous:
        report_info(f"应用{len(applied_overlay_lous)}个BBCode post overlay。")

    html_sources_by_lou: dict[int, PdfHtmlSource] = {}
    for record in records:
        lou = record["lou"]
        overlay = overlays_by_lou.get(lou)
        if overlay is not None:
            image_src_resolver = make_existing_overlay_image_src_resolver(
                overlay["bbcode"],
                thread_folder.parent,
                image_src_from_path=lambda _url, image_path: os.path.relpath(
                    image_path,
                    thread_folder,
                ).replace("\\", "/"),
            )
            html = render_overlay_html(
                overlay["bbcode"],
                image_src_resolver=image_src_resolver,
            )
            source_name = f"archive.sqlite3 post_overlays第{lou}楼"
        elif record["html"] is not None:
            html = record["html"]
            source_name = f"archive缺失占位第{lou}楼"
        else:
            post = record["post"]
            if post is None:
                raise RuntimeError(f"archive缺少第{lou}楼的可转换正文。")
            html = post_html_from_content(post)
            source_name = f"archive.sqlite3第{lou}楼"
        html_sources_by_lou[lou] = PdfHtmlSource(
            html=html,
            source_name=source_name,
            source_dir=thread_folder,
        )

    html_text_by_lou = {
        lou: source.html for lou, source in html_sources_by_lou.items()
    }
    validate_floor_labels(floor_labels, html_text_by_lou)

    html_content_by_lou: dict[int, str] = {}
    image_size_cache: dict[str, tuple[int, int]] = {}
    slice_cache: dict[str, list[str]] = {}
    converted_image_cache: dict[str, Path] = {}
    soups_by_lou: dict[int, BeautifulSoup] = {}
    image_urls: set[str] = set()

    for lou, source in html_sources_by_lou.items():
        soup = BeautifulSoup(source.html, "html.parser")
        soups_by_lou[lou] = soup
        for image in cast(list[Tag], soup.find_all("img")):
            image_src = effective_image_src(image)
            if image_src is not None and NGA_img_link_verify(image_src):
                image_urls.add(image_src)
    image_lookup = image_store.ImageLookupCache.for_urls(image_urls)

    for lou, source in sorted(html_sources_by_lou.items()):
        source_desc = f"{source.source_name}（{floor_labels.label(lou)}）"
        soup = soups_by_lou[lou]

        images = cast(list[Tag], soup.find_all("img"))
        for image in images:
            image_src = effective_image_src(image)
            if image_src is None:
                continue

            image_path = _image_path_for_pdf(
                image_src,
                source.source_dir,
                image_lookup,
            )
            if not image_path.exists():
                raise RuntimeError(
                    f"正文{source_desc}中引用了不存在的图片文件{image_src}！"
                )

            try:
                image_path = _prepare_image_for_pdf(
                    image_path,
                    folder_pdf,
                    converted_image_cache,
                )
            except Exception as error:
                report_warning(
                    WarningCategory.PDF,
                    f"{floor_labels.label(lou)}跳过PDF图片转换 "
                    f"{image_path}: {error}"
                )

            image["src"] = _relative_dir_path(folder_pdf, str(image_path))

            try:
                width, height = _get_image_size(str(image_path), image_size_cache)
            except (OSError, SyntaxError, ValueError) as error:
                report_warning(
                    WarningCategory.PDF,
                    f"{floor_labels.label(lou)}跳过无法识别尺寸的图片 "
                    f"{image_path}: {error}"
                )
                continue

            if _is_long_image(width, height):
                slice_paths = _slice_long_image_for_pdf(
                    str(image_path),
                    slice_output_dir,
                    slice_cache,
                )
                if slice_paths:
                    _replace_long_image_with_slices(soup, image, slice_paths, folder_pdf)
                    continue

            if _is_speaker_portrait(image, width, height):
                image_classes = _normalize_img_classes(image)
                if "speaker-portrait" not in image_classes:
                    image_classes.append("speaker-portrait")
                    image["class"] = " ".join(image_classes)
                _remove_leading_breaks_after(image)

        html_content_by_lou[lou] = str(soup).replace("&amp;#", "&#")

    return html_content_by_lou, folder_pdf, floor_labels


def generate_pdf(
    tid: int,
    aid: Optional[int],
    lou_per_pdf: int,
    pdf_workers: Optional[int],
    *,
    pdf_renderer: PdfRenderPool | None = None,
) -> None:
    if lou_per_pdf <= 0:
        raise ValueError("--lou_per_pdf必须大于0。")
    if pdf_workers is not None and pdf_workers <= 0:
        raise ValueError("--pdf_workers必须大于0。")

    with time_section("PDF读取与规划"):
        html_content_by_lou, folder_pdf, floor_labels = _read_pdf_html(tid, aid)
        render_plan = _build_render_tasks(
            html_content_by_lou,
            folder_pdf,
            lou_per_pdf,
            floor_labels,
        )

    render_tasks = render_plan.render_tasks
    worker_desc = (
        pdf_renderer.worker_desc
        if pdf_renderer is not None
        else _pdf_worker_desc(pdf_workers)
    )
    if render_plan.skipped_count:
        report_info(f"跳过{render_plan.skipped_count}个输入HTML未变化的PDF。")
    if render_plan.cleaned_count:
        report_info(f"删除{render_plan.cleaned_count}个旧PDF分段文件。")

    with time_section("PDF渲染"):
        report_info(f"开始生成{len(render_tasks)}个PDF，worker数量：{worker_desc}")
        if pdf_renderer is None:
            with PdfRenderPool(pdf_workers) as render_pool:
                render_results = render_pool.render(render_tasks)
        else:
            render_results = pdf_renderer.render(render_tasks)

        _report_weasyprint_output(render_results)
        failed_count = sum(
            render_result.returncode != 0 for render_result in render_results
        )
        if failed_count:
            raise RuntimeError(f"{failed_count}个PDF生成任务失败。")

    with time_section("PDF缓存写入"):
        _write_pdf_hashes(folder_pdf, render_plan.input_hashes)
    report_info("PDF生成完成。")

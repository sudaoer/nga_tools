from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from bs4 import BeautifulSoup, Tag
from bs4.element import NavigableString
from PIL import Image

from nga_tools import utils
from nga_tools.backup.floor_map import (
    FloorLabels,
    load_floor_labels,
    validate_floor_labels,
)
from nga_tools.backup import image_store
from nga_tools.backup.overlay import load_post_overlays
from nga_tools.backup.pdf_plan import (
    PdfRenderTask,
    build_render_tasks as _build_render_tasks,
    write_pdf_hashes as _write_pdf_hashes,
)
from nga_tools.config import get_config
from nga_tools.console import report_info, report_warning

SPEAKER_LINE_RE = re.compile(r"^([^\s：:][^：:]{0,15})[：:]")


@dataclass(frozen=True)
class PdfHtmlSource:
    html: str
    source_name: str
    source_dir: Path


def _tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


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
        with Image.open(image_path) as image:
            image_size_cache[image_path] = image.size
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
    if "A" in image.getbands():
        image.save(output_path, format="PNG", optimize=True)
        return

    image.convert("RGB").save(output_path, format="JPEG", quality=92, optimize=True)


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

    with Image.open(image_path) as image:
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
            if not os.path.exists(output_path):
                _save_slice_image(segment, output_path)
            slice_paths.append(output_path)

    slice_cache[image_path] = slice_paths
    return slice_paths


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
    alt_text = _tag_attr_str(img, "alt") or ""
    for slice_path in slice_paths:
        slice_img = soup.new_tag("img")
        slice_img["src"] = _relative_dir_path(html_dir, slice_path)
        slice_img["alt"] = alt_text
        slice_img["class"] = "long-image-slice"
        wrapper.append(slice_img)
    img.replace_with(wrapper)


def _run_weasyprint(task: PdfRenderTask) -> int:
    result = subprocess.run(
        ["weasyprint", task.html_path, task.output_path],
        check=False,
    )
    return result.returncode


def _image_path_for_pdf(image_src: str, source_dir: Path) -> Path:
    link_path = image_store.link_path_for_image_src(image_src)
    if link_path is not None:
        return link_path

    parsed_src = image_src.split("?", 1)[0]
    src_parts = parsed_src.split(":", 1)
    if len(src_parts) == 2 and src_parts[0].isalpha():
        raise RuntimeError(f"不支持的远程图片链接：{image_src}")

    path = Path(parsed_src)
    if path.is_absolute():
        return path
    return source_dir / path


def _read_pdf_html(
    tid: int,
    aid: Optional[int],
) -> tuple[dict[int, str], str, FloorLabels]:
    folder_html_modified = Path(utils.get_folder(tid, aid, "html_modified"))
    html_files = utils.list_files_in_folder(str(folder_html_modified), ends_with=".html")
    folder_pdf = utils.get_folder(tid, aid, "pdf")
    slice_output_dir = os.path.join(folder_pdf, "long_image_slices")
    os.makedirs(slice_output_dir, exist_ok=True)

    html_sources_by_lou: dict[int, PdfHtmlSource] = {}
    for html_file in html_files:
        html_path = folder_html_modified / html_file
        lou = int(html_file.split("_")[1].split(".")[0])
        html_sources_by_lou[lou] = PdfHtmlSource(
            html=html_path.read_text(encoding="utf-8"),
            source_name=f"html_modified/post_{lou}.html",
            source_dir=folder_html_modified,
        )

    floor_labels = load_floor_labels(tid, aid)
    html_text_by_lou = {
        lou: source.html for lou, source in html_sources_by_lou.items()
    }
    validate_floor_labels(floor_labels, html_text_by_lou)

    overlays_by_lou = load_post_overlays(
        tid,
        aid,
        set(html_sources_by_lou),
        floor_labels,
    )
    if overlays_by_lou:
        report_info(f"应用{len(overlays_by_lou)}个post overlay。")
        overlay_folder = Path(utils.get_folder(tid, aid, "overlay"))
        for lou, overlay_html in overlays_by_lou.items():
            html_sources_by_lou[lou] = PdfHtmlSource(
                html=overlay_html,
                source_name=f"overlay/post_{lou}.html",
                source_dir=overlay_folder,
            )

    html_content_by_lou: dict[int, str] = {}
    image_size_cache: dict[str, tuple[int, int]] = {}
    slice_cache: dict[str, list[str]] = {}

    for lou, source in sorted(html_sources_by_lou.items()):
        source_desc = f"{source.source_name}（{floor_labels.label(lou)}）"
        soup = BeautifulSoup(source.html, "html.parser")

        images = cast(list[Tag], soup.find_all("img"))
        for image in images:
            image_src = _tag_attr_str(image, "src")
            if not image_src:
                continue

            image_path = _image_path_for_pdf(image_src, source.source_dir)
            if not image_path.exists():
                raise RuntimeError(
                    f"HTML文件{source_desc}中引用了不存在的图片文件{image_src}！"
                )

            image["src"] = _relative_dir_path(folder_pdf, str(image_path))

            try:
                width, height = _get_image_size(str(image_path), image_size_cache)
            except OSError as error:
                report_warning(
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
) -> None:
    if lou_per_pdf <= 0:
        raise ValueError("--lou_per_pdf必须大于0。")
    if pdf_workers is not None and pdf_workers <= 0:
        raise ValueError("--pdf_workers必须大于0。")

    html_content_by_lou, folder_pdf, floor_labels = _read_pdf_html(tid, aid)
    render_plan = _build_render_tasks(
        html_content_by_lou,
        folder_pdf,
        lou_per_pdf,
        floor_labels,
    )

    render_tasks = render_plan.render_tasks
    worker_desc = pdf_workers if pdf_workers is not None else "默认"
    if render_plan.skipped_count:
        report_info(f"跳过{render_plan.skipped_count}个输入HTML未变化的PDF。")
    if render_plan.cleaned_count:
        report_info(f"删除{render_plan.cleaned_count}个旧PDF分段文件。")

    report_info(f"开始生成{len(render_tasks)}个PDF，worker数量：{worker_desc}")
    if render_tasks:
        with concurrent.futures.ProcessPoolExecutor(
            max_workers=pdf_workers
        ) as executor:
            exit_codes = list(executor.map(_run_weasyprint, render_tasks))
    else:
        exit_codes = []

    failed_count = sum(exit_code != 0 for exit_code in exit_codes)
    if failed_count:
        raise RuntimeError(f"{failed_count}个PDF生成任务失败。")
    _write_pdf_hashes(folder_pdf, render_plan.input_hashes)
    report_info("PDF生成完成。")

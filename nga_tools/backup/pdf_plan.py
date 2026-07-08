from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict, cast

from nga_tools.backup.floor_map import FloorLabels
from nga_tools.config import get_config
from nga_tools.console import report_warning

PDF_HASH_MANIFEST_FILENAME = "pdf_input_hashes.json"
PDF_HASH_MANIFEST_VERSION = 1
PDF_HASH_ALGORITHM = "sha256"
PDF_PART_ARTIFACT_RE = re.compile(r"^part_\d+_\d+\.(?:html|pdf)$")


class PdfHashManifest(TypedDict):
    version: int
    algorithm: str
    files: dict[str, str]


@dataclass(frozen=True)
class PdfRenderTask:
    html_path: str
    output_path: str


@dataclass(frozen=True)
class PdfRenderPlan:
    render_tasks: list[PdfRenderTask]
    skipped_count: int
    cleaned_count: int
    input_hashes: dict[str, str]


def pdf_hash_manifest_path(folder_pdf: str) -> Path:
    return Path(folder_pdf) / PDF_HASH_MANIFEST_FILENAME


def sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_pdf_hashes(folder_pdf: str) -> dict[str, str]:
    manifest_path = pdf_hash_manifest_path(folder_pdf)
    try:
        raw_data: object = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        report_warning(f"PDF hash缓存文件无效，按空缓存处理：{manifest_path}: {error}")
        return {}

    if not isinstance(raw_data, dict):
        report_warning(f"PDF hash缓存文件格式无效，按空缓存处理：{manifest_path}")
        return {}

    data = cast(dict[object, object], raw_data)
    if data.get("version") != PDF_HASH_MANIFEST_VERSION:
        return {}
    if data.get("algorithm") != PDF_HASH_ALGORITHM:
        return {}

    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        report_warning(f"PDF hash缓存文件缺少files对象，按空缓存处理：{manifest_path}")
        return {}

    files = cast(dict[object, object], raw_files)
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in files.items()
    ):
        report_warning(f"PDF hash缓存文件files格式无效，按空缓存处理：{manifest_path}")
        return {}

    return cast(dict[str, str], files)


def write_pdf_hashes(folder_pdf: str, input_hashes: dict[str, str]) -> None:
    manifest: PdfHashManifest = {
        "version": PDF_HASH_MANIFEST_VERSION,
        "algorithm": PDF_HASH_ALGORITHM,
        "files": dict(sorted(input_hashes.items())),
    }
    manifest_path = pdf_hash_manifest_path(folder_pdf)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def render_pdf_html(
    html_content_by_lou: dict[int, str],
    start_lou: int,
    end_lou: int,
    floor_labels: FloorLabels,
) -> str:
    app_config = get_config()
    parts = [
        "<html lang=\"zh-CN\">\n<head>\n<meta charset=\"utf-8\"/>\n",
        app_config.html_style,
        "\n</head>\n<body>\n",
        app_config.html_pre,
    ]
    for lou in range(start_lou, end_lou + 1):
        if lou in html_content_by_lou:
            parts.append(f"<h2>{floor_labels.label(lou)}</h2>\n")
            parts.append(html_content_by_lou[lou])
            parts.append("<hr/>\n")
    parts.append(app_config.html_post)
    parts.append("\n</body>\n</html>\n")
    return "".join(parts)


def write_text_if_changed(path: str, content: str) -> None:
    output_path = Path(path)
    try:
        if output_path.read_text(encoding="utf-8") == content:
            return
    except FileNotFoundError:
        pass

    output_path.write_text(content, encoding="utf-8")


def cleanup_stale_pdf_parts(folder_pdf: str, expected_filenames: set[str]) -> int:
    cleaned_count = 0
    for path in Path(folder_pdf).iterdir():
        if not path.is_file():
            continue
        if not PDF_PART_ARTIFACT_RE.fullmatch(path.name):
            continue
        if path.name in expected_filenames:
            continue

        try:
            path.unlink()
        except OSError as error:
            raise RuntimeError(f"无法删除旧PDF分段文件：{path}: {error}") from error
        cleaned_count += 1

    return cleaned_count


def build_render_tasks(
    html_content_by_lou: dict[int, str],
    folder_pdf: str,
    lou_per_pdf: int,
    floor_labels: FloorLabels,
) -> PdfRenderPlan:
    render_tasks: list[PdfRenderTask] = []
    folder_pdf_path = Path(folder_pdf)
    cached_hashes = load_pdf_hashes(folder_pdf)
    input_hashes: dict[str, str] = {}
    expected_filenames: set[str] = set()
    skipped_count = 0

    for index in range(1, len(html_content_by_lou) // lou_per_pdf + 2):
        start_lou = (index - 1) * lou_per_pdf
        end_lou = min(index * lou_per_pdf - 1, len(html_content_by_lou))
        if start_lou > end_lou:
            break

        pdf_html_path = folder_pdf_path / f"part_{start_lou}_{end_lou}.html"
        pdf_output_path = folder_pdf_path / f"part_{start_lou}_{end_lou}.pdf"
        html_filename = pdf_html_path.name
        pdf_filename = pdf_output_path.name
        expected_filenames.update({html_filename, pdf_filename})
        pdf_html = render_pdf_html(
            html_content_by_lou,
            start_lou,
            end_lou,
            floor_labels,
        )
        html_hash = sha256_text(pdf_html)
        input_hashes[html_filename] = html_hash

        if cached_hashes.get(html_filename) == html_hash and pdf_output_path.exists():
            write_text_if_changed(str(pdf_html_path), pdf_html)
            skipped_count += 1
            continue

        pdf_html_path.write_text(pdf_html, encoding="utf-8")
        render_tasks.append(PdfRenderTask(str(pdf_html_path), str(pdf_output_path)))

    cleaned_count = cleanup_stale_pdf_parts(folder_pdf, expected_filenames)
    return PdfRenderPlan(render_tasks, skipped_count, cleaned_count, input_hashes)

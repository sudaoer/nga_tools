from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict, cast

from nga_tools.console import report_warning

HTML_MANIFEST_FILENAME = "html_manifest.json"
HTML_MANIFEST_VERSION = 1
HTML_GENERATION_VERSION = 1
HTML_HASH_ALGORITHM = "sha256"


class HtmlManifestEntry(TypedDict):
    source_hash: str
    output_hash: str


class HtmlManifest(TypedDict):
    version: int
    html_generation_version: int
    algorithm: str
    files: dict[str, HtmlManifestEntry]


def post_html_filename(lou: int) -> str:
    return f"post_{lou}.html"


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def hash_object(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hash_text(payload)


def manifest_path(folder_html: Path) -> Path:
    return folder_html / HTML_MANIFEST_FILENAME


def load_manifest(folder_html: Path) -> dict[str, HtmlManifestEntry]:
    path = manifest_path(folder_html)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        report_warning(f"html缓存文件无效，按空缓存处理：{path}: {error}")
        return {}

    if not isinstance(raw_data, dict):
        report_warning(f"html缓存文件格式无效，按空缓存处理：{path}")
        return {}

    data = cast(dict[object, object], raw_data)
    if data.get("version") != HTML_MANIFEST_VERSION:
        return {}
    if data.get("html_generation_version") != HTML_GENERATION_VERSION:
        return {}
    if data.get("algorithm") != HTML_HASH_ALGORITHM:
        return {}

    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        report_warning(f"html缓存文件缺少files对象，按空缓存处理：{path}")
        return {}

    files: dict[str, HtmlManifestEntry] = {}
    for raw_filename, raw_entry in cast(dict[object, object], raw_files).items():
        if not isinstance(raw_filename, str) or not isinstance(raw_entry, dict):
            report_warning(f"html缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        entry = cast(dict[object, object], raw_entry)
        source_hash = entry.get("source_hash")
        output_hash = entry.get("output_hash")
        if not isinstance(source_hash, str) or not isinstance(output_hash, str):
            report_warning(f"html缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        files[raw_filename] = {
            "source_hash": source_hash,
            "output_hash": output_hash,
        }
    return files


def manifest_files_exist(
    folder_html: Path,
    entries: dict[str, HtmlManifestEntry],
) -> bool:
    return all((folder_html / filename).is_file() for filename in entries)


def write_manifest(
    folder_html: Path,
    entries: dict[str, HtmlManifestEntry],
) -> None:
    manifest: HtmlManifest = {
        "version": HTML_MANIFEST_VERSION,
        "html_generation_version": HTML_GENERATION_VERSION,
        "algorithm": HTML_HASH_ALGORITHM,
        "files": dict(sorted(entries.items())),
    }
    path = manifest_path(folder_html)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)

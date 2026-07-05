from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import TypedDict, cast

HTML_MODIFIED_MANIFEST_FILENAME = "html_modified_manifest.json"
HTML_MODIFIED_MANIFEST_VERSION = 1
HTML_MODIFIED_GENERATION_VERSION = 1
HTML_MODIFIED_HASH_ALGORITHM = "sha256"


class HtmlModifiedManifestEntry(TypedDict):
    source_hash: str
    output_hash: str


class HtmlModifiedManifest(TypedDict):
    version: int
    modified_generation_version: int
    algorithm: str
    files: dict[str, HtmlModifiedManifestEntry]


def post_html_filename(lou: int) -> str:
    return f"post_{lou}.html"


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def manifest_path(folder_html_modified: Path) -> Path:
    return folder_html_modified / HTML_MODIFIED_MANIFEST_FILENAME


def load_manifest(folder_html_modified: Path) -> dict[str, HtmlModifiedManifestEntry]:
    path = manifest_path(folder_html_modified)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        print(f"警告：html_modified缓存文件无效，按空缓存处理：{path}: {error}")
        return {}

    if not isinstance(raw_data, dict):
        print(f"警告：html_modified缓存文件格式无效，按空缓存处理：{path}")
        return {}

    data = cast(dict[object, object], raw_data)
    if data.get("version") != HTML_MODIFIED_MANIFEST_VERSION:
        return {}
    if data.get("modified_generation_version") != HTML_MODIFIED_GENERATION_VERSION:
        return {}
    if data.get("algorithm") != HTML_MODIFIED_HASH_ALGORITHM:
        return {}

    raw_files = data.get("files")
    if not isinstance(raw_files, dict):
        print(f"警告：html_modified缓存文件缺少files对象，按空缓存处理：{path}")
        return {}

    files: dict[str, HtmlModifiedManifestEntry] = {}
    for raw_filename, raw_entry in cast(dict[object, object], raw_files).items():
        if not isinstance(raw_filename, str) or not isinstance(raw_entry, dict):
            print(f"警告：html_modified缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        entry = cast(dict[object, object], raw_entry)
        source_hash = entry.get("source_hash")
        output_hash = entry.get("output_hash")
        if not isinstance(source_hash, str) or not isinstance(output_hash, str):
            print(f"警告：html_modified缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        files[raw_filename] = {
            "source_hash": source_hash,
            "output_hash": output_hash,
        }
    return files


def completed_post_lous(
    folder_html_modified: Path,
    source_hash_by_lou: dict[int, str],
    entries: dict[str, HtmlModifiedManifestEntry],
) -> set[int]:
    completed_lous: set[int] = set()
    for lou, source_hash in source_hash_by_lou.items():
        filename = post_html_filename(lou)
        entry = entries.get(filename)
        if entry is None:
            continue
        if entry["source_hash"] != source_hash:
            continue
        if not (folder_html_modified / filename).is_file():
            continue
        completed_lous.add(lou)
    return completed_lous


def manifest_files_exist(
    folder_html_modified: Path,
    entries: dict[str, HtmlModifiedManifestEntry],
) -> bool:
    return all((folder_html_modified / filename).is_file() for filename in entries)


def write_manifest(
    folder_html_modified: Path,
    entries: dict[str, HtmlModifiedManifestEntry],
) -> None:
    manifest: HtmlModifiedManifest = {
        "version": HTML_MODIFIED_MANIFEST_VERSION,
        "modified_generation_version": HTML_MODIFIED_GENERATION_VERSION,
        "algorithm": HTML_MODIFIED_HASH_ALGORITHM,
        "files": dict(sorted(entries.items())),
    }
    path = manifest_path(folder_html_modified)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)


def write_updated_manifest(
    folder_html_modified: Path,
    *,
    previous_entries: dict[str, HtmlModifiedManifestEntry],
    source_hash_by_lou: dict[int, str],
    skipped_lous: set[int],
    completed_lous: set[int],
    output_hash_by_lou: dict[int, str],
) -> None:
    entries: dict[str, HtmlModifiedManifestEntry] = {}
    for lou in sorted(source_hash_by_lou):
        filename = post_html_filename(lou)
        if lou in skipped_lous:
            previous_entry = previous_entries.get(filename)
            if previous_entry is not None:
                entries[filename] = previous_entry
            continue
        if lou not in completed_lous:
            continue
        output_hash = output_hash_by_lou.get(lou)
        if output_hash is None:
            continue
        entries[filename] = {
            "source_hash": source_hash_by_lou[lou],
            "output_hash": output_hash,
        }

    write_manifest(folder_html_modified, entries)

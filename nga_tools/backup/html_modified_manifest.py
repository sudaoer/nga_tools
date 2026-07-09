from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

from nga_tools.core.hashing import hash_text as _hash_text
from nga_tools.core.atomic import write_json_atomically
from nga_tools.console import report_warning

HTML_MODIFIED_MANIFEST_FILENAME = "html_modified_manifest.json"
HTML_MODIFIED_MANIFEST_VERSION = 1
HTML_MODIFIED_GENERATION_VERSION = 2
HTML_MODIFIED_HASH_ALGORITHM = "sha256"


class RequiredHtmlModifiedManifestEntry(TypedDict):
    source_hash: str
    output_hash: str


class HtmlModifiedManifestEntry(RequiredHtmlModifiedManifestEntry, total=False):
    output_size: int
    output_mtime_ns: int


class HtmlModifiedManifest(TypedDict):
    version: int
    modified_generation_version: int
    algorithm: str
    files: dict[str, HtmlModifiedManifestEntry]


def post_html_filename(lou: int) -> str:
    return f"post_{lou}.html"


def hash_text(text: str) -> str:
    return _hash_text(text)


def manifest_path(folder_html_modified: Path) -> Path:
    return folder_html_modified / HTML_MODIFIED_MANIFEST_FILENAME


def load_manifest(folder_html_modified: Path) -> dict[str, HtmlModifiedManifestEntry]:
    path = manifest_path(folder_html_modified)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as error:
        report_warning(f"html_modified缓存文件无效，按空缓存处理：{path}: {error}")
        return {}

    if not isinstance(raw_data, dict):
        report_warning(f"html_modified缓存文件格式无效，按空缓存处理：{path}")
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
        report_warning(f"html_modified缓存文件缺少files对象，按空缓存处理：{path}")
        return {}

    files: dict[str, HtmlModifiedManifestEntry] = {}
    for raw_filename, raw_entry in cast(dict[object, object], raw_files).items():
        if not isinstance(raw_filename, str) or not isinstance(raw_entry, dict):
            report_warning(f"html_modified缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        entry = cast(dict[object, object], raw_entry)
        source_hash = entry.get("source_hash")
        output_hash = entry.get("output_hash")
        if not isinstance(source_hash, str) or not isinstance(output_hash, str):
            report_warning(f"html_modified缓存文件files格式无效，按空缓存处理：{path}")
            return {}
        manifest_entry: HtmlModifiedManifestEntry = {
            "source_hash": source_hash,
            "output_hash": output_hash,
        }
        output_size = entry.get("output_size")
        output_mtime_ns = entry.get("output_mtime_ns")
        if type(output_size) is int and type(output_mtime_ns) is int:
            manifest_entry["output_size"] = output_size
            manifest_entry["output_mtime_ns"] = output_mtime_ns
        files[raw_filename] = manifest_entry
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
        if not _manifest_file_matches(folder_html_modified / filename, entry):
            continue
        completed_lous.add(lou)
    return completed_lous


def _entry_stat_matches(path: Path, entry: HtmlModifiedManifestEntry) -> bool:
    output_size = entry.get("output_size")
    output_mtime_ns = entry.get("output_mtime_ns")
    if type(output_size) is not int or type(output_mtime_ns) is not int:
        return False

    try:
        stat_result = path.stat()
    except OSError:
        return False
    return (
        stat_result.st_size == output_size
        and stat_result.st_mtime_ns == output_mtime_ns
    )


def _refresh_entry_stat(path: Path, entry: HtmlModifiedManifestEntry) -> None:
    try:
        stat_result = path.stat()
    except OSError:
        return
    entry["output_size"] = stat_result.st_size
    entry["output_mtime_ns"] = stat_result.st_mtime_ns


def _manifest_file_matches(path: Path, entry: HtmlModifiedManifestEntry) -> bool:
    if _entry_stat_matches(path, entry):
        return True
    try:
        output_matches = hash_text(path.read_text(encoding="utf-8")) == entry[
            "output_hash"
        ]
    except (FileNotFoundError, OSError, UnicodeDecodeError):
        return False
    if output_matches:
        _refresh_entry_stat(path, entry)
    return output_matches


def manifest_files_exist(
    folder_html_modified: Path,
    entries: dict[str, HtmlModifiedManifestEntry],
) -> bool:
    return all(
        _manifest_file_matches(folder_html_modified / filename, entry)
        for filename, entry in entries.items()
    )


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
    write_json_atomically(
        manifest_path(folder_html_modified),
        manifest,
        indent=2,
        trailing_newline=True,
    )


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
        entry: HtmlModifiedManifestEntry = {
            "source_hash": source_hash_by_lou[lou],
            "output_hash": output_hash,
        }
        _refresh_entry_stat(folder_html_modified / filename, entry)
        entries[filename] = entry

    write_manifest(folder_html_modified, entries)

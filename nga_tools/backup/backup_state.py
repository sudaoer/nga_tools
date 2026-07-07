from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict, cast

from nga_tools.backup import html_modified_manifest
from nga_tools.backup.floor_map import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
)
from nga_tools.console import report_warning

BACKUP_STATE_FILENAME = "backup_state.json"
BACKUP_STATE_VERSION = 3


class BackupState(TypedDict):
    version: int
    algorithm: str
    author_total_lou_count: int
    page_count: int
    html_modified_generation_version: int
    floor_map_generation_version: int
    html_modified_manifest_entry_count: int
    unresolved_missing_count: int


def state_path(thread_folder: Path) -> Path:
    return thread_folder / BACKUP_STATE_FILENAME


def load_state(thread_folder: Path) -> BackupState | None:
    path = state_path(thread_folder)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except json.JSONDecodeError as error:
        report_warning(f"备份状态文件无效，按无状态处理：{path}: {error}")
        return None

    if not isinstance(raw_data, dict):
        report_warning(f"备份状态文件格式无效，按无状态处理：{path}")
        return None

    data = cast(dict[object, object], raw_data)
    if data.get("version") != BACKUP_STATE_VERSION:
        return None
    if data.get("algorithm") != html_modified_manifest.HTML_MODIFIED_HASH_ALGORITHM:
        return None
    if data.get("algorithm") != FLOOR_MAP_HASH_ALGORITHM:
        return None
    if (
        data.get("html_modified_generation_version")
        != html_modified_manifest.HTML_MODIFIED_GENERATION_VERSION
    ):
        return None
    if data.get("floor_map_generation_version") != FLOOR_MAP_GENERATION_VERSION:
        return None

    int_fields = (
        "author_total_lou_count",
        "page_count",
        "html_modified_manifest_entry_count",
        "unresolved_missing_count",
    )
    for field in int_fields:
        if type(data.get(field)) is not int:
            report_warning(f"备份状态文件字段无效，按无状态处理：{path}: {field}")
            return None
    if data["unresolved_missing_count"] != 0:
        return None

    return {
        "version": BACKUP_STATE_VERSION,
        "algorithm": html_modified_manifest.HTML_MODIFIED_HASH_ALGORITHM,
        "author_total_lou_count": cast(int, data["author_total_lou_count"]),
        "page_count": cast(int, data["page_count"]),
        "html_modified_generation_version": (
            html_modified_manifest.HTML_MODIFIED_GENERATION_VERSION
        ),
        "floor_map_generation_version": FLOOR_MAP_GENERATION_VERSION,
        "html_modified_manifest_entry_count": cast(
            int,
            data["html_modified_manifest_entry_count"],
        ),
        "unresolved_missing_count": 0,
    }


def write_state(
    thread_folder: Path,
    *,
    author_total_lou_count: int,
    page_count: int,
    html_modified_manifest_entry_count: int,
    unresolved_missing_count: int,
) -> None:
    state: BackupState = {
        "version": BACKUP_STATE_VERSION,
        "algorithm": html_modified_manifest.HTML_MODIFIED_HASH_ALGORITHM,
        "author_total_lou_count": author_total_lou_count,
        "page_count": page_count,
        "html_modified_generation_version": (
            html_modified_manifest.HTML_MODIFIED_GENERATION_VERSION
        ),
        "floor_map_generation_version": FLOOR_MAP_GENERATION_VERSION,
        "html_modified_manifest_entry_count": html_modified_manifest_entry_count,
        "unresolved_missing_count": unresolved_missing_count,
    }
    path = state_path(thread_folder)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temp_path.replace(path)

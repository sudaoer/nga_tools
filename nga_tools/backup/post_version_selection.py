from __future__ import annotations

import datetime
import json
from pathlib import Path
from typing import TypedDict, cast

from nga_tools.core.atomic import write_json_atomically
from nga_tools.core.hashing import hash_object

POST_VERSION_SELECTIONS_FILENAME = "post_version_overrides.json"
POST_VERSION_SELECTIONS_VERSION = 1


class PostVersionSelection(TypedDict):
    version_id: int
    source_hash: str
    selected_at: str


class PostVersionSelectionFile(TypedDict):
    version: int
    selections: dict[str, PostVersionSelection]


def selection_path(thread_folder: Path) -> Path:
    return thread_folder / POST_VERSION_SELECTIONS_FILENAME


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def make_selection(version_id: int, source_hash: str) -> PostVersionSelection:
    return {
        "version_id": version_id,
        "source_hash": source_hash,
        "selected_at": _now_utc_iso(),
    }


def _normalize_raw_selection(raw_selection: object) -> PostVersionSelection | None:
    if not isinstance(raw_selection, dict):
        return None
    selection = cast(dict[object, object], raw_selection)
    version_id = selection.get("version_id")
    source_hash = selection.get("source_hash")
    selected_at = selection.get("selected_at")
    if type(version_id) is not int:
        return None
    if not isinstance(source_hash, str) or not source_hash:
        return None
    if not isinstance(selected_at, str) or not selected_at:
        return None
    return {
        "version_id": version_id,
        "source_hash": source_hash,
        "selected_at": selected_at,
    }


def load_selections(thread_folder: Path) -> dict[int, PostVersionSelection]:
    path = selection_path(thread_folder)
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

    if not isinstance(raw_data, dict):
        return {}
    data = cast(dict[object, object], raw_data)
    if data.get("version") != POST_VERSION_SELECTIONS_VERSION:
        return {}
    raw_selections = data.get("selections")
    if not isinstance(raw_selections, dict):
        return {}

    selections: dict[int, PostVersionSelection] = {}
    for raw_lou, raw_selection in cast(dict[object, object], raw_selections).items():
        if not isinstance(raw_lou, str) or not raw_lou.isdigit():
            continue
        lou = int(raw_lou)
        normalized_selection = _normalize_raw_selection(raw_selection)
        if normalized_selection is not None:
            selections[lou] = normalized_selection
    return selections


def write_selections(
    thread_folder: Path,
    selections: dict[int, PostVersionSelection],
) -> None:
    payload: PostVersionSelectionFile = {
        "version": POST_VERSION_SELECTIONS_VERSION,
        "selections": {
            str(lou): selections[lou]
            for lou in sorted(selections)
        },
    }
    write_json_atomically(
        selection_path(thread_folder),
        payload,
        indent=2,
        trailing_newline=True,
    )


def selections_fingerprint(thread_folder: Path) -> str:
    selections = load_selections(thread_folder)
    return hash_object(
        {
            "version": POST_VERSION_SELECTIONS_VERSION,
            "selections": {
                str(lou): selections[lou]
                for lou in sorted(selections)
            },
        }
    )

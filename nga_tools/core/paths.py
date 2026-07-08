from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from nga_tools.config import get_config

WARNING_LOG_FILENAME = "warnings.log"

_CREATED_FOLDERS: set[str] = set()


def get_folder(
    tid: int | str,
    aid: Optional[int | str],
    subfolder: Optional[str] = None,
    *,
    create: bool = True,
) -> str:
    if type(tid) is int:
        tid_part = str(tid)
    elif isinstance(tid, str):
        tid_part = tid
    else:
        raise TypeError("tid must be int or str")

    if type(aid) is int:
        aid_value = str(aid)
    elif aid is None or isinstance(aid, str):
        aid_value = aid
    else:
        raise TypeError("aid must be int, str, or None")

    aid_part = aid_value if aid_value else "all"
    folder_path = Path(get_config().output_dir) / f"{tid_part}_{aid_part}"
    if subfolder:
        folder_path = folder_path / subfolder

    folder = str(folder_path)
    if create and folder not in _CREATED_FOLDERS:
        _CREATED_FOLDERS.add(folder)
        folder_path.mkdir(parents=True, exist_ok=True)

    return folder


def warning_log_path(tid: int, aid: int | None) -> Path:
    return Path(get_folder(tid, aid)) / WARNING_LOG_FILENAME


def list_files_in_folder(folder: str, ends_with: str = "") -> list[str]:
    if not os.path.exists(folder):
        return []
    return [
        filename
        for filename in os.listdir(folder)
        if os.path.isfile(os.path.join(folder, filename))
        and filename.endswith(ends_with)
    ]

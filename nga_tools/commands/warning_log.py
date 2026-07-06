from __future__ import annotations

from pathlib import Path

from nga_tools import utils

WARNING_LOG_FILENAME = "warnings.log"


def warning_log_path(tid: int, aid: int | None) -> Path:
    return Path(utils.get_folder(tid, aid)) / WARNING_LOG_FILENAME

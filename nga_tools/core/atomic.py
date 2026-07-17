from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import threading
from pathlib import Path

_DEFAULT_FILE_MODE = 0o666
_UMASK_LOCK = threading.Lock()


def temporary_sibling_path(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    return Path(temp_name)


def _unlink_if_exists(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _current_umask() -> int:
    with _UMASK_LOCK:
        current_umask = os.umask(0)
        os.umask(current_umask)
    return current_umask


def _new_file_mode_from_umask() -> int:
    return _DEFAULT_FILE_MODE & ~_current_umask()


def _replacement_mode(target_path: Path) -> int:
    try:
        return stat.S_IMODE(target_path.stat().st_mode)
    except FileNotFoundError:
        return _new_file_mode_from_umask()


def _apply_replacement_mode(temp_path: Path, target_path: Path) -> None:
    try:
        temp_path.chmod(_replacement_mode(target_path))
    except OSError:
        if os.name != "nt":
            raise


def replace_temp_file(temp_path: Path, target_path: Path) -> None:
    _apply_replacement_mode(temp_path, target_path)
    temp_path.replace(target_path)


def write_text_atomically(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    temp_path = temporary_sibling_path(path)
    try:
        temp_path.write_text(text, encoding=encoding)
        replace_temp_file(temp_path, path)
    except BaseException:
        _unlink_if_exists(temp_path)
        raise

def write_json_atomically(
    path: Path,
    data: object,
    *,
    indent: int,
    trailing_newline: bool = False,
) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    if trailing_newline:
        text += "\n"
    write_text_atomically(path, text)


def write_text_if_changed_atomically(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> bool:
    try:
        if path.read_text(encoding=encoding) == text:
            return False
    except FileNotFoundError:
        pass

    write_text_atomically(path, text, encoding=encoding)
    return True


def replace_file_atomically(
    source_path: Path,
    target_path: Path,
    *,
    move_source: bool,
) -> None:
    temp_path = temporary_sibling_path(target_path)
    try:
        if move_source:
            shutil.move(str(source_path), str(temp_path))
        else:
            shutil.copy2(source_path, temp_path)
        replace_temp_file(temp_path, target_path)
    except BaseException:
        _unlink_if_exists(temp_path)
        raise

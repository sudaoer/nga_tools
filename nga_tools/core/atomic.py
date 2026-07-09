from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TextIO


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


def write_text_atomically(
    path: Path,
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    temp_path = temporary_sibling_path(path)
    try:
        temp_path.write_text(text, encoding=encoding)
        temp_path.replace(path)
    except BaseException:
        _unlink_if_exists(temp_path)
        raise


def write_bytes_atomically(path: Path, data: bytes) -> None:
    temp_path = temporary_sibling_path(path)
    try:
        temp_path.write_bytes(data)
        temp_path.replace(path)
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
        temp_path.replace(target_path)
    except BaseException:
        _unlink_if_exists(temp_path)
        raise


@contextmanager
def open_text_atomically(
    path: Path,
    *,
    encoding: str = "utf-8",
) -> Generator[TextIO, None, None]:
    temp_path = temporary_sibling_path(path)
    try:
        with temp_path.open("w", encoding=encoding) as output_file:
            yield output_file
        temp_path.replace(path)
    except BaseException:
        _unlink_if_exists(temp_path)
        raise

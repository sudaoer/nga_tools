from __future__ import annotations

from pathlib import Path

from nga_tools.core.atomic import write_text_atomically as _write_text_atomically


def write_text_atomically(path: Path, text: str) -> None:
    _write_text_atomically(path, text)

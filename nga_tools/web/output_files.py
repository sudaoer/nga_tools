from __future__ import annotations

from pathlib import Path
from typing import Optional


def safe_output_file(output_dir: Path, relative_path: str) -> Optional[Path]:
    if not relative_path or relative_path.startswith("/"):
        return None
    output_root = output_dir.resolve()
    candidate = (output_root / relative_path).resolve()
    if not candidate.is_relative_to(output_root):
        return None
    if not candidate.is_file():
        return None
    return candidate

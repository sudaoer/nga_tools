from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from nga_tools import utils
from nga_tools.backup.floor_map import FloorLabels

OVERLAY_POST_RE = re.compile(r"^post_([1-9]\d*)\.html$")


def read_overlay_folder(
    overlay_folder: Path,
    known_lous: set[int],
    floor_labels: Optional[FloorLabels] = None,
) -> dict[int, str]:
    overlays: dict[int, str] = {}
    if not overlay_folder.exists():
        return overlays

    for path in sorted(overlay_folder.iterdir()):
        if not path.is_file():
            continue

        match = OVERLAY_POST_RE.fullmatch(path.name)
        if not match:
            raise RuntimeError(
                f"overlay文件名无效：{path}。必须使用 post_<楼层>.html。"
            )

        lou = int(match.group(1))
        if lou not in known_lous:
            lou_label = floor_labels.label(lou) if floor_labels else f"第{lou}楼"
            raise RuntimeError(f"overlay覆盖了不存在的楼层：{lou_label}。")

        overlays[lou] = path.read_text(encoding="utf-8")

    return overlays


def load_post_overlays(
    tid: int,
    aid: Optional[int],
    known_lous: set[int],
    floor_labels: Optional[FloorLabels] = None,
) -> dict[int, str]:
    overlay_folder = Path(utils.get_folder(tid, aid, "overlay"))
    return read_overlay_folder(overlay_folder, known_lous, floor_labels)

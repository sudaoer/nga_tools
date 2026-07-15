from __future__ import annotations

from pathlib import Path
from typing import Optional

from bs4 import Tag

from nga_tools import utils
from nga_tools.backup import image_store

ABOUT_BLANK_IMAGE_SRC = "about:blank"
_LAZY_IMAGE_URL_ATTRS = ("data-srcorg", "data-srclazy")


def tag_attr_str(tag: Tag, attr_name: str) -> Optional[str]:
    value = tag.get(attr_name)
    if isinstance(value, str):
        return value
    return None


def _style_has_display_none(tag: Tag) -> bool:
    style = tag_attr_str(tag, "style")
    if style is None:
        return False
    compact_style = "".join(style.lower().split())
    return "display:none" in compact_style


def _normalized_src(value: str) -> str:
    return image_store.normalize_nga_image_url(value.strip())


def valid_nga_image_src(value: str) -> Optional[str]:
    normalized_src = _normalized_src(value)
    return normalized_src if utils.NGA_img_link_verify(normalized_src) else None


def _is_windows_drive_path(value: str) -> bool:
    return len(value) >= 2 and value[1] == ":" and value[0].isalpha()


def _has_url_scheme(value: str) -> bool:
    parts = value.split(":", 1)
    return len(parts) == 2 and parts[0].isalpha() and not _is_windows_drive_path(value)


def image_src_path(image_src: str, source_dir: Path) -> Path | None:
    parsed_src = image_src.split("?", 1)[0].strip()
    if _has_url_scheme(parsed_src):
        return None

    path = Path(parsed_src)
    if _is_windows_drive_path(parsed_src):
        return path
    if path.is_absolute():
        return path
    return source_dir / path


def _lazy_nga_image_src(tag: Tag) -> Optional[str]:
    for attr_name in _LAZY_IMAGE_URL_ATTRS:
        value = tag_attr_str(tag, attr_name)
        if value is None:
            continue
        normalized_value = _normalized_src(value)
        if utils.NGA_img_link_verify(normalized_value):
            return normalized_value
    return None


def is_hidden_about_blank_image(tag: Tag) -> bool:
    src = tag_attr_str(tag, "src")
    if src is None:
        return False
    return src.strip().lower() == ABOUT_BLANK_IMAGE_SRC and _style_has_display_none(tag)


def effective_image_src(tag: Tag) -> Optional[str]:
    src = tag_attr_str(tag, "src")
    if src is None:
        return None

    normalized_src = _normalized_src(src)
    if utils.NGA_img_link_verify(normalized_src):
        return normalized_src

    if normalized_src.lower() == ABOUT_BLANK_IMAGE_SRC:
        if is_hidden_about_blank_image(tag):
            return None

        lazy_src = _lazy_nga_image_src(tag)
        if lazy_src is not None:
            return lazy_src

    return normalized_src

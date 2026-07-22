from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from nga_tools.core.image_formats import open_image_for_processing

_BG_TOLERANCE = 30
_ALPHA_THRESHOLD = 128
_CORNER_BLOCK = 8
_BG_UNIFORM_RATIO = 0.9
_COMPOSITE_BG: tuple[int, int, int] = (255, 255, 255)
_MIN_SIDE = 8
_HUE_BINS = 32
_SATURATION_MIN = 25


@dataclass(frozen=True)
class NormalizeResult:
    array: np.ndarray
    has_alpha: bool
    bg_color: tuple[int, int, int] | None
    trimmed: bool
    color_histogram: str
    width: int
    height: int


def _split_channels(arr: np.ndarray) -> tuple[np.ndarray, np.ndarray | None]:
    if arr.ndim == 3 and arr.shape[2] == 4:
        return arr[:, :, :3].copy(), arr[:, :, 3].copy()
    return arr.copy(), None


def _detect_bg_color(arr: np.ndarray) -> tuple[int, int, int] | None:
    h, w = arr.shape[:2]
    bs = min(_CORNER_BLOCK, h, w)
    corners = (
        arr[:bs, :bs],
        arr[:bs, w - bs :],
        arr[h - bs :, :bs],
        arr[h - bs :, w - bs :],
    )
    combined = np.concatenate([c.reshape(-1, arr.shape[-1])[:, :3] for c in corners])
    colors, counts = np.unique(combined, axis=0, return_counts=True)
    index = int(np.argmax(counts))
    if counts[index] / combined.shape[0] < _BG_UNIFORM_RATIO:
        return None
    color = colors[index]
    return int(color[0]), int(color[1]), int(color[2])


def _background_mask_from_alpha(alpha: np.ndarray) -> np.ndarray:
    return alpha < _ALPHA_THRESHOLD


def _background_mask_from_color(
    arr: np.ndarray, bg_color: tuple[int, int, int]
) -> np.ndarray:
    diff = arr.astype(np.int32) - np.array(bg_color, dtype=np.int32)
    distance = np.sqrt((diff * diff).sum(axis=-1))
    return distance < _BG_TOLERANCE


def _trim_to_foreground(
    arr: np.ndarray, fg_mask: np.ndarray
) -> tuple[np.ndarray, np.ndarray, bool]:
    rows = np.any(fg_mask, axis=1)
    cols = np.any(fg_mask, axis=0)
    if not rows.any() or not cols.any():
        return arr, fg_mask, False
    r0, r1 = int(np.argmax(rows)), int(len(rows) - np.argmax(rows[::-1]))
    c0, c1 = int(np.argmax(cols)), int(len(cols) - np.argmax(cols[::-1]))
    if r0 == 0 and r1 == arr.shape[0] and c0 == 0 and c1 == arr.shape[1]:
        return arr, fg_mask, False
    return arr[r0:r1, c0:c1].copy(), fg_mask[r0:r1, c0:c1].copy(), True


def _composite_on_white(
    arr: np.ndarray, fg_mask: np.ndarray
) -> np.ndarray:
    if arr.ndim != 3 or arr.shape[2] != 3:
        arr = arr[:, :, :3].copy()
    canvas = np.full_like(arr, 0, dtype=np.uint8)
    canvas[:, :, 0] = _COMPOSITE_BG[0]
    canvas[:, :, 1] = _COMPOSITE_BG[1]
    canvas[:, :, 2] = _COMPOSITE_BG[2]
    canvas[fg_mask] = arr[fg_mask]
    return canvas


def _compute_color_histogram(
    arr: np.ndarray, fg_mask: np.ndarray
) -> str:
    if not fg_mask.any():
        return ""
    hsv = np.array(Image.fromarray(arr).convert("HSV"))
    hue = hsv[:, :, 0][fg_mask]
    saturation = hsv[:, :, 1][fg_mask]
    colored = saturation > _SATURATION_MIN
    if not colored.any():
        return ""
    hue_colored = hue[colored]
    hist, _ = np.histogram(hue_colored, bins=_HUE_BINS, range=(0, 256))
    total = hist.sum()
    if total == 0:
        return ""
    normalized = hist / total
    return ",".join(f"{v:.6f}" for v in normalized)


def normalize_image(path: Path) -> NormalizeResult:
    with open_image_for_processing(path) as image:
        converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        source = np.array(converted)

    has_alpha = source.ndim == 3 and source.shape[2] == 4
    arr, alpha = _split_channels(source)
    h, w = arr.shape[:2]

    bg_color: tuple[int, int, int] | None = None
    bg_mask: np.ndarray

    if has_alpha and alpha is not None:
        bg_mask = _background_mask_from_alpha(alpha)
    else:
        detected = _detect_bg_color(arr)
        bg_color = detected
        if detected is None:
            bg_mask = np.zeros((h, w), dtype=bool)
        else:
            bg_mask = _background_mask_from_color(arr, detected)

    fg_mask = ~bg_mask
    color_histogram = _compute_color_histogram(arr, fg_mask)
    trimmed_arr, trimmed_fg, trimmed = _trim_to_foreground(arr, fg_mask)
    composite = _composite_on_white(trimmed_arr, trimmed_fg)

    return NormalizeResult(
        array=composite,
        has_alpha=has_alpha,
        bg_color=bg_color,
        trimmed=trimmed,
        color_histogram=color_histogram,
        width=int(composite.shape[1]),
        height=int(composite.shape[0]),
    )

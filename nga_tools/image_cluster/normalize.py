from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from nga_tools.core.image_formats import open_image_for_processing

_BG_TOLERANCE = 30
_ALPHA_THRESHOLD = 128
_CORNER_BLOCK = 8
_BG_UNIFORM_RATIO = 0.9
_COMPOSITE_BG: tuple[int, int, int] = (255, 255, 255)
_MIN_SIDE = 8


@dataclass(frozen=True)
class NormalizeResult:
    array: np.ndarray
    has_alpha: bool
    bg_color: tuple[int, int, int] | None
    trimmed: bool
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
    trimmed_arr, trimmed_fg, trimmed = _trim_to_foreground(arr, fg_mask)
    composite = _composite_on_white(trimmed_arr, trimmed_fg)

    return NormalizeResult(
        array=composite,
        has_alpha=has_alpha,
        bg_color=bg_color,
        trimmed=trimmed,
        width=int(composite.shape[1]),
        height=int(composite.shape[0]),
    )


def normalize_image_passthrough(path: Path) -> NormalizeResult:
    with open_image_for_processing(path) as image:
        converted = image.convert("RGB")
        arr = np.array(converted)
    h, w = arr.shape[:2]
    if h < _MIN_SIDE or w < _MIN_SIDE:
        canvas = np.full(
            (_MIN_SIDE, _MIN_SIDE, 3), 0, dtype=np.uint8
        )
        canvas[:, :, 0] = _COMPOSITE_BG[0]
        canvas[:, :, 1] = _COMPOSITE_BG[1]
        canvas[:, :, 2] = _COMPOSITE_BG[2]
        return NormalizeResult(
            array=canvas,
            has_alpha=False,
            bg_color=None,
            trimmed=False,
            width=_MIN_SIDE,
            height=_MIN_SIDE,
        )
    return NormalizeResult(
        array=arr,
        has_alpha=False,
        bg_color=None,
        trimmed=False,
        width=w,
        height=h,
    )

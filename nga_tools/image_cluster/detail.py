from __future__ import annotations

import warnings
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import numpy as np
from numpy.typing import NDArray
from PIL import Image
from scipy import ndimage # pyright: ignore[reportMissingTypeStubs]
from scipy.cluster import hierarchy as scipy_hierarchy # pyright: ignore[reportMissingTypeStubs]

from nga_tools.image_cluster.normalize import normalize_image


DETAIL_MAX_WIDTH = 256
DETAIL_MAX_HEIGHT = 512
DETAIL_BORDER_RATIO = 0.2
DETAIL_GAUSSIAN_SIGMA = 0.65
DETAIL_WINDOW_SIZE = 13
DETAIL_MIN_WINDOW_VALID_RATIO = 0.8
DETAIL_SCORE_ALGORITHM = (
    "center-local-rgb-v1:"
    f"w{DETAIL_MAX_WIDTH}-h{DETAIL_MAX_HEIGHT}-"
    f"border{DETAIL_BORDER_RATIO}-sigma{DETAIL_GAUSSIAN_SIGMA}-"
    f"window{DETAIL_WINDOW_SIZE}"
)

FloatImage = NDArray[np.float32]
Float64Array = NDArray[np.float64]
IntArray = NDArray[np.int32]
PairKey = tuple[str, str]

_gaussian_filter = cast(
    Callable[..., FloatImage],
    ndimage.gaussian_filter,  # pyright: ignore[reportUnknownMemberType]
)
_uniform_filter = cast(
    Callable[..., FloatImage],
    ndimage.uniform_filter,  # pyright: ignore[reportUnknownMemberType]
)
_linkage = cast(
    Callable[..., Float64Array],
    scipy_hierarchy.linkage,  # pyright: ignore[reportUnknownMemberType]
)
_fcluster = cast(
    Callable[..., IntArray],
    scipy_hierarchy.fcluster,  # pyright: ignore[reportUnknownMemberType]
)


@dataclass(frozen=True)
class DetailPairScore:
    path_a: str
    path_b: str
    size_a: int
    mtime_ns_a: int
    size_b: int
    mtime_ns_b: int
    algorithm: str
    score: float


def pair_key(path_a: str, path_b: str) -> PairKey:
    if path_a <= path_b:
        return path_a, path_b
    return path_b, path_a


def prepare_detail_image(path: Path) -> FloatImage | None:
    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message=(
                    "Palette images with Transparency expressed in bytes "
                    "should be converted to RGBA images"
                ),
                category=UserWarning,
                module="PIL.Image",
            )
            normalized = normalize_image(path)
    except (OSError, ValueError):
        return None

    height, width = normalized.array.shape[:2]
    if height <= 0 or width <= 0:
        return None

    scale = min(DETAIL_MAX_WIDTH / width, DETAIL_MAX_HEIGHT / height)
    target_width = max(1, int(round(width * scale)))
    target_height = max(1, int(round(height * scale)))
    image = Image.fromarray(normalized.array, mode="RGB")
    resized = image.resize(
        (target_width, target_height),
        resample=Image.Resampling.LANCZOS,
    )
    array = np.asarray(resized, dtype=np.float32) / np.float32(255.0)
    blurred = _gaussian_filter(
        array,
        sigma=(DETAIL_GAUSSIAN_SIGMA, DETAIL_GAUSSIAN_SIGMA, 0.0),
        mode="nearest",
    )
    return np.asarray(blurred, dtype=np.float32)


def detail_difference_score(
    image_a: FloatImage | None,
    image_b: FloatImage | None,
) -> float:
    if image_a is None or image_b is None:
        return 1.0
    if image_a.ndim != 3 or image_b.ndim != 3:
        return 1.0
    if image_a.shape[2] != 3 or image_b.shape[2] != 3:
        return 1.0

    canvas_height = max(image_a.shape[0], image_b.shape[0])
    canvas_width = max(image_a.shape[1], image_b.shape[1])
    if canvas_height < DETAIL_WINDOW_SIZE or canvas_width < DETAIL_WINDOW_SIZE:
        return 1.0

    canvas_a = np.ones(
        (canvas_height, canvas_width, 3), dtype=np.float32
    )
    canvas_b = np.ones_like(canvas_a)
    valid_a = np.zeros((canvas_height, canvas_width), dtype=np.float32)
    valid_b = np.zeros_like(valid_a)

    _place_centered(canvas_a, valid_a, image_a)
    _place_centered(canvas_b, valid_b, image_b)

    top = int(round(canvas_height * DETAIL_BORDER_RATIO))
    bottom = int(round(canvas_height * (1.0 - DETAIL_BORDER_RATIO)))
    left = int(round(canvas_width * DETAIL_BORDER_RATIO))
    right = int(round(canvas_width * (1.0 - DETAIL_BORDER_RATIO)))
    if bottom - top < DETAIL_WINDOW_SIZE or right - left < DETAIL_WINDOW_SIZE:
        return 1.0

    roi_a = canvas_a[top:bottom, left:right]
    roi_b = canvas_b[top:bottom, left:right]
    valid = (valid_a[top:bottom, left:right] * valid_b[top:bottom, left:right])
    if not np.any(valid):
        return 1.0

    difference = np.max(np.abs(roi_a - roi_b), axis=2) * valid
    window_difference = _uniform_filter(
        difference,
        size=DETAIL_WINDOW_SIZE,
        mode="constant",
        cval=0.0,
    )
    window_valid = _uniform_filter(
        valid,
        size=DETAIL_WINDOW_SIZE,
        mode="constant",
        cval=0.0,
    )
    eligible = window_valid >= DETAIL_MIN_WINDOW_VALID_RATIO
    if not np.any(eligible):
        return 1.0

    means = np.zeros_like(window_difference)
    np.divide(
        window_difference,
        window_valid,
        out=means,
        where=window_valid > 0.0,
    )
    score = float(np.max(means[eligible]))
    return min(max(score, 0.0), 1.0)


def _place_centered(
    canvas: FloatImage,
    valid: FloatImage,
    image: FloatImage,
) -> None:
    height, width = image.shape[:2]
    top = (canvas.shape[0] - height) // 2
    left = (canvas.shape[1] - width) // 2
    canvas[top : top + height, left : left + width] = image
    valid[top : top + height, left : left + width] = 1.0


def complete_linkage_groups(
    members: list[str],
    scores: dict[PairKey, float],
    threshold: float,
) -> list[list[str]]:
    ordered = sorted(members)
    if len(ordered) < 2:
        return [ordered]

    condensed = np.fromiter(
        (
            scores.get(pair_key(ordered[i], ordered[j]), 1.0)
            for i in range(len(ordered) - 1)
            for j in range(i + 1, len(ordered))
        ),
        dtype=np.float64,
        count=len(ordered) * (len(ordered) - 1) // 2,
    )
    hierarchy = _linkage(condensed, method="complete")
    labels = _fcluster(hierarchy, t=threshold, criterion="distance")
    grouped: dict[int, list[str]] = {}
    for path, label in zip(ordered, labels, strict=True):
        grouped.setdefault(int(label), []).append(path)
    result = [sorted(group) for group in grouped.values()]
    result.sort(key=lambda group: (-len(group), group[0]))
    return result

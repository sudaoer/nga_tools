from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import imagehash
import numpy as np
from PIL import Image

from nga_tools.image_cluster.normalize import (
    NormalizeResult,
    normalize_image,
)

_HASH_SIZE = 8
_IMAGEHASH_VERSION = str(getattr(imagehash, "__version__", "unknown"))
IMAGE_HASH_ALGORITHM = (
    f"imagehash-{_IMAGEHASH_VERSION}:phash{_HASH_SIZE}-dhash{_HASH_SIZE}"
)


@dataclass(frozen=True)
class ImageFeatures:
    relative_path: str
    size: int
    mtime_ns: int
    phash: str
    dhash: str
    has_alpha: bool
    bg_color: tuple[int, int, int] | None
    trimmed: bool
    color_histogram: str
    width: int
    height: int


def compute_hashes(array: np.ndarray) -> tuple[str, str]:
    image = Image.fromarray(array)
    phash = imagehash.phash(image, hash_size=_HASH_SIZE)
    dhash = imagehash.dhash(image, hash_size=_HASH_SIZE)
    return str(phash), str(dhash)


def hamming_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def color_distance(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ha = a.split(",")
    hb = b.split(",")
    if len(ha) != len(hb):
        return 0.0
    try:
        return sum(abs(float(x) - float(y)) for x, y in zip(ha, hb))
    except ValueError:
        return 0.0


def extract_features(
    path: Path, relative_path: str
) -> ImageFeatures | None:
    try:
        result: NormalizeResult = normalize_image(path)
    except (OSError, ValueError):
        return None

    phash, dhash = compute_hashes(result.array)

    stat = path.stat()
    return ImageFeatures(
        relative_path=relative_path,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        phash=phash,
        dhash=dhash,
        has_alpha=result.has_alpha,
        bg_color=result.bg_color,
        trimmed=result.trimmed,
        color_histogram=result.color_histogram,
        width=result.width,
        height=result.height,
    )

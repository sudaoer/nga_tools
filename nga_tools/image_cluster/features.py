from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
from imagededup.methods import DHash, PHash

from nga_tools.image_cluster.normalize import (
    NormalizeResult,
    normalize_image,
)

_phash = PHash(verbose=False)
_dhash = DHash(verbose=False)


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
    watermark_masked: bool
    width: int
    height: int


def compute_hashes(array: np.ndarray) -> tuple[str | None, str | None]:
    phash = _phash.encode_image(image_array=array)  # pyright: ignore[reportUnknownMemberType]
    dhash = _dhash.encode_image(image_array=array)  # pyright: ignore[reportUnknownMemberType]
    return phash, dhash


def hamming_distance(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def extract_features(
    path: Path, relative_path: str
) -> ImageFeatures | None:
    try:
        result: NormalizeResult = normalize_image(path)
    except (OSError, ValueError):
        return None

    phash, dhash = compute_hashes(result.array)
    if phash is None or dhash is None:
        return None

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
        watermark_masked=result.watermark_masked,
        width=result.width,
        height=result.height,
    )

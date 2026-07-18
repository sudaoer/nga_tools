from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from nga_tools.image_cluster.detail import (
    DETAIL_MAX_HEIGHT,
    DETAIL_MAX_WIDTH,
    complete_linkage_groups,
    detail_difference_score,
    pair_key,
    prepare_detail_image,
)


def _solid(
    value: float, height: int = 100, width: int = 100
) -> NDArray[np.float32]:
    return np.full((height, width, 3), value, dtype=np.float32)


def test_detail_score_ignores_outer_watermark_region() -> None:
    original = _solid(0.2)
    watermarked = original.copy()
    watermarked[:15, :15] = 1.0

    assert detail_difference_score(original, watermarked) == 0.0


def test_detail_score_detects_small_central_pupil_change() -> None:
    yellow_pupil = _solid(0.2)
    purple_pupil = yellow_pupil.copy()
    yellow_pupil[44:57, 44:57] = np.array(
        [1.0, 0.8, 0.0], dtype=np.float32
    )
    purple_pupil[44:57, 44:57] = np.array(
        [0.5, 0.0, 1.0], dtype=np.float32
    )

    assert detail_difference_score(yellow_pupil, purple_pupil) > 0.18


def test_detail_score_tolerates_low_level_compression_noise() -> None:
    rng = np.random.default_rng(42)
    original = rng.random((100, 100, 3), dtype=np.float32)
    noise = rng.uniform(-0.02, 0.02, size=original.shape).astype(np.float32)
    compressed = np.clip(original + noise, 0.0, 1.0).astype(np.float32)

    assert detail_difference_score(original, compressed) < 0.18


def test_detail_score_compares_centered_valid_overlap() -> None:
    rng = np.random.default_rng(1)
    original = rng.random((100, 100, 3), dtype=np.float32)
    padded = np.ones((100, 120, 3), dtype=np.float32)
    padded[:, 10:110] = original

    assert detail_difference_score(original, padded) == 0.0


def test_complete_linkage_prevents_chain_merge() -> None:
    scores = {
        pair_key("a", "b"): 0.1,
        pair_key("b", "c"): 0.1,
        pair_key("a", "c"): 0.3,
    }

    groups = complete_linkage_groups(["a", "b", "c"], scores, 0.18)

    assert sorted(len(group) for group in groups) == [1, 2]


def test_prepare_detail_image_respects_size_bounds(tmp_path: Path) -> None:
    array = np.zeros((1200, 400, 3), dtype=np.uint8)
    array[:, :, 1] = 128
    path = tmp_path / "tall.png"
    Image.fromarray(array, mode="RGB").save(path)

    prepared = prepare_detail_image(path)

    assert prepared is not None
    assert prepared.shape[0] <= DETAIL_MAX_HEIGHT
    assert prepared.shape[1] <= DETAIL_MAX_WIDTH
    assert prepared.dtype == np.float32

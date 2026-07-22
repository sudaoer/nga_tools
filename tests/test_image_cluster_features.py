from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from nga_tools.image_cluster.features import (
    ImageFeatures,
    extract_features,
    hamming_distance,
)


def _make_image(
    path: Path, color: tuple[int, int, int] = (200, 50, 80)
) -> None:
    arr = np.zeros((200, 250, 3), dtype=np.uint8)
    arr[:, :] = (255, 255, 255)
    span = np.arange(100)
    r_vals = np.clip(200 - span * 2, 0, 255).astype(np.uint8)
    g_vals = np.clip(50 + span.reshape(-1, 1) * 2, 0, 255).astype(np.uint8)
    arr[50:150, 75:175, 0] = r_vals
    arr[50:150, 75:175, 1] = g_vals
    arr[50:150, 75:175, 2] = color[2]
    Image.fromarray(arr, mode="RGB").save(path)


def test_extract_features_returns_hashes(tmp_path: Path) -> None:
    path = tmp_path / "img.png"
    _make_image(path)

    features = extract_features(path, "img.png")

    assert features is not None
    assert isinstance(features, ImageFeatures)
    assert features.relative_path == "img.png"
    assert len(features.phash) == 16
    assert len(features.dhash) == 16
    assert features.phash != features.dhash
    assert features.bg_color == (255, 255, 255)
    assert features.has_alpha is False
    assert features.width > 0
    assert features.height > 0
    assert features.size == path.stat().st_size


def test_identical_images_produce_identical_features(tmp_path: Path) -> None:
    path_a = tmp_path / "a.png"
    path_b = tmp_path / "b.png"
    _make_image(path_a)
    _make_image(path_b)

    fa = extract_features(path_a, "a.png")
    fb = extract_features(path_b, "b.png")

    assert fa is not None and fb is not None
    assert fa.phash == fb.phash
    assert fa.dhash == fb.dhash
    assert hamming_distance(fa.phash, fb.phash) == 0


def test_jpeg_recompression_preserves_hash(tmp_path: Path) -> None:
    base = tmp_path / "base.png"
    _make_image(base, (200, 50, 80))

    arr = np.array(Image.open(base).convert("RGB"))
    jpeg_path = tmp_path / "recompressed.jpg"
    Image.fromarray(arr, mode="RGB").save(jpeg_path, quality=98)

    base_f = extract_features(base, "base.png")
    jpeg_f = extract_features(jpeg_path, "recompressed.jpg")

    assert base_f is not None and jpeg_f is not None
    assert hamming_distance(base_f.phash, jpeg_f.phash) < 32


def test_extract_features_returns_none_for_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")

    assert extract_features(path, "broken.png") is None

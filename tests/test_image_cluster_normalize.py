from __future__ import annotations

from pathlib import Path

import numpy as np
import imagehash
from PIL import Image

from nga_tools.image_cluster.normalize import (
    normalize_image,
    normalize_image_passthrough,
)


def _save_png(arr: np.ndarray, path: Path, mode: str = "RGB") -> None:
    Image.fromarray(arr.astype(np.uint8), mode=mode).save(path)


def _make_subject_on(
    bg_color: tuple[int, int, int], size: tuple[int, int] = (100, 80)
) -> np.ndarray:
    arr = np.zeros((size[1], size[0], 3), dtype=np.uint8)
    arr[:, :, 0] = bg_color[0]
    arr[:, :, 1] = bg_color[1]
    arr[:, :, 2] = bg_color[2]
    r_vals = np.clip(200 - np.arange(40) * 3, 0, 255).astype(np.uint8)
    g_vals = np.clip(50 + np.arange(40).reshape(-1, 1) * 3, 0, 255).astype(np.uint8)
    arr[20:60, 30:70, 0] = r_vals
    arr[20:60, 30:70, 1] = g_vals
    arr[20:60, 30:70, 2] = 80
    return arr


_SUBJECT_TOP_LEFT = (200, 50, 80)


def test_white_bg_detected(tmp_path: Path) -> None:
    path = tmp_path / "white.png"
    _save_png(_make_subject_on((255, 255, 255)), path)

    result = normalize_image(path)

    assert result.has_alpha is False
    assert result.bg_color == (255, 255, 255)
    assert result.trimmed is True
    assert len(result.color_histogram) > 0
    assert tuple(result.array[0, 0].tolist()) == _SUBJECT_TOP_LEFT


def test_black_bg_detected(tmp_path: Path) -> None:
    path = tmp_path / "black.png"
    _save_png(_make_subject_on((0, 0, 0)), path)

    result = normalize_image(path)

    assert result.bg_color == (0, 0, 0)
    assert result.trimmed is True
    assert tuple(result.array[0, 0].tolist()) == _SUBJECT_TOP_LEFT


def test_alpha_transparent_bg_detected(tmp_path: Path) -> None:
    rgba = np.zeros((80, 100, 4), dtype=np.uint8)
    rgba[20:60, 30:70] = (200, 50, 80, 255)
    rgba[20:60, 30:70, 0] = np.clip(200 - np.arange(40) * 3, 0, 255).astype(np.uint8)
    rgba[20:60, 30:70, 1] = np.clip(
        50 + np.arange(40).reshape(-1, 1) * 3, 0, 255
    ).astype(np.uint8)
    rgba[20:60, 30:70, 2] = 80
    path = tmp_path / "alpha.png"
    Image.fromarray(rgba, mode="RGBA").save(path)

    result = normalize_image(path)

    assert result.has_alpha is True
    assert result.bg_color is None
    assert tuple(result.array[0, 0].tolist()) == _SUBJECT_TOP_LEFT


def test_black_white_alpha_produce_identical_hashes(tmp_path: Path) -> None:
    variants = []
    for name, bg in (("black.png", (0, 0, 0)), ("white.png", (255, 255, 255))):
        path = tmp_path / name
        _save_png(_make_subject_on(bg), path)
        variants.append(
            str(imagehash.phash(Image.fromarray(normalize_image(path).array)))
        )

    rgba = np.zeros((80, 100, 4), dtype=np.uint8)
    rgba[20:60, 30:70] = (200, 50, 80, 255)
    rgba[20:60, 30:70, 0] = np.clip(200 - np.arange(40) * 3, 0, 255).astype(np.uint8)
    rgba[20:60, 30:70, 1] = np.clip(
        50 + np.arange(40).reshape(-1, 1) * 3, 0, 255
    ).astype(np.uint8)
    rgba[20:60, 30:70, 2] = 80
    alpha_path = tmp_path / "alpha.png"
    Image.fromarray(rgba, mode="RGBA").save(alpha_path)
    variants.append(
        str(imagehash.phash(Image.fromarray(normalize_image(alpha_path).array)))
    )

    assert variants[0] == variants[1] == variants[2]
    assert variants[0] != "ffffffffffffffff"
    assert variants[0] != "0000000000000000"


def test_realphoto_no_uniform_bg(tmp_path: Path) -> None:
    arr = np.zeros((80, 100, 3), dtype=np.uint8)
    for x in range(100):
        for y in range(80):
            arr[y, x] = (y * 3 % 256, x * 2 % 256, (x + y) % 256)
    path = tmp_path / "photo.png"
    _save_png(arr, path)

    result = normalize_image(path)

    assert result.bg_color is None
    assert result.trimmed is False
    assert result.array.shape == (80, 100, 3)


def test_trim_removes_uniform_border(tmp_path: Path) -> None:
    arr = np.full((80, 100, 3), 255, dtype=np.uint8)
    arr[30:50, 40:60] = (200, 50, 50)
    path = tmp_path / "bordered.png"
    _save_png(arr, path)

    result = normalize_image(path)

    assert result.trimmed is True
    assert result.width == 20
    assert result.height == 20


def test_small_image_returns_min_canvas(tmp_path: Path) -> None:
    arr = np.full((3, 4, 3), 200, dtype=np.uint8)
    path = tmp_path / "tiny.png"
    _save_png(arr, path)

    result = normalize_image_passthrough(path)

    assert result.width == 8
    assert result.height == 8
    assert tuple(result.array[0, 0].tolist()) == (255, 255, 255)


def test_jpeg_input_works(tmp_path: Path) -> None:
    arr = _make_subject_on((255, 255, 255))
    path = tmp_path / "img.jpg"
    Image.fromarray(arr, mode="RGB").save(path, quality=95)

    result = normalize_image(path)

    assert result.bg_color is not None
    hash_value = str(imagehash.phash(Image.fromarray(result.array)))
    assert isinstance(hash_value, str)
    assert hash_value != "ffffffffffffffff"
    assert hash_value != "0000000000000000"

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

from nga_tools.image_cluster import (
    ClusterParams,
    ClusterRunResult,
    default_workers,
    run_image_cluster,
)
from nga_tools.image_cluster.store import ImageClusterStore


def _make_subject_image(
    path: Path,
    bg: tuple[int, int, int],
    seed: int = 0,
) -> None:
    arr = np.zeros((200, 250, 3), dtype=np.uint8)
    arr[:, :] = bg
    rng = np.random.default_rng(seed)
    span = np.arange(100)
    r_vals = np.clip(200 - span * 2, 0, 255).astype(np.uint8)
    g_vals = np.clip(50 + span.reshape(-1, 1) * 2, 0, 255).astype(np.uint8)
    arr[50:150, 75:175, 0] = r_vals
    arr[50:150, 75:175, 1] = g_vals
    arr[50:150, 75:175, 2] = 80
    noise = rng.integers(0, 10, size=(100, 100, 3), dtype=np.uint8)
    sub = arr[50:150, 75:175].astype(np.int16)
    arr[50:150, 75:175] = np.clip(sub + noise, 0, 255).astype(np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)


def _make_alpha_image(path: Path, seed: int = 0) -> None:
    rgba = np.zeros((200, 250, 4), dtype=np.uint8)
    rng = np.random.default_rng(seed)
    span = np.arange(100)
    r_vals = np.clip(200 - span * 2, 0, 255).astype(np.uint8)
    g_vals = np.clip(50 + span.reshape(-1, 1) * 2, 0, 255).astype(np.uint8)
    rgba[50:150, 75:175, 0] = r_vals
    rgba[50:150, 75:175, 1] = g_vals
    rgba[50:150, 75:175, 2] = 80
    rgba[50:150, 75:175, 3] = 255
    noise = rng.integers(0, 10, size=(100, 100, 3), dtype=np.uint8)
    sub = rgba[50:150, 75:175, :3].astype(np.int16)
    rgba[50:150, 75:175, :3] = np.clip(sub + noise, 0, 255).astype(np.uint8)
    Image.fromarray(rgba, mode="RGBA").save(path)


def _make_unrelated_image(path: Path, seed: int = 100) -> None:
    rng = np.random.default_rng(seed)
    arr = rng.integers(0, 256, size=(200, 250, 3), dtype=np.uint8)
    Image.fromarray(arr, mode="RGB").save(path)


def test_run_image_cluster_clusters_variants(tmp_path: Path) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    _make_subject_image(images_dir / "white.png", (255, 255, 255), seed=1)
    _make_subject_image(images_dir / "black.png", (0, 0, 0), seed=1)
    _make_alpha_image(images_dir / "alpha.png", seed=1)
    _make_unrelated_image(images_dir / "noise.png", seed=99)

    result = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, min_cluster_size=2, workers=1)
    )

    assert isinstance(result, ClusterRunResult)
    assert result.total_images == 4
    assert result.features_computed == 4
    assert result.features_reused == 0
    assert result.cluster_count >= 1
    assert result.clustered_images >= 2

    store = ImageClusterStore(tmp_path)
    assert store.latest_run_id() == result.run_id
    clusters = store.load_clusters(result.run_id)
    assert len(clusters) == result.cluster_count

    biggest = clusters[0]
    member_paths = {m.relative_path for m in clusters[0].members}
    assert "images_unique/noise.png" not in member_paths
    sources = [m for m in biggest.members if m.is_source_candidate]
    assert len(sources) == 1
    assert sources[0].relative_path == "images_unique/alpha.png"


def test_run_image_cluster_incremental_reuses_cache(tmp_path: Path) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    _make_subject_image(images_dir / "a.png", (255, 255, 255), seed=1)
    _make_subject_image(images_dir / "b.png", (0, 0, 0), seed=1)

    first = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1)
    )
    assert first.features_computed == 2
    assert first.features_reused == 0

    second = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1)
    )
    assert second.features_computed == 0
    assert second.features_reused == 2


def test_run_image_cluster_invalidates_changed_hash_algorithm(
    tmp_path: Path,
) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    _make_subject_image(images_dir / "a.png", (255, 255, 255), seed=1)
    _make_subject_image(images_dir / "b.png", (0, 0, 0), seed=1)

    run_image_cluster(tmp_path, ClusterParams(threshold=12, workers=1))
    store = ImageClusterStore(tmp_path)
    store.save_run({"hash_algorithm": "legacy:phash8-dhash8"}, [])

    result = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1)
    )

    assert result.features_computed == 2
    assert result.features_reused == 0


def test_run_image_cluster_force_recomputes(tmp_path: Path) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    _make_subject_image(images_dir / "a.png", (255, 255, 255), seed=1)

    run_image_cluster(tmp_path, ClusterParams(threshold=12, workers=1))
    forced = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1, force=True)
    )
    assert forced.features_computed == 1
    assert forced.features_reused == 0


def test_run_image_cluster_deletes_missing_features(tmp_path: Path) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    _make_subject_image(images_dir / "a.png", (255, 255, 255), seed=1)
    _make_subject_image(images_dir / "b.png", (0, 0, 0), seed=1)

    run_image_cluster(tmp_path, ClusterParams(threshold=12, workers=1))
    (images_dir / "b.png").unlink()

    result = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1)
    )
    assert result.features_deleted == 1

    store = ImageClusterStore(tmp_path)
    features = store.load_all_features()
    assert "images_unique/b.png" not in features


def test_run_image_cluster_empty_dir(tmp_path: Path) -> None:
    result = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1)
    )
    assert result.total_images == 0
    assert result.cluster_count == 0
    assert result.candidate_pairs == 0


def test_run_image_cluster_limit(tmp_path: Path) -> None:
    images_dir = tmp_path / "images_unique"
    images_dir.mkdir()
    for i in range(5):
        _make_subject_image(
            images_dir / f"img{i}.png", (255, 255, 255), seed=i
        )

    result = run_image_cluster(
        tmp_path, ClusterParams(threshold=12, workers=1, limit=2)
    )
    assert result.total_images == 2
    assert result.features_computed == 2


def test_default_workers_positive() -> None:
    assert default_workers() >= 1

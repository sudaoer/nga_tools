from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from PIL import Image

from nga_tools.cli import args_parse
from nga_tools.commands.cluster import cluster_run
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


class ClusterCliTest:
    def test_cluster_run_args_parse_defaults(self) -> None:
        args = args_parse(["cluster", "run"])

        assert args["command"] == "cluster"
        assert args["action"] == "run"
        assert args["threshold"] == 1
        assert args["dhash_threshold"] == 2
        assert args["min_cluster_size"] == 2
        assert args["lsh_bands"] == 4

    def test_cluster_run_args_parse_custom(self) -> None:
        args = args_parse(
            [
                "cluster",
                "run",
                "--threshold",
                "12",
                "--dhash-threshold",
                "0",
                "--min-cluster-size",
                "3",
                "--lsh-bands",
                "8",
                "--workers",
                "2",
                "--limit",
                "50",
                "--force",
            ]
        )

        assert args["threshold"] == 12
        assert args["dhash_threshold"] == 0
        assert args["min_cluster_size"] == 3
        assert args["lsh_bands"] == 8
        assert args["workers"] == 2
        assert args["limit"] == 50
        assert args["force"] is True

    def test_cluster_run_processor_end_to_end(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        images_dir = output_dir / "images_unique"
        images_dir.mkdir(parents=True)
        _make_subject_image(images_dir / "white.png", (255, 255, 255), seed=1)
        _make_subject_image(images_dir / "black.png", (0, 0, 0), seed=1)

        with patch(
            "nga_tools.commands.cluster.get_config",
            return_value=SimpleNamespace(output_dir=str(output_dir)),
        ):
            cluster_run({"workers": 1, "threshold": 12})

        store = ImageClusterStore(output_dir)
        assert store.latest_run_id() is not None
        clusters = store.load_clusters(store.latest_run_id() or 0)
        assert len(clusters) >= 1
        assert any(
            m.is_source_candidate
            for cluster in clusters
            for m in cluster.members
        )

    def test_cluster_run_processor_with_force(
        self, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "output"
        images_dir = output_dir / "images_unique"
        images_dir.mkdir(parents=True)
        _make_subject_image(images_dir / "a.png", (255, 255, 255), seed=1)

        with patch(
            "nga_tools.commands.cluster.get_config",
            return_value=SimpleNamespace(output_dir=str(output_dir)),
        ):
            cluster_run({"workers": 1, "threshold": 12})
            cluster_run({"workers": 1, "threshold": 12, "force": True})

        store = ImageClusterStore(output_dir)
        assert store.latest_run_id() is not None

    def test_cluster_run_processor_empty_dir(
        self, tmp_path: Path
    ) -> None:
        output_dir = tmp_path / "output"
        (output_dir / "images_unique").mkdir(parents=True)

        with patch(
            "nga_tools.commands.cluster.get_config",
            return_value=SimpleNamespace(output_dir=str(output_dir)),
        ):
            cluster_run({"workers": 1})

        store = ImageClusterStore(output_dir)
        assert store.latest_run_id() is not None
        clusters = store.load_clusters(store.latest_run_id() or 0)
        assert clusters == []

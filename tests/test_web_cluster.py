from __future__ import annotations

from pathlib import Path

import numpy as np
from fastapi.testclient import TestClient
from PIL import Image

from nga_tools.image_cluster import ClusterParams, run_image_cluster
from nga_tools.image_cluster.store import ImageClusterStore
from nga_tools.web.cluster_data import (
    read_cluster_detail,
    read_cluster_stats,
    read_clusters,
)
from nga_tools.web.server import create_app


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


def _prepare_clusters(tmp_path: Path) -> int:
    output_dir = tmp_path / "output"
    images_dir = output_dir / "images_unique"
    images_dir.mkdir(parents=True)
    _make_subject_image(images_dir / "white.png", (255, 255, 255), seed=1)
    _make_subject_image(images_dir / "black.png", (0, 0, 0), seed=1)
    result = run_image_cluster(
        output_dir, ClusterParams(threshold=12, workers=1)
    )
    return result.run_id


class WebClusterTest:
    def test_read_clusters_returns_items(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)

        result = read_clusters(
            output_dir, run_id=None, min_size=2, offset=0, limit=100
        )

        assert result["runId"] == run_id
        assert result["total"] >= 1
        assert len(result["items"]) >= 1
        item = result["items"][0]
        assert item["memberCount"] >= 2
        assert item["sourceRelativePath"].startswith("images_unique/")

    def test_read_clusters_no_data(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir(parents=True)

        result = read_clusters(
            output_dir, run_id=None, min_size=2, offset=0, limit=100
        )

        assert result["runId"] is None
        assert result["items"] == []
        assert result["total"] == 0

    def test_read_cluster_detail(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)
        listing = read_clusters(
            output_dir, run_id=None, min_size=2, offset=0, limit=100
        )
        cluster_id = listing["items"][0]["clusterId"]

        detail = read_cluster_detail(
            output_dir, run_id=None, cluster_id=cluster_id
        )

        assert detail["runId"] == run_id
        assert detail["cluster"] is not None
        members = detail["cluster"]["members"]
        assert len(members) >= 2
        for member in members:
            assert member["fileUrl"].startswith("/api/files/images_unique/")
        sources = [m for m in members if m["isSourceCandidate"]]
        assert len(sources) == 1

    def test_read_cluster_detail_missing(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        _prepare_clusters(tmp_path)

        detail = read_cluster_detail(
            output_dir, run_id=None, cluster_id=99999
        )

        assert detail["cluster"] is None

    def test_read_cluster_stats(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)

        stats = read_cluster_stats(output_dir, run_id=None)

        assert stats["runId"] == run_id
        assert stats["totalClusters"] >= 1
        assert stats["totalImages"] >= 2
        assert stats["maxClusterSize"] >= 2

    def test_api_clusters_endpoint(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/clusters", params={"min_size": 2})

        assert response.status_code == 200
        body = response.json()
        assert body["runId"] == run_id
        assert body["total"] >= 1

    def test_api_cluster_detail_endpoint(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        _prepare_clusters(tmp_path)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )
        listing = client.get("/api/clusters", params={"min_size": 2}).json()
        cluster_id = listing["items"][0]["clusterId"]

        response = client.get(f"/api/clusters/{cluster_id}")

        assert response.status_code == 200
        body = response.json()
        assert body["cluster"]["clusterId"] == cluster_id
        assert len(body["cluster"]["members"]) >= 2

    def test_api_cluster_stats_endpoint(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/clusters/stats")

        assert response.status_code == 200
        body = response.json()
        assert body["runId"] == run_id
        assert body["totalClusters"] >= 1

    def test_api_clusters_with_explicit_run_id(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        run_id = _prepare_clusters(tmp_path)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/clusters", params={"run_id": run_id})

        assert response.status_code == 200
        assert response.json()["runId"] == run_id

    def test_api_clusters_pagination(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        _prepare_clusters(tmp_path)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        page1 = client.get(
            "/api/clusters", params={"min_size": 2, "offset": 0, "limit": 1}
        ).json()
        page2 = client.get(
            "/api/clusters", params={"min_size": 2, "offset": 1, "limit": 1}
        ).json()

        assert page1["limit"] == 1
        assert len(page1["items"]) <= 1
        if page2["items"]:
            assert page1["items"][0]["clusterId"] != page2["items"][0]["clusterId"]

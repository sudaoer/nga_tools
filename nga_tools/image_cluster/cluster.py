from __future__ import annotations

import math
from dataclasses import dataclass

from nga_tools.image_cluster.features import (
    ImageFeatures,
    color_distance,
    hamming_distance,
)
from nga_tools.image_cluster.lsh import CandidatePair


@dataclass(frozen=True)
class ClusterMember:
    relative_path: str
    is_source_candidate: bool


@dataclass(frozen=True)
class Cluster:
    cluster_id: int
    members: list[ClusterMember]


class UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent: dict[str, str] = {item: item for item in items}
        self._rank: dict[str, int] = {item: 0 for item in items}

    def find(self, x: str) -> str:
        root = x
        while self._parent[root] != root:
            root = self._parent[root]
        current = x
        while self._parent[current] != root:
            self._parent[current], current = root, self._parent[current]
        return root

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx == ry:
            return
        if self._rank[rx] < self._rank[ry]:
            rx, ry = ry, rx
        self._parent[ry] = rx
        if self._rank[rx] == self._rank[ry]:
            self._rank[rx] += 1

    def groups(self) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for item in self._parent:
            root = self.find(item)
            result.setdefault(root, []).append(item)
        return result


_FORMAT_SCORES = {
    "png": 20.0,
    "webp": 10.0,
    "jpg": 0.0,
    "jpeg": 0.0,
    "gif": 5.0,
    "bmp": 0.0,
    "heif": 15.0,
    "heic": 15.0,
    "avif": 15.0,
    "jxl": 15.0,
}


def _extension(relative_path: str) -> str:
    dot = relative_path.rfind(".")
    if dot < 0:
        return ""
    return relative_path[dot + 1 :].lower()


def source_score(features: ImageFeatures) -> float:
    score = 0.0
    if features.has_alpha:
        score += 100.0
    pixels = max(features.width * features.height, 1)
    score += math.log10(pixels)
    score += math.log10(max(features.size, 1))
    score += _FORMAT_SCORES.get(_extension(features.relative_path), 0.0)
    return score


def build_clusters(
    features: dict[str, ImageFeatures],
    pairs: list[CandidatePair],
    threshold: int,
    min_cluster_size: int,
    dhash_threshold: int = 2,
    color_threshold: float = 0.05,
) -> list[Cluster]:
    groups = build_coarse_groups(
        features,
        pairs,
        threshold,
        dhash_threshold,
        color_threshold,
    )
    return clusters_from_groups(groups, features, min_cluster_size)


def build_coarse_groups(
    features: dict[str, ImageFeatures],
    pairs: list[CandidatePair],
    threshold: int,
    dhash_threshold: int = 2,
    color_threshold: float = 0.05,
) -> list[list[str]]:
    if not features:
        return []

    paths = list(features.keys())
    uf = UnionFind(paths)
    for pair in pairs:
        fa = features.get(pair.a)
        fb = features.get(pair.b)
        if fa is None or fb is None:
            continue
        phash_dist = hamming_distance(fa.phash, fb.phash)
        if phash_dist > threshold:
            continue
        dhash_dist = hamming_distance(fa.dhash, fb.dhash)
        if dhash_dist > dhash_threshold:
            continue
        cdist = color_distance(fa.color_histogram, fb.color_histogram)
        if cdist > color_threshold:
            continue
        uf.union(pair.a, pair.b)

    groups = [sorted(members) for members in uf.groups().values()]
    groups.sort(key=lambda group: (-len(group), group[0]))
    return groups


def clusters_from_groups(
    groups: list[list[str]],
    features: dict[str, ImageFeatures],
    min_cluster_size: int,
) -> list[Cluster]:
    filtered = [
        sorted(members)
        for members in groups
        if len(members) >= min_cluster_size
    ]
    filtered.sort(key=lambda g: (-len(g), g[0]))

    clusters: list[Cluster] = []
    for cluster_id, members in enumerate(filtered, start=1):
        member_features = [
            (path, features[path]) for path in members if path in features
        ]
        best_path = max(
            member_features,
            key=lambda item: source_score(item[1]),
        )[0]
        cluster_members = [
            ClusterMember(
                relative_path=path,
                is_source_candidate=(path == best_path),
            )
            for path, _ in member_features
        ]
        clusters.append(Cluster(cluster_id=cluster_id, members=cluster_members))

    return clusters

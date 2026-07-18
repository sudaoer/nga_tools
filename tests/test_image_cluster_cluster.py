from __future__ import annotations

from nga_tools.image_cluster.cluster import (
    Cluster,
    ClusterMember,
    UnionFind,
    build_clusters,
    source_score,
)
from nga_tools.image_cluster.features import ImageFeatures
from nga_tools.image_cluster.lsh import CandidatePair


def _make_features(
    path: str,
    phash: str = "0000000000000000",
    dhash: str = "0000000000000000",
    has_alpha: bool = False,
    color_histogram: str = "",
    size: int = 1000,
    width: int = 100,
    height: int = 100,
) -> ImageFeatures:
    return ImageFeatures(
        relative_path=path,
        size=size,
        mtime_ns=0,
        phash=phash,
        dhash=dhash,
        has_alpha=has_alpha,
        bg_color=None,
        trimmed=False,
        color_histogram=color_histogram,
        width=width,
        height=height,
    )


def test_union_find_basic() -> None:
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("c", "d")
    groups = uf.groups()
    assert len(groups) == 2
    members = sorted([sorted(g) for g in groups.values()])
    assert members == [["a", "b"], ["c", "d"]]


def test_union_find_path_compression() -> None:
    uf = UnionFind(["a", "b", "c", "d"])
    uf.union("a", "b")
    uf.union("b", "c")
    uf.union("c", "d")
    assert uf.find("a") == uf.find("d")


def test_build_clusters_merges_within_threshold() -> None:
    features = {
        "a.png": _make_features("a.png", phash="ffff0000ffff0000"),
        "b.png": _make_features("b.png", phash="ffff0000ffff0001"),
        "c.png": _make_features("c.png", phash="0000ffff0000ffff"),
    }
    pairs = [
        CandidatePair(a="a.png", b="b.png"),
        CandidatePair(a="a.png", b="c.png"),
        CandidatePair(a="b.png", b="c.png"),
    ]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    assert len(clusters) == 1
    assert len(clusters[0].members) == 2
    paths = [m.relative_path for m in clusters[0].members]
    assert paths == ["a.png", "b.png"]


def test_build_clusters_respects_threshold() -> None:
    features = {
        "a.png": _make_features("a.png", phash="ffffffffffffffff"),
        "b.png": _make_features("b.png", phash="0000000000000000"),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)
    assert clusters == []


def test_build_clusters_respects_dhash_threshold() -> None:
    features = {
        "a.png": _make_features(
            "a.png", phash="ffff0000ffff0000", dhash="ffffffffffffffff"
        ),
        "b.png": _make_features(
            "b.png", phash="ffff0000ffff0000", dhash="0000000000000000"
        ),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(
        features, pairs, threshold=8, min_cluster_size=2, dhash_threshold=2
    )
    assert clusters == []


def test_build_clusters_merges_when_dhash_within_threshold() -> None:
    features = {
        "a.png": _make_features(
            "a.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0000"
        ),
        "b.png": _make_features(
            "b.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0001"
        ),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(
        features, pairs, threshold=8, min_cluster_size=2, dhash_threshold=2
    )
    assert len(clusters) == 1
    assert len(clusters[0].members) == 2


def test_build_clusters_respects_color_threshold() -> None:
    hist_a = ",".join("0.5" if i == 0 else "0.0" for i in range(32))
    hist_b = ",".join("0.5" if i == 1 else "0.0" for i in range(32))
    features = {
        "a.png": _make_features(
            "a.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0000",
            color_histogram=hist_a,
        ),
        "b.png": _make_features(
            "b.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0000",
            color_histogram=hist_b,
        ),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(
        features, pairs, threshold=8, min_cluster_size=2,
        dhash_threshold=2, color_threshold=0.5,
    )
    assert clusters == []


def test_build_clusters_merges_when_color_within_threshold() -> None:
    hist_a = ",".join("0.5" if i == 0 else "0.0" for i in range(32))
    hist_b = ",".join("0.4" if i == 0 else "0.1" if i == 1 else "0.0" for i in range(32))
    features = {
        "a.png": _make_features(
            "a.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0000",
            color_histogram=hist_a,
        ),
        "b.png": _make_features(
            "b.png", phash="ffff0000ffff0000", dhash="ffff0000ffff0000",
            color_histogram=hist_b,
        ),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(
        features, pairs, threshold=8, min_cluster_size=2,
        dhash_threshold=2, color_threshold=0.5,
    )
    assert len(clusters) == 1
    assert len(clusters[0].members) == 2


def test_build_clusters_empty_histogram_skips_color_check() -> None:
    features = {
        "a.png": _make_features(
            "a.png", phash="ffff0000ffff0000", color_histogram="",
        ),
        "b.png": _make_features(
            "b.png", phash="ffff0000ffff0000", color_histogram="",
        ),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(
        features, pairs, threshold=8, min_cluster_size=2,
        color_threshold=0.01,
    )
    assert len(clusters) == 1


def test_build_clusters_min_size_filter() -> None:
    features = {
        "a.png": _make_features("a.png", phash="ffff0000ffff0000"),
        "b.png": _make_features("b.png", phash="ffff0000ffff0000"),
        "c.png": _make_features("c.png", phash="0000ffff0000ffff"),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    assert len(clusters) == 1
    assert len(clusters[0].members) == 2


def test_clusters_sorted_by_size_desc() -> None:
    features = {
        f"img{i}.png": _make_features(
            f"img{i}.png", phash="ffff0000ffff0000"
        )
        for i in range(5)
    }
    pairs = [
        CandidatePair(a="img0.png", b="img1.png"),
        CandidatePair(a="img2.png", b="img3.png"),
        CandidatePair(a="img3.png", b="img4.png"),
    ]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    assert len(clusters) == 2
    assert len(clusters[0].members) == 3
    assert len(clusters[1].members) == 2
    assert clusters[0].cluster_id == 1
    assert clusters[1].cluster_id == 2


def test_source_candidate_prefers_alpha() -> None:
    features = {
        "a.png": _make_features("a.png", has_alpha=False, size=5000),
        "b.png": _make_features("b.png", has_alpha=True, size=5000),
    }
    pairs = [CandidatePair(a="a.png", b="b.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    assert len(clusters) == 1
    source = [m for m in clusters[0].members if m.is_source_candidate]
    assert len(source) == 1
    assert source[0].relative_path == "b.png"


def test_source_candidate_prefers_larger_pixels() -> None:
    features = {
        "small.png": _make_features("small.png", width=100, height=100, size=1000),
        "large.png": _make_features("large.png", width=500, height=500, size=1000),
    }
    pairs = [CandidatePair(a="small.png", b="large.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    source = [m for m in clusters[0].members if m.is_source_candidate]
    assert source[0].relative_path == "large.png"


def test_source_candidate_prefers_png_format() -> None:
    features = {
        "a.jpg": _make_features("a.jpg", size=1000),
        "b.png": _make_features("b.png", size=1000),
    }
    pairs = [CandidatePair(a="a.jpg", b="b.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=2)

    source = [m for m in clusters[0].members if m.is_source_candidate]
    assert source[0].relative_path == "b.png"


def test_source_score_values() -> None:
    alpha_png = source_score(
        _make_features("a.png", has_alpha=True, width=500, height=500, size=10000)
    )
    small_jpg = source_score(
        _make_features("a.jpg", has_alpha=False, width=100, height=100, size=100)
    )
    assert alpha_png > small_jpg


def test_empty_features_returns_empty() -> None:
    assert build_clusters({}, [], threshold=8, min_cluster_size=2) == []


def test_missing_pair_member_skipped() -> None:
    features = {
        "a.png": _make_features("a.png", phash="ffff0000ffff0000"),
    }
    pairs = [CandidatePair(a="a.png", b="missing.png")]
    clusters = build_clusters(features, pairs, threshold=8, min_cluster_size=1)
    assert len(clusters) == 1
    assert len(clusters[0].members) == 1
    assert clusters[0].members[0].is_source_candidate is True

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from nga_tools.image_cluster.cluster import Cluster, ClusterMember
from nga_tools.image_cluster.detail import (
    DETAIL_SCORE_ALGORITHM,
    DetailPairScore,
)
from nga_tools.image_cluster.features import ImageFeatures
from nga_tools.image_cluster.store import (
    IMAGE_CLUSTERS_FILENAME,
    ImageClusterStore,
)
from nga_tools.storage import UnsupportedStorageFormatError


def _make_features(path: str, **overrides: object) -> ImageFeatures:
    defaults: dict[str, object] = {
        "relative_path": path,
        "size": 1000,
        "mtime_ns": 12345,
        "phash": "ffff0000ffff0000",
        "dhash": "0000ffff0000ffff",
        "has_alpha": False,
        "bg_color": None,
        "trimmed": False,
        "color_histogram": "",
        "width": 100,
        "height": 100,
    }
    defaults.update(overrides)
    return ImageFeatures(**defaults)  # type: ignore[arg-type]


def test_ensure_store_creates_tables(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    assert store.db_path.is_file()
    with closing(sqlite3.connect(store.db_path)) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    assert {
        "storage_metadata",
        "image_features",
        "cluster_runs",
        "cluster_members",
        "detail_pair_scores",
    } <= tables


def test_ensure_store_idempotent(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()
    store.ensure_store()
    assert store.db_path.is_file()


def test_read_methods_do_not_create_missing_store(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)

    assert store.load_all_features() == {}
    assert store.latest_run_id() is None
    assert not store.db_path.exists()


def test_upsert_and_load_features(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    features = [
        _make_features("a.png", bg_color=(255, 255, 255)),
        _make_features("b.png", has_alpha=True, phash="aaaa0000aaaa0000"),
    ]
    store.upsert_features(features)

    loaded = store.load_all_features()
    assert set(loaded.keys()) == {"a.png", "b.png"}
    assert loaded["a.png"].bg_color == (255, 255, 255)
    assert loaded["b.png"].has_alpha is True
    assert loaded["b.png"].phash == "aaaa0000aaaa0000"


def test_upsert_features_replaces_existing(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    store.upsert_features([_make_features("a.png", size=1000)])
    store.upsert_features([_make_features("a.png", size=2000)])

    loaded = store.load_all_features()
    assert loaded["a.png"].size == 2000


def test_load_feature_fingerprints(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    store.upsert_features(
        [
            _make_features("a.png", size=100, mtime_ns=111),
            _make_features("b.png", size=200, mtime_ns=222),
        ]
    )

    fingerprints = store.load_feature_fingerprints()
    assert fingerprints == {"a.png": (100, 111), "b.png": (200, 222)}


def test_delete_features(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    store.upsert_features(
        [_make_features("a.png"), _make_features("b.png"), _make_features("c.png")]
    )
    store.delete_features({"a.png", "c.png"})

    loaded = store.load_all_features()
    assert set(loaded.keys()) == {"b.png"}


def test_detail_pair_score_cache_validates_fingerprints_and_algorithm(
    tmp_path: Path,
) -> None:
    store = ImageClusterStore(tmp_path)
    features = {
        "a.png": _make_features("a.png", size=100, mtime_ns=111),
        "b.png": _make_features("b.png", size=200, mtime_ns=222),
    }
    store.upsert_features(list(features.values()))
    store.upsert_detail_pair_scores(
        [
            DetailPairScore(
                path_a="a.png",
                path_b="b.png",
                size_a=100,
                mtime_ns_a=111,
                size_b=200,
                mtime_ns_b=222,
                algorithm=DETAIL_SCORE_ALGORITHM,
                score=0.125,
            )
        ]
    )

    assert store.load_detail_pair_scores(
        DETAIL_SCORE_ALGORITHM, features
    ) == {("a.png", "b.png"): 0.125}
    assert store.load_detail_pair_scores("other-algorithm", features) == {}

    changed = dict(features)
    changed["a.png"] = _make_features("a.png", size=101, mtime_ns=111)
    assert store.load_detail_pair_scores(
        DETAIL_SCORE_ALGORITHM, changed
    ) == {}


def test_delete_features_also_deletes_detail_pair_scores(
    tmp_path: Path,
) -> None:
    store = ImageClusterStore(tmp_path)
    features = {
        "a.png": _make_features("a.png", size=100, mtime_ns=111),
        "b.png": _make_features("b.png", size=200, mtime_ns=222),
    }
    store.upsert_features(list(features.values()))
    store.upsert_detail_pair_scores(
        [
            DetailPairScore(
                path_a="a.png",
                path_b="b.png",
                size_a=100,
                mtime_ns_a=111,
                size_b=200,
                mtime_ns_b=222,
                algorithm=DETAIL_SCORE_ALGORITHM,
                score=0.1,
            )
        ]
    )

    store.delete_features({"a.png"})

    assert store.load_detail_pair_scores(
        DETAIL_SCORE_ALGORITHM, features
    ) == {}


def test_ensure_store_repairs_missing_current_table(
    tmp_path: Path,
) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()
    store.upsert_features([_make_features("a.png")])
    run_id = store.save_run({"threshold": 1}, [])
    with closing(sqlite3.connect(store.db_path)) as connection:
        connection.execute("DROP TABLE detail_pair_scores")
        connection.commit()

    with pytest.raises(UnsupportedStorageFormatError):
        store.load_detail_pair_scores(DETAIL_SCORE_ALGORITHM, {})
    with closing(sqlite3.connect(store.db_path)) as connection:
        table_before_ensure = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'detail_pair_scores'"
        ).fetchone()
    assert table_before_ensure is None

    store.ensure_store()

    assert store.load_all_features()["a.png"].size == 1000
    assert store.latest_run_id() == run_id
    with closing(sqlite3.connect(store.db_path)) as connection:
        table = connection.execute(
            "SELECT 1 FROM sqlite_schema "
            "WHERE type = 'table' AND name = 'detail_pair_scores'"
        ).fetchone()
    assert table == (1,)


def test_save_run_and_load_clusters(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    clusters = [
        Cluster(
            cluster_id=1,
            members=[
                ClusterMember(relative_path="a.png", is_source_candidate=True),
                ClusterMember(relative_path="b.png", is_source_candidate=False),
            ],
        ),
        Cluster(
            cluster_id=2,
            members=[
                ClusterMember(relative_path="c.png", is_source_candidate=True),
            ],
        ),
    ]
    run_id = store.save_run({"threshold": 8}, clusters)

    assert run_id >= 1
    assert store.latest_run_id() == run_id

    loaded = store.load_clusters(run_id)
    assert len(loaded) == 2
    assert loaded[0].cluster_id == 1
    assert len(loaded[0].members) == 2
    assert loaded[0].members[0].is_source_candidate is True
    assert loaded[0].members[1].is_source_candidate is False
    assert loaded[1].cluster_id == 2


def test_load_run_params(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()

    run_id = store.save_run({"threshold": 8, "bands": 4}, [])
    params = store.load_run_params(run_id)
    assert params == {"threshold": 8, "bands": 4}


def test_readonly_access(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    store.ensure_store()
    store.upsert_features([_make_features("a.png")])

    with closing(store._connect_readonly()) as conn:
        row = conn.execute(
            "SELECT relative_path FROM image_features"
        ).fetchone()
    assert row is not None and row[0] == "a.png"


def test_corrupt_database_raises(tmp_path: Path) -> None:
    store = ImageClusterStore(tmp_path)
    db_path = tmp_path / IMAGE_CLUSTERS_FILENAME
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.write_bytes(b"not a database")
    with pytest.raises((sqlite3.DatabaseError, UnsupportedStorageFormatError)):
        store.ensure_store()

from __future__ import annotations

import datetime
import json
import math
import sqlite3
import threading
from contextlib import closing
from pathlib import Path
from typing import cast

from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.image_cluster.cluster import Cluster, ClusterMember
from nga_tools.image_cluster.detail import DetailPairScore, PairKey
from nga_tools.image_cluster.features import ImageFeatures
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import (
    require_exact_columns,
    require_table_names,
)

IMAGE_CLUSTERS_FILENAME = "image_clusters.sqlite3"

_FEATURES_COLUMNS = (
    ("relative_path", "TEXT"),
    ("size", "INTEGER"),
    ("mtime_ns", "INTEGER"),
    ("phash", "TEXT"),
    ("dhash", "TEXT"),
    ("has_alpha", "INTEGER"),
    ("bg_color", "TEXT"),
    ("trimmed", "INTEGER"),
    ("color_histogram", "TEXT"),
    ("width", "INTEGER"),
    ("height", "INTEGER"),
    ("updated_at", "TEXT"),
)

_RUNS_COLUMNS = (
    ("run_id", "INTEGER"),
    ("created_at", "TEXT"),
    ("params", "TEXT"),
)

_MEMBERS_COLUMNS = (
    ("run_id", "INTEGER"),
    ("cluster_id", "INTEGER"),
    ("ordinal", "INTEGER"),
    ("relative_path", "TEXT"),
    ("is_source_candidate", "INTEGER"),
)

_DETAIL_PAIR_SCORES_COLUMNS = (
    ("path_a", "TEXT"),
    ("path_b", "TEXT"),
    ("size_a", "INTEGER"),
    ("mtime_ns_a", "INTEGER"),
    ("size_b", "INTEGER"),
    ("mtime_ns_b", "INTEGER"),
    ("algorithm", "TEXT"),
    ("score", "REAL"),
    ("updated_at", "TEXT"),
)

_CURRENT_TABLES = {
    "storage_metadata",
    "image_features",
    "cluster_runs",
    "cluster_members",
    "detail_pair_scores",
}

_LOCK = threading.RLock()


def _encode_bg_color(bg: tuple[int, int, int] | None) -> str | None:
    if bg is None:
        return None
    return f"{bg[0]},{bg[1]},{bg[2]}"


def _decode_bg_color(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str):
        return None
    parts = value.split(",")
    if len(parts) != 3:
        return None
    try:
        return int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return None


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _create_detail_pair_scores_table(
    connection: sqlite3.Connection,
) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS detail_pair_scores (
            path_a TEXT NOT NULL,
            path_b TEXT NOT NULL,
            size_a INTEGER NOT NULL,
            mtime_ns_a INTEGER NOT NULL,
            size_b INTEGER NOT NULL,
            mtime_ns_b INTEGER NOT NULL,
            algorithm TEXT NOT NULL,
            score REAL NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (path_a, path_b),
            CHECK (path_a < path_b),
            CHECK (score >= 0.0 AND score <= 1.0)
        )
        """
    )


class ImageClusterStore:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._db_path = (output_dir / IMAGE_CLUSTERS_FILENAME).resolve()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _require_base(
        self,
        connection: sqlite3.Connection,
        *,
        expected_tables: set[str],
    ) -> None:
        source = f"image_cluster {self._db_path}"
        require_storage_metadata(connection, role="image_cluster")
        require_table_names(
            connection,
            expected=expected_tables,
            source=source,
        )
        require_exact_columns(
            connection, "image_features", _FEATURES_COLUMNS, source=source
        )
        require_exact_columns(
            connection, "cluster_runs", _RUNS_COLUMNS, source=source
        )
        require_exact_columns(
            connection, "cluster_members", _MEMBERS_COLUMNS, source=source
        )

    def _require_current(self, connection: sqlite3.Connection) -> None:
        self._require_base(connection, expected_tables=_CURRENT_TABLES)
        require_exact_columns(
            connection,
            "detail_pair_scores",
            _DETAIL_PAIR_SCORES_COLUMNS,
            source=f"image_cluster {self._db_path}",
        )

    def ensure_store(self) -> Path:
        with _LOCK:
            new_database = not self._db_path.is_file()
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(
                sqlite3.connect(self._db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
            ) as connection:
                configure_connection(connection)
                with connection:
                    if new_database:
                        ensure_storage_metadata(connection, role="image_cluster")
                    else:
                        require_storage_metadata(
                            connection,
                            role="image_cluster",
                        )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS image_features (
                            relative_path TEXT PRIMARY KEY,
                            size INTEGER NOT NULL,
                            mtime_ns INTEGER NOT NULL,
                            phash TEXT NOT NULL,
                            dhash TEXT NOT NULL,
                            has_alpha INTEGER NOT NULL,
                            bg_color TEXT,
                            trimmed INTEGER NOT NULL,
                            color_histogram TEXT,
                            width INTEGER NOT NULL,
                            height INTEGER NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cluster_runs (
                            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            created_at TEXT NOT NULL,
                            params TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE IF NOT EXISTS cluster_members (
                            run_id INTEGER NOT NULL,
                            cluster_id INTEGER NOT NULL,
                            ordinal INTEGER NOT NULL,
                            relative_path TEXT NOT NULL,
                            is_source_candidate INTEGER NOT NULL,
                            PRIMARY KEY (run_id, cluster_id, ordinal)
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX IF NOT EXISTS idx_cluster_members_run
                        ON cluster_members(run_id, cluster_id)
                        """
                    )
                    _create_detail_pair_scores_table(connection)
                    self._require_current(connection)
            return self._db_path

    def _connect_writable(self) -> sqlite3.Connection:
        self.ensure_store()
        connection = sqlite3.connect(
            self._db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS
        )
        configure_connection(connection)
        return connection

    def _connect_readonly(self) -> sqlite3.Connection:
        if not self._db_path.is_file():
            raise FileNotFoundError(self._db_path)
        connection = sqlite3.connect(
            f"{self._db_path.as_uri()}?mode=ro",
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
        try:
            self._require_current(connection)
        except BaseException:
            connection.close()
            raise
        return connection

    def load_feature_fingerprints(self) -> dict[str, tuple[int, int]]:
        try:
            with closing(self._connect_readonly()) as connection:
                rows = connection.execute(
                    "SELECT relative_path, size, mtime_ns FROM image_features"
                ).fetchall()
        except (OSError, sqlite3.Error):
            return {}
        result: dict[str, tuple[int, int]] = {}
        for path, size, mtime_ns in rows:
            if (
                isinstance(path, str)
                and type(size) is int
                and type(mtime_ns) is int
            ):
                result[path] = (size, mtime_ns)
        return result

    def invalidate_features_if_hash_algorithm_changed(
        self, algorithm: str
    ) -> int:
        latest_run_id = self.latest_run_id()
        if latest_run_id is not None:
            params = self.load_run_params(latest_run_id)
            if isinstance(params, dict):
                params_dict = cast(dict[str, object], params)
                stored_algorithm = params_dict.get("hash_algorithm")
                if isinstance(stored_algorithm, str) and stored_algorithm == algorithm:
                    return 0

        with _LOCK:
            with closing(self._connect_writable()) as connection:
                with connection:
                    row = connection.execute(
                        "SELECT COUNT(*) FROM image_features"
                    ).fetchone()
                    count = (
                        row[0]
                        if row is not None and type(row[0]) is int
                        else 0
                    )
                    if count:
                        connection.execute("DELETE FROM image_features")
        return count

    def upsert_features(self, features: list[ImageFeatures]) -> None:
        if not features:
            return
        now = _now_iso()
        rows = [
            (
                f.relative_path,
                f.size,
                f.mtime_ns,
                f.phash,
                f.dhash,
                int(f.has_alpha),
                _encode_bg_color(f.bg_color),
                int(f.trimmed),
                f.color_histogram,
                f.width,
                f.height,
                now,
            )
            for f in features
        ]
        with _LOCK:
            with closing(self._connect_writable()) as connection:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO image_features (
                            relative_path, size, mtime_ns, phash, dhash,
                            has_alpha, bg_color, trimmed, color_histogram,
                            width, height, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(relative_path) DO UPDATE SET
                            size = excluded.size,
                            mtime_ns = excluded.mtime_ns,
                            phash = excluded.phash,
                            dhash = excluded.dhash,
                            has_alpha = excluded.has_alpha,
                            bg_color = excluded.bg_color,
                            trimmed = excluded.trimmed,
                            color_histogram = excluded.color_histogram,
                            width = excluded.width,
                            height = excluded.height,
                            updated_at = excluded.updated_at
                        """,
                        rows,
                    )

    def load_all_features(self) -> dict[str, ImageFeatures]:
        try:
            with closing(self._connect_readonly()) as connection:
                rows = connection.execute(
                    """
                    SELECT relative_path, size, mtime_ns, phash, dhash,
                           has_alpha, bg_color, trimmed, color_histogram,
                           width, height
                    FROM image_features
                    """
                ).fetchall()
        except (OSError, sqlite3.Error):
            return {}
        result: dict[str, ImageFeatures] = {}
        for row in rows:
            features = _row_to_features(row)
            if features is not None:
                result[features.relative_path] = features
        return result

    def load_detail_pair_scores(
        self,
        algorithm: str,
        features: dict[str, ImageFeatures],
    ) -> dict[PairKey, float]:
        try:
            with closing(self._connect_readonly()) as connection:
                rows = connection.execute(
                    """
                    SELECT path_a, path_b, size_a, mtime_ns_a,
                           size_b, mtime_ns_b, score
                    FROM detail_pair_scores
                    WHERE algorithm = ?
                    """,
                    (algorithm,),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return {}

        result: dict[PairKey, float] = {}
        for row in rows:
            if len(row) != 7:
                continue
            (
                path_a,
                path_b,
                size_a,
                mtime_ns_a,
                size_b,
                mtime_ns_b,
                score,
            ) = row
            if not isinstance(path_a, str) or not isinstance(path_b, str):
                continue
            if not all(
                type(value) is int
                for value in (size_a, mtime_ns_a, size_b, mtime_ns_b)
            ):
                continue
            if not isinstance(score, (int, float)):
                continue
            feature_a = features.get(path_a)
            feature_b = features.get(path_b)
            if feature_a is None or feature_b is None:
                continue
            if (feature_a.size, feature_a.mtime_ns) != (size_a, mtime_ns_a):
                continue
            if (feature_b.size, feature_b.mtime_ns) != (size_b, mtime_ns_b):
                continue
            score_value = float(score)
            if not math.isfinite(score_value) or not 0.0 <= score_value <= 1.0:
                continue
            result[(path_a, path_b)] = score_value
        return result

    def upsert_detail_pair_scores(
        self,
        scores: list[DetailPairScore],
    ) -> None:
        if not scores:
            return
        now = _now_iso()
        rows = [
            (
                score.path_a,
                score.path_b,
                score.size_a,
                score.mtime_ns_a,
                score.size_b,
                score.mtime_ns_b,
                score.algorithm,
                score.score,
                now,
            )
            for score in scores
        ]
        with _LOCK:
            with closing(self._connect_writable()) as connection:
                with connection:
                    connection.executemany(
                        """
                        INSERT INTO detail_pair_scores (
                            path_a, path_b, size_a, mtime_ns_a,
                            size_b, mtime_ns_b, algorithm, score, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(path_a, path_b) DO UPDATE SET
                            size_a = excluded.size_a,
                            mtime_ns_a = excluded.mtime_ns_a,
                            size_b = excluded.size_b,
                            mtime_ns_b = excluded.mtime_ns_b,
                            algorithm = excluded.algorithm,
                            score = excluded.score,
                            updated_at = excluded.updated_at
                        """,
                        rows,
                    )

    def delete_features(self, paths: set[str]) -> None:
        if not paths:
            return
        with _LOCK:
            try:
                with closing(self._connect_writable()) as connection:
                    with connection:
                        for chunk in iter_in_clause_chunks(sorted(paths)):
                            placeholders = ",".join("?" for _ in chunk)
                            connection.execute(
                                f"DELETE FROM image_features "
                                f"WHERE relative_path IN ({placeholders})",
                                chunk,
                            )
                            connection.execute(
                                "DELETE FROM detail_pair_scores "
                                f"WHERE path_a IN ({placeholders})",
                                chunk,
                            )
                            connection.execute(
                                "DELETE FROM detail_pair_scores "
                                f"WHERE path_b IN ({placeholders})",
                                chunk,
                            )
            except (OSError, sqlite3.Error):
                pass

    def save_run(
        self, params: object, clusters: list[Cluster]
    ) -> int:
        params_json = json.dumps(params, ensure_ascii=False, sort_keys=True)
        now = _now_iso()
        member_rows: list[tuple[int, int, int, str, int]] = []
        with _LOCK:
            with closing(self._connect_writable()) as connection:
                with connection:
                    cursor = connection.execute(
                        "INSERT INTO cluster_runs (created_at, params) VALUES (?, ?)",
                        (now, params_json),
                    )
                    run_id_obj = cursor.lastrowid
                    if run_id_obj is None:
                        raise RuntimeError("无法获取 cluster run_id")
                    run_id = run_id_obj
                    for cluster in clusters:
                        for ordinal, member in enumerate(cluster.members):
                            member_rows.append(
                                (
                                    run_id,
                                    cluster.cluster_id,
                                    ordinal,
                                    member.relative_path,
                                    int(member.is_source_candidate),
                                )
                            )
                    if member_rows:
                        connection.executemany(
                            """
                            INSERT INTO cluster_members (
                                run_id, cluster_id, ordinal,
                                relative_path, is_source_candidate
                            )
                            VALUES (?, ?, ?, ?, ?)
                            """,
                            member_rows,
                        )
            return run_id

    def latest_run_id(self) -> int | None:
        try:
            with closing(self._connect_readonly()) as connection:
                row = connection.execute(
                    "SELECT MAX(run_id) FROM cluster_runs"
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if not row or row[0] is None:
            return None
        value = row[0]
        return value if isinstance(value, int) else None

    def load_clusters(self, run_id: int) -> list[Cluster]:
        try:
            with closing(self._connect_readonly()) as connection:
                rows = connection.execute(
                    """
                    SELECT cluster_id, ordinal, relative_path, is_source_candidate
                    FROM cluster_members
                    WHERE run_id = ?
                    ORDER BY cluster_id, ordinal
                    """,
                    (run_id,),
                ).fetchall()
        except (OSError, sqlite3.Error):
            return []

        by_cluster: dict[int, list[ClusterMember]] = {}
        for cluster_id, _ordinal, relative_path, is_source in rows:
            if (
                isinstance(cluster_id, int)
                and isinstance(relative_path, str)
                and isinstance(is_source, int)
            ):
                by_cluster.setdefault(cluster_id, []).append(
                    ClusterMember(
                        relative_path=relative_path,
                        is_source_candidate=bool(is_source),
                    )
                )
        return [
            Cluster(cluster_id=cid, members=members)
            for cid, members in sorted(by_cluster.items())
        ]

    def load_run_params(self, run_id: int) -> object:
        try:
            with closing(self._connect_readonly()) as connection:
                row = connection.execute(
                    "SELECT params FROM cluster_runs WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return None
        if not row or row[0] is None:
            return None
        value = row[0]
        if not isinstance(value, str):
            return None
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return None


def _row_to_features(row: tuple[object, ...]) -> ImageFeatures | None:
    if len(row) < 11:
        return None
    (
        relative_path,
        size,
        mtime_ns,
        phash,
        dhash,
        has_alpha,
        bg_color,
        trimmed,
        color_histogram,
        width,
        height,
    ) = row
    if not isinstance(relative_path, str):
        return None
    if not isinstance(phash, str) or not isinstance(dhash, str):
        return None
    if not (
        isinstance(size, int)
        and isinstance(mtime_ns, int)
        and isinstance(has_alpha, int)
        and isinstance(trimmed, int)
        and isinstance(width, int)
        and isinstance(height, int)
    ):
        return None
    histogram_value: str = color_histogram if isinstance(color_histogram, str) else ""
    return ImageFeatures(
        relative_path=relative_path,
        size=size,
        mtime_ns=mtime_ns,
        phash=phash,
        dhash=dhash,
        has_alpha=bool(has_alpha),
        bg_color=_decode_bg_color(bg_color),
        trimmed=bool(trimmed),
        color_histogram=histogram_value,
        width=width,
        height=height,
    )

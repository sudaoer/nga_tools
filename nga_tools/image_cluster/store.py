from __future__ import annotations

import datetime
import json
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.image_cluster.cluster import Cluster, ClusterMember
from nga_tools.image_cluster.features import ImageFeatures
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import require_exact_columns, require_table_names

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


class ImageClusterStore:
    def __init__(self, output_dir: Path) -> None:
        self._output_dir = output_dir
        self._db_path = (output_dir / IMAGE_CLUSTERS_FILENAME).resolve()

    @property
    def db_path(self) -> Path:
        return self._db_path

    def _require_current(self, connection: sqlite3.Connection) -> None:
        source = f"image_cluster {self._db_path}"
        require_storage_metadata(connection, role="image_cluster")
        require_table_names(
            connection,
            expected={
                "storage_metadata",
                "image_features",
                "cluster_runs",
                "cluster_members",
            },
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

    def ensure_store(self) -> Path:
        with _LOCK:
            new_database = not self._db_path.is_file()
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(
                sqlite3.connect(self._db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
            ) as connection:
                configure_connection(connection)
                if new_database:
                    ensure_storage_metadata(connection, role="image_cluster")
                    connection.execute(
                        """
                        CREATE TABLE image_features (
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
                        CREATE TABLE cluster_runs (
                            run_id INTEGER PRIMARY KEY AUTOINCREMENT,
                            created_at TEXT NOT NULL,
                            params TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE TABLE cluster_members (
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
                        CREATE INDEX idx_cluster_members_run
                        ON cluster_members(run_id, cluster_id)
                        """
                    )
                else:
                    self._require_current(connection)
                connection.commit()
            return self._db_path

    def _connect_writable(self) -> sqlite3.Connection:
        self.ensure_store()
        connection = sqlite3.connect(
            self._db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS
        )
        configure_connection(connection)
        return connection

    def _connect_readonly(self) -> sqlite3.Connection:
        self.ensure_store()
        connection = sqlite3.connect(
            f"{self._db_path.as_uri()}?mode=ro",
            timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
            uri=True,
        )
        configure_readonly_connection(connection)
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

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import cast

from nga_tools.backup.image_index import (
    IMAGE_INDEX_FILENAME,
    ImageIndexStore,
    require_current_image_index,
)
from nga_tools.backup.image_validation_store import IMAGE_CACHE_FILENAME
from nga_tools.backup.archive_schema import require_current_archive_schema
from nga_tools.backup.audio_store import (
    AUDIO_INDEX_FILENAME,
    require_current_audio_index,
)
from nga_tools.backup.thread_stores import (
    ARCHIVE_CACHE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
)
from nga_tools.core.hashing import hash_object
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_readonly_connection,
)
from nga_tools.forum.thread_configs import (
    ThreadConfig,
    thread_config_aid,
    thread_config_tid,
)
from nga_tools.replay.state import InitialState


@dataclass(frozen=True, slots=True)
class ValidationStats:
    elapsed_seconds: float
    checked_archive_count: int
    compared_archive_count: int
    post_count: int
    floor_map_entry_count: int
    target_image_mapping_count: int
    image_mapping_mismatch_count: int
    target_audio_mapping_count: int = 0
    audio_mapping_mismatch_count: int = 0

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self))


def _open_readonly_connection(
    path: Path,
    *,
    immutable: bool = False,
) -> sqlite3.Connection:
    immutable_query = "&immutable=1" if immutable else ""
    uri = f"{path.resolve().as_uri()}?mode=ro{immutable_query}"
    connection = sqlite3.connect(
        uri,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        uri=True,
    )
    configure_readonly_connection(connection)
    return connection


def _quick_check(path: Path) -> None:
    with closing(_open_readonly_connection(path)) as connection:
        rows = connection.execute("PRAGMA quick_check").fetchall()
    if rows != [("ok",)]:
        raise RuntimeError(f"目标数据库quick_check失败：{path}: {rows}")


def _archive_dir(output: Path, thread_config: ThreadConfig) -> Path:
    tid = thread_config_tid(thread_config)
    aid = thread_config_aid(thread_config)
    return output / f"{tid}_{aid if aid is not None else 'all'}"


def _latest_post_signature(
    path: Path,
    *,
    immutable: bool,
) -> tuple[int, str]:
    with closing(
        _open_readonly_connection(path, immutable=immutable)
    ) as connection:
        require_current_archive_schema(connection, path)
        rows = connection.execute(
            """
            SELECT
                latest.lou,
                latest.pid,
                latest.source_hash,
                metadata.author_name,
                metadata.author_uid,
                metadata.postdate_json
            FROM (
                SELECT
                    lou,
                    pid,
                    source_hash,
                    ROW_NUMBER() OVER (
                        PARTITION BY lou
                        ORDER BY last_seen_at DESC, id DESC
                    ) AS row_number
                FROM post_versions
            ) AS latest
            LEFT JOIN post_latest_metadata AS metadata
                ON metadata.pid = latest.pid
                AND metadata.lou = latest.lou
            WHERE latest.row_number = 1
            ORDER BY latest.lou
            """
        ).fetchall()
    return len(rows), hash_object(rows)


def _floor_map_rows(
    path: Path,
    *,
    immutable: bool,
) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with closing(
        _open_readonly_connection(path, immutable=immutable)
    ) as connection:
        require_current_archive_schema(connection, path)
        entries = cast(
            list[tuple[object, ...]],
            connection.execute(
                """
                SELECT author_lou, pid, original_lou, original_pid
                FROM floor_map_entries
                ORDER BY author_lou
                """
            ).fetchall(),
        )
        candidates = cast(
            list[tuple[object, ...]],
            connection.execute(
                """
                SELECT author_lou, candidate_index, original_lou
                FROM floor_map_candidates
                ORDER BY author_lou, candidate_index
                """
            ).fetchall(),
        )
        return entries, candidates


def _post_version_count(path: Path, *, immutable: bool) -> int:
    with closing(
        _open_readonly_connection(path, immutable=immutable)
    ) as connection:
        require_current_archive_schema(connection, path)
        row = connection.execute("SELECT COUNT(*) FROM post_versions").fetchone()
    if row is None or type(row[0]) is not int:
        raise RuntimeError(f"无法读取帖子版本数：{path}")
    return row[0]


def _image_mappings(path: Path, *, immutable: bool) -> dict[str, str]:
    if not path.is_file():
        return {}
    with closing(
        _open_readonly_connection(path, immutable=immutable)
    ) as connection:
        require_current_image_index(connection, path)
        return dict(
            ImageIndexStore(path.parent).iter_mapping_rows(connection)
        )


def _audio_mappings(path: Path, *, immutable: bool) -> dict[str, str]:
    if not path.is_file():
        return {}
    with closing(
        _open_readonly_connection(path, immutable=immutable)
    ) as connection:
        require_current_audio_index(connection, path)
        rows = connection.execute(
            "SELECT url, unique_rel_path FROM audio_mappings"
        ).fetchall()
    mappings: dict[str, str] = {}
    for raw_url, raw_path in rows:
        if isinstance(raw_url, str) and isinstance(raw_path, str):
            mappings[raw_url] = raw_path
    return mappings


def validate_replay_output(
    source_output: Path,
    target_output: Path,
    thread_configs: list[ThreadConfig],
    initial_state: InitialState,
) -> ValidationStats:
    started = perf_counter()
    checked = 0
    compared = 0
    post_count = 0
    floor_count = 0
    for thread_config in thread_configs:
        source_dir = _archive_dir(source_output, thread_config)
        target_dir = _archive_dir(target_output, thread_config)
        source_archive = source_dir / "archive.sqlite3"
        target_archive = target_dir / "archive.sqlite3"
        if not target_archive.is_file():
            raise RuntimeError(f"重放运行后缺少目标归档：{target_archive}")
        _quick_check(target_archive)
        for auxiliary_filename in (
            ARCHIVE_STATE_DB_FILENAME,
            ARCHIVE_CACHE_DB_FILENAME,
        ):
            auxiliary_path = target_dir / auxiliary_filename
            if auxiliary_path.is_file():
                _quick_check(auxiliary_path)
        checked += 1
        target_signature = _latest_post_signature(
            target_archive,
            immutable=False,
        )
        post_count += target_signature[0]
        target_floor_entries, target_floor_candidates = _floor_map_rows(
            target_archive,
            immutable=False,
        )
        floor_count += len(target_floor_entries)
        if not source_archive.is_file():
            continue
        source_signature = _latest_post_signature(
            source_archive,
            immutable=True,
        )
        if target_signature != source_signature:
            raise RuntimeError(
                "重放目标有效帖子数量、正文或元数据与源归档不一致："
                f"{target_dir.name}"
            )
        source_floor_entries, source_floor_candidates = _floor_map_rows(
            source_archive,
            immutable=True,
        )
        if (
            target_floor_entries != source_floor_entries
            or target_floor_candidates != source_floor_candidates
        ):
            raise RuntimeError(f"重放目标楼层映射与源归档不一致：{target_dir.name}")
        if (
            initial_state == "warm"
            and _post_version_count(target_archive, immutable=False)
            != _post_version_count(source_archive, immutable=True)
        ):
            raise RuntimeError(f"暖状态重放产生了新的正文版本：{target_dir.name}")
        compared += 1

    target_image_index = target_output / IMAGE_INDEX_FILENAME
    if target_image_index.is_file():
        _quick_check(target_image_index)
    target_image_cache = target_output / IMAGE_CACHE_FILENAME
    if target_image_cache.is_file():
        _quick_check(target_image_cache)
    source_mappings = _image_mappings(
        source_output / IMAGE_INDEX_FILENAME,
        immutable=True,
    )
    target_mappings = _image_mappings(target_image_index, immutable=False)
    mismatch_count = sum(
        source_mappings.get(url) != relative_path
        for url, relative_path in target_mappings.items()
    )
    if mismatch_count:
        raise RuntimeError(
            f"重放目标有{mismatch_count}条成功图片映射与源索引不一致。"
        )

    target_audio_index = target_output / AUDIO_INDEX_FILENAME
    if target_audio_index.is_file():
        _quick_check(target_audio_index)
    source_audio_mappings = _audio_mappings(
        source_output / AUDIO_INDEX_FILENAME,
        immutable=True,
    )
    target_audio_mappings = _audio_mappings(
        target_audio_index,
        immutable=False,
    )
    audio_mismatch_count = sum(
        source_audio_mappings.get(url) != relative_path
        for url, relative_path in target_audio_mappings.items()
    )
    if audio_mismatch_count:
        raise RuntimeError(
            f"重放目标有{audio_mismatch_count}条成功音频映射与源索引不一致。"
        )

    return ValidationStats(
        elapsed_seconds=perf_counter() - started,
        checked_archive_count=checked,
        compared_archive_count=compared,
        post_count=post_count,
        floor_map_entry_count=floor_count,
        target_image_mapping_count=len(target_mappings),
        image_mapping_mismatch_count=mismatch_count,
        target_audio_mapping_count=len(target_audio_mappings),
        audio_mapping_mismatch_count=audio_mismatch_count,
    )

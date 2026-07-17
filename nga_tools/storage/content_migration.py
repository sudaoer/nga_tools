from __future__ import annotations

import datetime
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from nga_tools.backup.archive_schema import require_current_archive_schema
from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.backup.content_codec import (
    decode_content,
    encode_content,
)
from nga_tools.core.atomic import (
    replace_file_atomically,
    replace_temp_file,
    temporary_sibling_path,
    write_json_atomically,
)
from nga_tools.core.hashing import hash_text


CONTENT_MIGRATION_BACKUP_DIRNAME = ".migration-backups"
CONTENT_MIGRATION_MANIFEST_FILENAME = "content-manifest.json"
_POST_VERSIONS_INDEX = "idx_post_versions_latest_covering"
_CONTENT_TABLE = "post_versions_content_migration"


@dataclass(frozen=True)
class ContentMigrationStats:
    path: Path
    version_count: int
    raw_content_bytes: int
    compressed_content_bytes: int
    database_bytes: int
    post_versions_bytes: int
    migrated: bool

    @property
    def saved_content_bytes(self) -> int:
        return self.raw_content_bytes - self.compressed_content_bytes


@dataclass(frozen=True)
class ContentMigrationResult:
    run_id: str | None
    migrated_count: int
    skipped_count: int
    failures: tuple[tuple[Path, str], ...]
    stats: tuple[ContentMigrationStats, ...]


@dataclass(frozen=True)
class ContentRollbackResult:
    run_id: str
    restored_count: int


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _new_run_id() -> str:
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def _database_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _connect_readonly(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_database_uri(path), uri=True)


def _table_columns(
    connection: sqlite3.Connection,
    table_name: str,
) -> set[str]:
    return {
        row[1]
        for row in connection.execute(
            f'PRAGMA table_info("{table_name}")'
        )
        if isinstance(row[1], str)
    }


def _post_version_rows(
    connection: sqlite3.Connection,
) -> Iterator[
    tuple[int, int, int, str, object, int, int, int, str, str, int]
]:
    required_columns = {
        "id",
        "pid",
        "lou",
        "source_hash",
        "content",
        "word_count_version",
        "word_count_chinese_chars",
        "word_count_chinese_with_punctuation",
        "first_seen_at",
        "last_seen_at",
        "seen_count",
    }
    columns = _table_columns(connection, "post_versions")
    if not required_columns <= columns:
        raise ValueError(
            "post_versions字段不完整，需先运行 backup migrate-layout："
            f"{sorted(columns)}"
        )
    rows = connection.execute(
        """
        SELECT
            id,
            pid,
            lou,
            source_hash,
            content,
            word_count_version,
            word_count_chinese_chars,
            word_count_chinese_with_punctuation,
            first_seen_at,
            last_seen_at,
            seen_count
        FROM post_versions
        ORDER BY id
        """
    )
    for row in rows:
        if len(row) != 11:
            raise ValueError(f"post_versions行字段数无效：{row!r}")
        yield cast(
            tuple[int, int, int, str, object, int, int, int, str, str, int],
            tuple(row),
        )


def _table_bytes(
    connection: sqlite3.Connection,
    table_name: str,
) -> int:
    try:
        row = connection.execute(
            "SELECT COALESCE(SUM(pgsize), 0) FROM dbstat WHERE name = ?",
            (table_name,),
        ).fetchone()
    except sqlite3.Error:
        return 0
    return row[0] if row is not None and type(row[0]) is int else 0


def inspect_content(path: Path, *, migrated: bool) -> ContentMigrationStats:
    if not path.is_file():
        raise FileNotFoundError(f"缺少{ARCHIVE_DB_FILENAME}：{path}")
    with closing(_connect_readonly(path)) as connection:
        require_current_archive_schema(connection, path)
        version_count = 0
        raw_content_bytes = 0
        compressed_content_bytes = 0
        for row in _post_version_rows(connection):
            (
                version_id,
                _pid,
                _lou,
                _source_hash,
                raw_content,
                _word_count_version,
                _word_count_chinese_chars,
                _word_count_chinese_with_punctuation,
                _first_seen_at,
                _last_seen_at,
                _seen_count,
            ) = row
            content = decode_content(
                raw_content,
                source=f"{path}帖子版本{version_id}正文",
            )
            raw_content_bytes += len(content.encode("utf-8"))
            compressed_content_bytes += len(encode_content(content))
            version_count += 1
        return ContentMigrationStats(
            path=path,
            version_count=version_count,
            raw_content_bytes=raw_content_bytes,
            compressed_content_bytes=compressed_content_bytes,
            database_bytes=path.stat().st_size,
            post_versions_bytes=_table_bytes(connection, "post_versions"),
            migrated=migrated,
        )


def _needs_migration(path: Path) -> bool:
    with closing(_connect_readonly(path)) as connection:
        require_current_archive_schema(connection, path)
        columns = {
            row[1]: row[2]
            for row in connection.execute("PRAGMA table_info(post_versions)")
            if isinstance(row[1], str)
        }
        if columns.get("content") != "BLOB":
            return True
        row = connection.execute(
            """
            SELECT 1
            FROM post_versions
            WHERE typeof(content) != 'blob'
            LIMIT 1
            """
        ).fetchone()
        return row is not None


def _snapshot_sqlite(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination.unlink()
    with (
        closing(_connect_readonly(source)) as source_connection,
        closing(sqlite3.connect(destination)) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _create_content_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        f"""
        CREATE TABLE {_CONTENT_TABLE} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pid INTEGER NOT NULL,
            lou INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            content BLOB NOT NULL CHECK(typeof(content) = 'blob'),
            word_count_version INTEGER NOT NULL DEFAULT 0,
            word_count_chinese_chars INTEGER NOT NULL DEFAULT 0,
            word_count_chinese_with_punctuation INTEGER NOT NULL DEFAULT 0,
            first_seen_at TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            seen_count INTEGER NOT NULL,
            UNIQUE(pid, lou, source_hash)
        )
        """
    )


def _rebuild_post_versions(path: Path, target: Path) -> None:
    _snapshot_sqlite(path, target)
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        rows = list(_post_version_rows(connection))
        _create_content_table(connection)
        connection.executemany(
            f"""
            INSERT INTO {_CONTENT_TABLE} (
                id,
                pid,
                lou,
                source_hash,
                content,
                word_count_version,
                word_count_chinese_chars,
                word_count_chinese_with_punctuation,
                first_seen_at,
                last_seen_at,
                seen_count
            )
            SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            """,
            (
                (
                    version_id,
                    pid,
                    lou,
                    source_hash,
                    encode_content(
                        decode_content(
                            raw_content,
                            source=f"{path}帖子版本{version_id}正文",
                        )
                    ),
                    word_count_version,
                    word_count_chinese_chars,
                    word_count_chinese_with_punctuation,
                    first_seen_at,
                    last_seen_at,
                    seen_count,
                )
                for (
                    version_id,
                    pid,
                    lou,
                    source_hash,
                    raw_content,
                    word_count_version,
                    word_count_chinese_chars,
                    word_count_chinese_with_punctuation,
                    first_seen_at,
                    last_seen_at,
                    seen_count,
                ) in rows
            ),
        )
        connection.execute("DROP TABLE post_versions")
        connection.execute(
            f"ALTER TABLE {_CONTENT_TABLE} RENAME TO post_versions"
        )
        connection.execute(
            f"""
            CREATE INDEX {_POST_VERSIONS_INDEX}
            ON post_versions(lou, last_seen_at DESC, id DESC, pid)
            """
        )
        connection.commit()
        connection.execute("VACUUM")
        connection.execute("PRAGMA foreign_keys = ON")
        _validate_connection(connection, target)


def _validate_connection(
    connection: sqlite3.Connection,
    path: Path,
) -> None:
    quick_check = connection.execute("PRAGMA quick_check").fetchone()
    if quick_check != ("ok",):
        raise ValueError(f"SQLite quick_check失败：{path}：{quick_check!r}")
    foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_key_errors:
        raise ValueError(
            f"SQLite foreign_key_check失败：{path}：{foreign_key_errors[:3]!r}"
        )
    if _needs_migration(path):
        raise ValueError(f"正文压缩迁移未完成：{path}")
    for row in _post_version_rows(connection):
        (
            version_id,
            _pid,
            _lou,
            source_hash,
            raw_content,
            _word_count_version,
            _word_count_chinese_chars,
            _word_count_chinese_with_punctuation,
            _first_seen_at,
            _last_seen_at,
            _seen_count,
        ) = row
        content = decode_content(
            raw_content,
            source=f"{path}帖子版本{version_id}正文",
        )
        if hash_text(content) != source_hash:
            raise ValueError(f"正文hash校验失败：{path} version_id={version_id}")


def _manifest_path(run_root: Path) -> Path:
    return run_root / CONTENT_MIGRATION_MANIFEST_FILENAME


def _write_manifest(run_root: Path, manifest: dict[str, object]) -> None:
    path = _manifest_path(run_root)
    write_json_atomically(
        path,
        manifest,
        indent=2,
        trailing_newline=True,
    )


def _load_manifest(path: Path) -> dict[str, object]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"正文迁移清单无效：{path}")
    return cast(dict[str, object], raw)


def _target_names(manifest: dict[str, object]) -> tuple[str, ...]:
    raw_targets = manifest.get("targets")
    if not isinstance(raw_targets, list):
        raise ValueError("正文迁移清单targets无效。")
    target_items = cast(list[object], raw_targets)
    if not all(isinstance(item, str) for item in target_items):
        raise ValueError("正文迁移清单targets无效。")
    return tuple(cast(str, item) for item in target_items)


def _find_resumable_run(
    output_root: Path,
    target_names: tuple[str, ...],
) -> tuple[Path, dict[str, object]] | None:
    backup_root = output_root / CONTENT_MIGRATION_BACKUP_DIRNAME
    if not backup_root.is_dir():
        return None
    for run_root in sorted(backup_root.iterdir(), reverse=True):
        manifest_path = _manifest_path(run_root)
        if not manifest_path.is_file():
            continue
        try:
            manifest = _load_manifest(manifest_path)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if (
            manifest.get("output_root") == str(output_root)
            and manifest.get("status") == "incomplete"
            and _target_names(manifest) == target_names
        ):
            return run_root, manifest
    return None


def _entry(manifest: dict[str, object], target_name: str) -> dict[str, object]:
    raw_entries = manifest.get("entries")
    if not isinstance(raw_entries, dict):
        raise ValueError("正文迁移清单entries无效。")
    entries = cast(dict[object, object], raw_entries)
    raw_entry = entries.get(target_name)
    if not isinstance(raw_entry, dict):
        raise ValueError(f"正文迁移清单缺少目标：{target_name}")
    return cast(dict[str, object], raw_entry)


def _new_manifest(
    output_root: Path,
    target_names: tuple[str, ...],
) -> tuple[Path, dict[str, object]]:
    run_root = (
        output_root
        / CONTENT_MIGRATION_BACKUP_DIRNAME
        / _new_run_id()
    )
    run_root.mkdir(parents=True, exist_ok=False)
    manifest: dict[str, object] = {
        "version": 1,
        "kind": "post_versions_content_zstd",
        "output_root": str(output_root),
        "created_at": _now_utc_iso(),
        "updated_at": _now_utc_iso(),
        "status": "incomplete",
        "targets": list(target_names),
        "entries": {
            target_name: {"status": "pending"}
            for target_name in target_names
        },
    }
    _write_manifest(run_root, manifest)
    return run_root, manifest


def _original_backup_path(run_root: Path, target_name: str) -> Path:
    return run_root / "files" / target_name / ARCHIVE_DB_FILENAME


def migrate_content(
    output_root: Path,
    thread_folders: list[Path],
    *,
    dry_run: bool = False,
) -> ContentMigrationResult:
    resolved_root = output_root.resolve()
    ordered_folders = sorted(
        {folder.resolve() for folder in thread_folders},
        key=lambda folder: folder.name,
    )
    for folder in ordered_folders:
        if folder.parent != resolved_root:
            raise ValueError(f"迁移目标不在output根目录内：{folder}")
    target_names = tuple(folder.name for folder in ordered_folders)
    if dry_run:
        stats: list[ContentMigrationStats] = []
        failures: list[tuple[Path, str]] = []
        skipped_count = 0
        for folder in ordered_folders:
            path = folder / ARCHIVE_DB_FILENAME
            try:
                needs_migration = _needs_migration(path)
                stats.append(inspect_content(path, migrated=not needs_migration))
                if not needs_migration:
                    skipped_count += 1
            except Exception as error:
                failures.append((path, f"{type(error).__name__}: {error}"))
        return ContentMigrationResult(
            run_id=None,
            migrated_count=0,
            skipped_count=skipped_count,
            failures=tuple(failures),
            stats=tuple(stats),
        )

    resumable = _find_resumable_run(resolved_root, target_names)
    if resumable is None:
        run_root, manifest = _new_manifest(resolved_root, target_names)
    else:
        run_root, manifest = resumable
    failures: list[tuple[Path, str]] = []
    stats: list[ContentMigrationStats] = []
    migrated_count = 0
    skipped_count = 0
    for folder in ordered_folders:
        path = folder / ARCHIVE_DB_FILENAME
        entry = _entry(manifest, folder.name)
        if entry.get("status") == "completed":
            skipped_count += 1
            stats.append(inspect_content(path, migrated=True))
            continue
        try:
            if not _needs_migration(path):
                entry["status"] = "completed"
                skipped_count += 1
                stats.append(inspect_content(path, migrated=True))
            else:
                backup_path = _original_backup_path(run_root, folder.name)
                if not backup_path.is_file():
                    _snapshot_sqlite(path, backup_path)
                temp_path = temporary_sibling_path(path)
                try:
                    _rebuild_post_versions(path, temp_path)
                    replace_temp_file(temp_path, path)
                finally:
                    if temp_path.exists():
                        temp_path.unlink()
                entry["status"] = "completed"
                migrated_count += 1
                stats.append(inspect_content(path, migrated=True))
        except Exception as error:
            error_text = f"{type(error).__name__}: {error}"
            entry["status"] = "failed"
            entry["error"] = error_text
            failures.append((path, error_text))
        manifest["updated_at"] = _now_utc_iso()
        _write_manifest(run_root, manifest)
    manifest["status"] = "completed" if not failures else "incomplete"
    manifest["updated_at"] = _now_utc_iso()
    _write_manifest(run_root, manifest)
    return ContentMigrationResult(
        run_id=run_root.name,
        migrated_count=migrated_count,
        skipped_count=skipped_count,
        failures=tuple(failures),
        stats=tuple(stats),
    )


def rollback_content(
    output_root: Path,
    run_id: str,
) -> ContentRollbackResult:
    resolved_root = output_root.resolve()
    if not run_id or Path(run_id).name != run_id:
        raise ValueError(f"正文迁移运行ID无效：{run_id!r}")
    run_root = resolved_root / CONTENT_MIGRATION_BACKUP_DIRNAME / run_id
    manifest = _load_manifest(_manifest_path(run_root))
    if manifest.get("output_root") != str(resolved_root):
        raise ValueError("正文迁移清单不属于当前output目录。")
    restored_count = 0
    for target_name in _target_names(manifest):
        backup_path = _original_backup_path(run_root, target_name)
        if not backup_path.is_file():
            continue
        target = resolved_root / target_name / ARCHIVE_DB_FILENAME
        replace_file_atomically(backup_path, target, move_source=False)
        entry = _entry(manifest, target_name)
        entry["status"] = "rolled_back"
        restored_count += 1
    manifest["status"] = "rolled_back"
    manifest["updated_at"] = _now_utc_iso()
    _write_manifest(run_root, manifest)
    return ContentRollbackResult(run_id=run_id, restored_count=restored_count)

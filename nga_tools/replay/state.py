from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from time import perf_counter
from typing import Literal, cast

from nga_tools.backup.archive_store import ARCHIVE_DB_FILENAME
from nga_tools.backup.audio_store import AUDIO_INDEX_FILENAME, AUDIO_UNIQUE_DIRNAME
from nga_tools.backup.image_store import (
    IMAGE_INDEX_FILENAME,
)
from nga_tools.backup.image_validation_store import IMAGE_CACHE_FILENAME
from nga_tools.backup.thread_stores import (
    ARCHIVE_CACHE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
)
from nga_tools.console import report_progress
from nga_tools.core.atomic import (
    replace_temp_file,
    temporary_sibling_path,
    write_json_atomically,
)
from nga_tools.core.sqlite import SQLITE_BUSY_TIMEOUT_SECONDS

InitialState = Literal["empty", "warm", "existing"]
REPLAY_TARGET_MARKER_FILENAME = ".nga-replay-target.json"
_THREAD_SQLITE_BACKUP_FILENAMES = (
    ARCHIVE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
    ARCHIVE_CACHE_DB_FILENAME,
)
_GLOBAL_SQLITE_BACKUP_FILENAMES = (
    IMAGE_INDEX_FILENAME,
    IMAGE_CACHE_FILENAME,
    AUDIO_INDEX_FILENAME,
)
_REPLAY_TARGET_MARKER_KIND = "nga-tools-replay-target"
_REPLAY_TARGET_MARKER_VERSION = 1
_FICLONE = 0x40049409


@dataclass(frozen=True, slots=True)
class PreparationStats:
    initial_state: InitialState
    elapsed_seconds: float
    sqlite_database_count: int = 0
    selection_file_count: int = 0
    image_file_count: int = 0
    image_file_bytes: int = 0
    audio_file_count: int = 0
    audio_file_bytes: int = 0
    reflink_file_count: int = 0
    copied_file_count: int = 0
    validation_cache_path_updates: int = 0
    source_fingerprint_before: str | None = None
    source_fingerprint_after: str | None = None

    def as_dict(self) -> dict[str, object]:
        return cast(dict[str, object], asdict(self))


@dataclass(frozen=True, slots=True)
class _FileState:
    exists: bool
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _DatabaseState:
    database: _FileState
    wal: _FileState


def _is_windows() -> bool:
    return os.name == "nt"


def validate_source_target_paths(source_output: Path, target_output: Path) -> None:
    source = source_output.resolve()
    if not _is_windows() and target_output.is_symlink():
        raise ValueError(f"target-output不能是符号链接：{target_output}")
    target = target_output.resolve()
    if source == target or source in target.parents or target in source.parents:
        raise ValueError("source-output与target-output不能相同或互相嵌套。")
    if not source.is_dir():
        raise ValueError(f"source-output不存在或不是目录：{source}")


def _ensure_empty_target(target_output: Path) -> None:
    if target_output.exists():
        if not target_output.is_dir():
            raise ValueError(f"target-output已存在且不是目录：{target_output}")
        if any(target_output.iterdir()):
            raise ValueError(f"target-output必须不存在或为空：{target_output}")


def _write_replay_target_marker(
    target_output: Path,
    source_output: Path,
    initial_state: Literal["empty", "warm"],
) -> None:
    write_json_atomically(
        target_output / REPLAY_TARGET_MARKER_FILENAME,
        {
            "format_version": _REPLAY_TARGET_MARKER_VERSION,
            "kind": _REPLAY_TARGET_MARKER_KIND,
            "source_output": str(source_output.resolve()),
            "created_initial_state": initial_state,
        },
        indent=2,
        trailing_newline=True,
    )


def _validate_replay_target_marker(
    target_output: Path,
    source_output: Path,
) -> None:
    marker_path = target_output / REPLAY_TARGET_MARKER_FILENAME
    windows = _is_windows()
    try:
        marker_state = marker_path.stat() if windows else marker_path.lstat()
    except FileNotFoundError as error:
        raise ValueError(
            "existing要求target-output带有replay创建的归属标记："
            f"{marker_path}"
        ) from error
    if not windows and stat.S_ISLNK(marker_state.st_mode):
        raise ValueError(f"replay目标归属标记不能是符号链接：{marker_path}")
    if not stat.S_ISREG(marker_state.st_mode):
        raise ValueError(f"replay目标归属标记不是普通文件：{marker_path}")
    try:
        raw_marker: object = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"replay目标归属标记无效：{marker_path}") from error
    if not isinstance(raw_marker, dict):
        raise ValueError(f"replay目标归属标记无效：{marker_path}")
    marker = cast(dict[object, object], raw_marker)
    marker_source = marker.get("source_output")
    if (
        marker.get("format_version") != _REPLAY_TARGET_MARKER_VERSION
        or marker.get("kind") != _REPLAY_TARGET_MARKER_KIND
        or not isinstance(marker_source, str)
        or not marker_source
    ):
        raise ValueError(f"replay目标归属标记无效：{marker_path}")
    if Path(marker_source).resolve() != source_output.resolve():
        raise ValueError(
            "existing目标归属标记对应的source-output不一致："
            f"marker={Path(marker_source).resolve()}，"
            f"runner={source_output.resolve()}"
        )


def _iter_regular_file_identities(
    root: Path,
    *,
    reject_symlinks: bool,
) -> Iterator[tuple[Path, tuple[int, int]]]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = list(iterator)
        except FileNotFoundError as error:
            raise ValueError(f"状态目录扫描期间发生变化：{directory}") from error
        for entry in entries:
            path = Path(entry.path)
            try:
                entry_state = entry.stat(follow_symlinks=False)
            except FileNotFoundError as error:
                raise ValueError(f"状态目录扫描期间发生变化：{path}") from error
            mode = entry_state.st_mode
            if stat.S_ISLNK(mode):
                if reject_symlinks:
                    raise ValueError(f"existing目标不能包含符号链接：{path}")
                continue
            if stat.S_ISDIR(mode):
                pending.append(path)
                continue
            if stat.S_ISREG(mode):
                yield path, (entry_state.st_dev, entry_state.st_ino)
                continue
            if reject_symlinks:
                raise ValueError(f"existing目标包含不支持的文件类型：{path}")


def _validate_existing_target_isolated(
    source_output: Path,
    target_output: Path,
) -> None:
    if _is_windows():
        return
    source_identities = {
        identity
        for _path, identity in _iter_regular_file_identities(
            source_output,
            reject_symlinks=False,
        )
    }
    for target_path, identity in _iter_regular_file_identities(
        target_output,
        reject_symlinks=True,
    ):
        if identity in source_identities:
            raise ValueError(
                "existing目标文件与source-output共享同一inode，"
                f"可能是硬链接：{target_path}"
            )


def _iter_archive_directories(source_output: Path) -> list[Path]:
    return sorted(
        (
            child
            for child in source_output.iterdir()
            if child.is_dir() and (child / ARCHIVE_DB_FILENAME).is_file()
        ),
        key=lambda path: path.name,
    )


def _file_state(path: Path) -> _FileState:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        return _FileState(False, 0, 0)
    return _FileState(True, stat_result.st_size, stat_result.st_mtime_ns)


def _database_state(path: Path) -> _DatabaseState:
    wal_state = _file_state(Path(f"{path}-wal"))
    if wal_state.size == 0:
        wal_state = _FileState(False, 0, 0)
    return _DatabaseState(
        database=_file_state(path),
        wal=wal_state,
    )


def _source_database_states(source_output: Path) -> dict[str, _DatabaseState]:
    database_paths = [
        source_output / filename for filename in _GLOBAL_SQLITE_BACKUP_FILENAMES
    ]
    database_paths.extend(
        archive_dir / filename
        for archive_dir in _iter_archive_directories(source_output)
        for filename in _THREAD_SQLITE_BACKUP_FILENAMES
    )
    return {
        path.relative_to(source_output).as_posix(): _database_state(path)
        for path in database_paths
        if path.is_file()
    }


def _iter_image_files(source_output: Path) -> list[Path]:
    images_root = source_output / "images_unique"
    if not images_root.is_dir():
        return []
    return sorted(
        (path for path in images_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(images_root).as_posix(),
    )


def _iter_audio_files(source_output: Path) -> list[Path]:
    audio_root = source_output / AUDIO_UNIQUE_DIRNAME
    if not audio_root.is_dir():
        return []
    return sorted(
        (path for path in audio_root.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(audio_root).as_posix(),
    )


def _iter_fingerprint_paths(source_output: Path) -> list[Path]:
    paths: list[Path] = []
    for relative_path in sorted(_source_database_states(source_output)):
        database_path = source_output / relative_path
        paths.append(database_path)
        wal_path = Path(f"{database_path}-wal")
        if wal_path.is_file() and wal_path.stat().st_size:
            paths.append(wal_path)
    paths.extend(_iter_image_files(source_output))
    paths.extend(_iter_audio_files(source_output))
    return paths


def source_state_fingerprint(source_output: Path) -> str:
    source = source_output.resolve()
    hasher = hashlib.sha256()
    for path in _iter_fingerprint_paths(source):
        stat_result = path.stat()
        relative = path.relative_to(source).as_posix().encode("utf-8")
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        hasher.update(stat_result.st_size.to_bytes(8, "big", signed=False))
        hasher.update(stat_result.st_mtime_ns.to_bytes(8, "big", signed=False))
    return hasher.hexdigest()


def _sqlite_online_backup(source_path: Path, target_path: Path) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = temporary_sibling_path(target_path)
    state_before = _database_state(source_path)
    if not state_before.database.exists:
        raise ValueError(f"暖状态源数据库不存在：{source_path}")
    if state_before.wal.exists and state_before.wal.size:
        raise ValueError(
            f"暖状态源数据库存在未检查点的WAL：{source_path}。"
            "请停止写入并完成检查点后重试。"
        )
    try:
        source_uri = f"{source_path.resolve().as_uri()}?mode=ro&immutable=1"
        with (
            closing(
                sqlite3.connect(
                    source_uri,
                    timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                    uri=True,
                )
            ) as source_connection,
            closing(
                sqlite3.connect(
                    temp_path,
                    timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
                )
            ) as target_connection,
        ):
            source_connection.execute(
                f"PRAGMA busy_timeout = {int(SQLITE_BUSY_TIMEOUT_SECONDS * 1000)}"
            )
            source_connection.backup(target_connection)
        if _database_state(source_path) != state_before:
            raise RuntimeError(
                f"SQLite Online Backup期间源数据库发生变化：{source_path}"
            )
        replace_temp_file(temp_path, target_path)
    except BaseException:
        try:
            temp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def _try_reflink(source_path: Path, target_path: Path) -> bool:
    try:
        import fcntl
    except ImportError:
        return False

    source_fd = os.open(source_path, os.O_RDONLY)
    try:
        target_fd = os.open(
            target_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o666,
        )
        try:
            fcntl.ioctl(target_fd, _FICLONE, source_fd)
        except OSError:
            os.close(target_fd)
            target_fd = -1
            target_path.unlink(missing_ok=True)
            return False
        finally:
            if target_fd >= 0:
                os.close(target_fd)
    finally:
        os.close(source_fd)
    shutil.copystat(source_path, target_path, follow_symlinks=True)
    return True


def _copy_preserving_file(source_path: Path, target_path: Path) -> bool:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    if _try_reflink(source_path, target_path):
        return True
    shutil.copy2(source_path, target_path)
    return False


def _prepare_warm(source_output: Path, target_output: Path) -> PreparationStats:
    started = perf_counter()
    source = source_output.resolve()
    target = target_output.resolve()
    fingerprint_before = source_state_fingerprint(source)
    database_states_before = _source_database_states(source)
    target.mkdir(parents=True, exist_ok=True)
    _write_replay_target_marker(target, source, "warm")

    database_count = 0
    reflink_count = 0
    copied_count = 0
    archive_dirs = _iter_archive_directories(source)
    for index, archive_dir in enumerate(archive_dirs, start=1):
        target_dir = target / archive_dir.name
        for filename in _THREAD_SQLITE_BACKUP_FILENAMES:
            source_database = archive_dir / filename
            if not source_database.is_file():
                continue
            _sqlite_online_backup(
                source_database,
                target_dir / filename,
            )
            database_count += 1
        if index == len(archive_dirs) or index % 25 == 0:
            report_progress(
                "正在复制暖状态归档",
                completed=index,
                total=len(archive_dirs),
            )

    for filename in _GLOBAL_SQLITE_BACKUP_FILENAMES:
        source_database = source / filename
        if filename == IMAGE_INDEX_FILENAME and not source_database.is_file():
            raise ValueError(f"暖状态缺少图片索引：{source_database}")
        if not source_database.is_file():
            continue
        _sqlite_online_backup(source_database, target / filename)
        database_count += 1

    image_files = _iter_image_files(source)
    image_bytes = 0
    for index, source_image in enumerate(image_files, start=1):
        before = source_image.stat()
        relative = source_image.relative_to(source)
        was_reflink = _copy_preserving_file(source_image, target / relative)
        after = source_image.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError(f"暖状态复制期间源图片发生变化：{source_image}")
        image_bytes += before.st_size
        reflink_count += int(was_reflink)
        copied_count += int(not was_reflink)
        if index == len(image_files) or index % 1000 == 0:
            report_progress(
                "正在复制暖状态图片",
                completed=index,
                total=len(image_files),
            )

    audio_files = _iter_audio_files(source)
    audio_bytes = 0
    for index, source_audio in enumerate(audio_files, start=1):
        before = source_audio.stat()
        relative = source_audio.relative_to(source)
        was_reflink = _copy_preserving_file(source_audio, target / relative)
        after = source_audio.stat()
        if (
            before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
        ):
            raise RuntimeError(f"暖状态复制期间源音频发生变化：{source_audio}")
        audio_bytes += before.st_size
        reflink_count += int(was_reflink)
        copied_count += int(not was_reflink)
        if index == len(audio_files) or index % 250 == 0:
            report_progress(
                "正在复制暖状态音频",
                completed=index,
                total=len(audio_files),
            )

    fingerprint_after = source_state_fingerprint(source)
    if fingerprint_after != fingerprint_before:
        raise RuntimeError("暖状态准备期间source-output发生变化，本次运行作废。")
    if _source_database_states(source) != database_states_before:
        raise RuntimeError("暖状态准备期间源SQLite状态发生变化，本次运行作废。")
    return PreparationStats(
        initial_state="warm",
        elapsed_seconds=perf_counter() - started,
        sqlite_database_count=database_count,
        selection_file_count=0,
        image_file_count=len(image_files),
        image_file_bytes=image_bytes,
        audio_file_count=len(audio_files),
        audio_file_bytes=audio_bytes,
        reflink_file_count=reflink_count,
        copied_file_count=copied_count,
        validation_cache_path_updates=0,
        source_fingerprint_before=fingerprint_before,
        source_fingerprint_after=fingerprint_after,
    )


def prepare_target_state(
    initial_state: InitialState,
    source_output: Path,
    target_output: Path,
) -> PreparationStats:
    validate_source_target_paths(source_output, target_output)
    target = target_output.resolve()
    if initial_state == "existing":
        if not target.is_dir():
            raise ValueError(f"existing要求target-output已存在：{target}")
        started = perf_counter()
        _validate_replay_target_marker(target, source_output)
        _validate_existing_target_isolated(source_output.resolve(), target)
        return PreparationStats(
            initial_state="existing",
            elapsed_seconds=perf_counter() - started,
        )
    _ensure_empty_target(target)
    if initial_state == "empty":
        started = perf_counter()
        target.mkdir(parents=True, exist_ok=True)
        _write_replay_target_marker(target, source_output, "empty")
        return PreparationStats(
            initial_state="empty",
            elapsed_seconds=perf_counter() - started,
        )
    if initial_state == "warm":
        return _prepare_warm(source_output, target)
    raise ValueError(f"未知initial-state：{initial_state}")

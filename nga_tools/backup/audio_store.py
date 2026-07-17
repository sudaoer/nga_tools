from __future__ import annotations

import datetime
import filecmp
import sqlite3
import tempfile
import threading
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, TypedDict

from tinytag import TinyTag, TinyTagException

from nga_tools.core import downloads
from nga_tools.core.download_types import (
    DownloadFileResult,
    DownloadProgressCallback,
    DownloadSummary,
    DownloadTask,
)
from nga_tools.core.hashing import sha256
from nga_tools.config import get_config
from nga_tools.console import WarningCategory, report_warning
from nga_tools.core.atomic import replace_file_atomically
from nga_tools.core.nga_audio import normalize_nga_audio_url
from nga_tools.core.sqlite import (
    SQLITE_BUSY_TIMEOUT_SECONDS,
    configure_connection,
    configure_readonly_connection,
    iter_in_clause_chunks,
)
from nga_tools.storage import ensure_storage_metadata, require_storage_metadata
from nga_tools.storage.schema import (
    require_exact_columns,
    require_index_names,
    require_table_names,
)

AUDIO_INDEX_FILENAME = "audio_index.sqlite3"
AUDIO_UNIQUE_DIRNAME = "audio_unique"


class AudioDownloadTask(TypedDict):
    url: str


@dataclass(frozen=True)
class AudioMapping:
    url: str
    unique_rel_path: str
    content_sha256: str
    content_bytes: int
    duration_seconds: float

    def path(self, output_root: Path) -> Path:
        return output_root / self.unique_rel_path


@dataclass(frozen=True)
class StoredAudio:
    mapping: AudioMapping
    reused: bool
    collision: bool


@dataclass(slots=True)
class _AudioURLClaim:
    completed: threading.Event
    result: DownloadFileResult | None = None
    error: BaseException | None = None


_SCHEMA_LOCK = threading.RLock()
_AUDIO_HASH_LOCKS = tuple(threading.Lock() for _index in range(256))
_AUDIO_CLAIMS_LOCK = threading.RLock()
_AUDIO_CLAIMS: dict[tuple[str, str], _AudioURLClaim] = {}
_INITIALIZED_AUDIO_INDEX_PATHS: set[Path] = set()
_AUDIO_VALIDATION_LOCK = threading.RLock()
_AUDIO_VALIDATION_CACHE: dict[Path, tuple[int, int, int, str]] = {}
_AUDIO_VALIDATION_CACHE_MAX_ENTRIES = 8192
_AUDIO_MAPPING_COLUMNS = (
    ("url", "TEXT"), ("unique_rel_path", "TEXT"),
    ("content_sha256", "TEXT"), ("content_bytes", "INTEGER"),
    ("duration_seconds", "REAL"), ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
)


def require_current_audio_index(
    connection: sqlite3.Connection,
    db_path: Path,
) -> None:
    source = f"audio_index {db_path}"
    require_storage_metadata(connection, role="audio_index")
    require_table_names(
        connection,
        expected={"storage_metadata", "audio_mappings"},
        source=source,
    )
    require_exact_columns(
        connection,
        "audio_mappings",
        _AUDIO_MAPPING_COLUMNS,
        source=source,
    )
    require_index_names(
        connection,
        required={"idx_audio_mappings_unique_rel_path"},
        forbidden=set(),
        source=source,
    )


def configured_output_root() -> Path:
    return Path(get_config().output_dir)


def audio_index_path(output_root: Path | None = None) -> Path:
    root = configured_output_root() if output_root is None else output_root
    return root / AUDIO_INDEX_FILENAME


def audio_unique_dir(output_root: Path | None = None) -> Path:
    root = configured_output_root() if output_root is None else output_root
    return root / AUDIO_UNIQUE_DIRNAME


def _now_utc_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _initialize_audio_index(output_root: Path) -> Path:
    db_path = audio_index_path(output_root).resolve()
    with _SCHEMA_LOCK:
        if db_path in _INITIALIZED_AUDIO_INDEX_PATHS and db_path.is_file():
            return db_path
        new_database = not db_path.is_file()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with closing(
            sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
        ) as connection:
            configure_connection(connection)
            with connection:
                if new_database:
                    ensure_storage_metadata(connection, role="audio_index")
                    connection.execute(
                        """
                        CREATE TABLE audio_mappings (
                            url TEXT PRIMARY KEY,
                            unique_rel_path TEXT NOT NULL,
                            content_sha256 TEXT NOT NULL,
                            content_bytes INTEGER NOT NULL CHECK(content_bytes > 0),
                            duration_seconds REAL NOT NULL CHECK(duration_seconds > 0),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        CREATE INDEX idx_audio_mappings_unique_rel_path
                        ON audio_mappings(unique_rel_path)
                        """
                    )
                else:
                    require_current_audio_index(connection, db_path)
        _INITIALIZED_AUDIO_INDEX_PATHS.add(db_path)
    return db_path


def ensure_audio_index(output_root: Path | None = None) -> Path:
    root = configured_output_root() if output_root is None else output_root
    return _initialize_audio_index(root)


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
        uri=True,
    )
    configure_readonly_connection(connection)
    try:
        require_current_audio_index(connection, db_path)
    except BaseException:
        connection.close()
        raise
    return connection


def _row_to_mapping(row: tuple[object, ...]) -> AudioMapping | None:
    if len(row) != 5:
        return None
    url, relative_path, content_hash, content_bytes, duration_seconds = row
    if (
        not isinstance(url, str)
        or not isinstance(relative_path, str)
        or not isinstance(content_hash, str)
        or type(content_bytes) is not int
        or content_bytes <= 0
        or not isinstance(duration_seconds, (int, float))
        or isinstance(duration_seconds, bool)
        or duration_seconds <= 0
    ):
        return None
    return AudioMapping(
        url=url,
        unique_rel_path=relative_path,
        content_sha256=content_hash,
        content_bytes=content_bytes,
        duration_seconds=float(duration_seconds),
    )


def _mapping_path_is_valid(output_root: Path, mapping: AudioMapping) -> bool:
    relative_path = Path(mapping.unique_rel_path)
    if (
        relative_path.is_absolute()
        or not relative_path.parts
        or relative_path.parts[0] != AUDIO_UNIQUE_DIRNAME
    ):
        return False
    audio_root = audio_unique_dir(output_root).resolve()
    path = (output_root.resolve() / relative_path).resolve()
    if not path.is_relative_to(audio_root):
        return False
    try:
        before = path.stat()
    except OSError:
        return False
    if not path.is_file() or before.st_size != mapping.content_bytes:
        return False

    fingerprint = (before.st_size, before.st_mtime_ns, before.st_ctime_ns)
    with _AUDIO_VALIDATION_LOCK:
        cached = _AUDIO_VALIDATION_CACHE.get(path)
        if cached is not None and cached[:3] == fingerprint:
            return cached[3] == mapping.content_sha256

    try:
        content_hash = sha256(str(path))
        after = path.stat()
    except OSError:
        return False
    if (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ) != fingerprint:
        return False

    with _AUDIO_VALIDATION_LOCK:
        if len(_AUDIO_VALIDATION_CACHE) >= _AUDIO_VALIDATION_CACHE_MAX_ENTRIES:
            _AUDIO_VALIDATION_CACHE.clear()
        _AUDIO_VALIDATION_CACHE[path] = (*fingerprint, content_hash)
    return content_hash == mapping.content_sha256


def audio_mappings_for_urls(
    output_root: Path,
    urls: Iterable[str],
    *,
    require_existing_file: bool = True,
) -> dict[str, AudioMapping]:
    normalized_urls = sorted(
        {
            normalized
            for url in urls
            if (normalized := normalize_nga_audio_url(url)) is not None
        }
    )
    if not normalized_urls:
        return {}
    db_path = audio_index_path(output_root)
    if not db_path.is_file():
        return {}

    mappings: dict[str, AudioMapping] = {}
    try:
        with closing(_connect_readonly(db_path)) as connection:
            for chunk in iter_in_clause_chunks(normalized_urls):
                placeholders = ",".join("?" for _item in chunk)
                rows = connection.execute(
                    f"""
                    SELECT
                        url,
                        unique_rel_path,
                        content_sha256,
                        content_bytes,
                        duration_seconds
                    FROM audio_mappings
                    WHERE url IN ({placeholders})
                    """,
                    chunk,
                ).fetchall()
                for raw_row in rows:
                    mapping = _row_to_mapping(tuple(raw_row))
                    if mapping is None:
                        continue
                    if require_existing_file and not _mapping_path_is_valid(
                        output_root,
                        mapping,
                    ):
                        continue
                    mappings[mapping.url] = mapping
    except sqlite3.Error:
        return {}
    return mappings


def _upsert_audio_mappings(
    output_root: Path,
    mappings: list[AudioMapping],
) -> None:
    if not mappings:
        return
    db_path = _initialize_audio_index(output_root)
    now = _now_utc_iso()
    with closing(
        sqlite3.connect(db_path, timeout=SQLITE_BUSY_TIMEOUT_SECONDS)
    ) as connection:
        configure_connection(connection)
        with connection:
            connection.executemany(
                """
                INSERT INTO audio_mappings (
                    url,
                    unique_rel_path,
                    content_sha256,
                    content_bytes,
                    duration_seconds,
                    created_at,
                    updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    unique_rel_path = excluded.unique_rel_path,
                    content_sha256 = excluded.content_sha256,
                    content_bytes = excluded.content_bytes,
                    duration_seconds = excluded.duration_seconds,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        mapping.url,
                        mapping.unique_rel_path,
                        mapping.content_sha256,
                        mapping.content_bytes,
                        mapping.duration_seconds,
                        now,
                        now,
                    )
                    for mapping in mappings
                ],
            )


def _audio_duration(path: Path) -> float:
    try:
        tag = TinyTag.get(path, tags=False, duration=True, image=False)
    except (OSError, TinyTagException) as error:
        raise ValueError(f"音频文件无法解析：{path}: {error}") from error
    duration = tag.duration
    if (
        not isinstance(duration, (int, float))
        or isinstance(duration, bool)
        or duration <= 0
    ):
        raise ValueError(f"音频文件缺少有效时长：{path}")
    return float(duration)


def _download_identity(
    source_path: Path,
    download_result: DownloadFileResult,
) -> tuple[str, int]:
    content_hash = download_result.get("content_sha256")
    content_bytes = download_result.get("content_bytes")
    if (
        isinstance(content_hash, str)
        and len(content_hash) == 64
        and isinstance(content_bytes, int)
        and not isinstance(content_bytes, bool)
        and content_bytes > 0
        and source_path.stat().st_size == content_bytes
    ):
        try:
            int(content_hash, 16)
        except ValueError:
            pass
        else:
            return content_hash.lower(), content_bytes
    return sha256(str(source_path)), source_path.stat().st_size


def _hash_lock(content_hash: str) -> threading.Lock:
    try:
        index = int(content_hash[:2], 16)
    except ValueError:
        index = hash(content_hash) % len(_AUDIO_HASH_LOCKS)
    return _AUDIO_HASH_LOCKS[index]


def _same_content(first: Path, second: Path) -> bool:
    return first.is_file() and second.is_file() and filecmp.cmp(
        first,
        second,
        shallow=False,
    )


def _select_target_path(
    source_path: Path,
    output_root: Path,
    content_hash: str,
) -> tuple[Path, bool, bool]:
    unique_dir = audio_unique_dir(output_root)
    unique_dir.mkdir(parents=True, exist_ok=True)
    target_path = unique_dir / f"{content_hash}.mp3"
    if not target_path.exists():
        return target_path, False, False
    if _same_content(source_path, target_path):
        return target_path, True, False

    collision_index = 1
    while True:
        collision_path = unique_dir / (
            f"{content_hash}-collision-{collision_index}.mp3"
        )
        if not collision_path.exists():
            report_warning(
                WarningCategory.AUDIO_PROCESSING,
                f"音频SHA-256 hash碰撞，保存为：{collision_path}",
            )
            return collision_path, False, True
        if _same_content(source_path, collision_path):
            return collision_path, True, True
        collision_index += 1


def store_downloaded_audio(
    source_path: Path,
    url: str,
    download_result: DownloadFileResult,
    *,
    output_root: Path | None = None,
) -> StoredAudio:
    root = configured_output_root() if output_root is None else output_root
    normalized_url = normalize_nga_audio_url(url)
    if normalized_url is None:
        raise ValueError(f"NGA音频链接无效：{url}")
    duration_seconds = _audio_duration(source_path)
    content_hash, content_bytes = _download_identity(source_path, download_result)

    with _hash_lock(content_hash):
        target_path, reused, collision = _select_target_path(
            source_path,
            root,
            content_hash,
        )
        if reused:
            source_path.unlink(missing_ok=True)
        else:
            replace_file_atomically(source_path, target_path, move_source=True)
            _audio_duration(target_path)

    relative_path = target_path.resolve().relative_to(root.resolve()).as_posix()
    mapping = AudioMapping(
        url=normalized_url,
        unique_rel_path=relative_path,
        content_sha256=content_hash,
        content_bytes=content_bytes,
        duration_seconds=duration_seconds,
    )
    return StoredAudio(mapping=mapping, reused=reused, collision=collision)


def _claim_audio_url(
    output_root: Path,
    url: str,
) -> tuple[tuple[str, str], _AudioURLClaim, bool]:
    key = (str(output_root.resolve()), url)
    with _AUDIO_CLAIMS_LOCK:
        existing = _AUDIO_CLAIMS.get(key)
        if existing is not None:
            return key, existing, False
        claim = _AudioURLClaim(threading.Event())
        _AUDIO_CLAIMS[key] = claim
        return key, claim, True


def _release_audio_claim(
    key: tuple[str, str],
    claim: _AudioURLClaim,
    *,
    result: DownloadFileResult | None = None,
    error: BaseException | None = None,
) -> None:
    with _AUDIO_CLAIMS_LOCK:
        claim.result = result
        claim.error = error
        claim.completed.set()
        if _AUDIO_CLAIMS.get(key) is claim:
            del _AUDIO_CLAIMS[key]


def _failure_from_download(
    result: DownloadFileResult,
    output_root: Path,
) -> DownloadFileResult:
    failure: DownloadFileResult = {
        "url": result["url"],
        "save_path": str(audio_unique_dir(output_root)),
        "success": False,
        "error": result.get("error", "unknown"),
        "failure_kind": result.get("failure_kind", "unexpected_download"),
    }
    if "http_status" in result:
        failure["http_status"] = result["http_status"]
    return failure


def download_audio_tasks(
    audio_tasks: list[AudioDownloadTask],
    *,
    output_root: Path | None = None,
    on_progress: DownloadProgressCallback | None = None,
    on_download_progress: DownloadProgressCallback | None = None,
) -> DownloadSummary:
    root = configured_output_root() if output_root is None else output_root
    normalized_tasks: list[AudioDownloadTask] = []
    invalid_results: list[DownloadFileResult] = []
    for task in audio_tasks:
        normalized_url = normalize_nga_audio_url(task["url"])
        if normalized_url is None:
            invalid_results.append(
                {
                    "url": task["url"],
                    "save_path": str(audio_unique_dir(root)),
                    "success": False,
                    "error": "NGA音频链接无效",
                    "failure_kind": "audio_validation",
                }
            )
        else:
            normalized_tasks.append({"url": normalized_url})

    existing = audio_mappings_for_urls(
        root,
        (task["url"] for task in normalized_tasks),
    )
    already_complete = [
        task for task in normalized_tasks if task["url"] in existing
    ]
    pending = [
        task for task in normalized_tasks if task["url"] not in existing
    ]

    owner_by_url: dict[
        str,
        tuple[tuple[str, str], _AudioURLClaim],
    ] = {}
    waiters: list[tuple[AudioDownloadTask, _AudioURLClaim]] = []
    for task in pending:
        key, claim, is_owner = _claim_audio_url(root, task["url"])
        if is_owner:
            owner_by_url[task["url"]] = (key, claim)
        else:
            waiters.append((task, claim))

    result_by_url: dict[str, DownloadFileResult] = {}
    for task in already_complete:
        mapping = existing[task["url"]]
        result_by_url[task["url"]] = {
            "url": task["url"],
            "save_path": str(mapping.path(root)),
            "success": True,
        }

    try:
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix=".nga_audio_download_",
            dir=root,
        ) as temp_dir:
            temp_root = Path(temp_dir)
            download_tasks: list[DownloadTask] = []
            temp_by_url: dict[str, Path] = {}
            for index, url in enumerate(owner_by_url):
                temp_path = temp_root / f"audio_{index}.mp3"
                temp_by_url[url] = temp_path
                download_tasks.append(
                    {
                        "url": url,
                        "save_path": str(temp_path),
                    }
                )

            if on_download_progress is None:
                downloaded = downloads.download_files(
                    download_tasks,
                    resource_kind="audio",
                )
            else:
                downloaded = downloads.download_files(
                    download_tasks,
                    resource_kind="audio",
                    on_progress=on_download_progress,
                )
            mappings_to_write: list[AudioMapping] = []
            stored_by_url: dict[str, StoredAudio] = {}
            for download_result in downloaded["succeeded"]:
                url = download_result["url"]
                try:
                    stored = store_downloaded_audio(
                        temp_by_url[url],
                        url,
                        download_result,
                        output_root=root,
                    )
                except ValueError as error:
                    result_by_url[url] = {
                        "url": url,
                        "save_path": str(audio_unique_dir(root)),
                        "success": False,
                        "error": str(error),
                        "failure_kind": "audio_validation",
                    }
                except Exception as error:
                    result_by_url[url] = {
                        "url": url,
                        "save_path": str(audio_unique_dir(root)),
                        "success": False,
                        "error": str(error),
                        "failure_kind": "audio_store",
                    }
                else:
                    stored_by_url[url] = stored
                    mappings_to_write.append(stored.mapping)
            for download_result in downloaded["failed"]:
                result_by_url[download_result["url"]] = _failure_from_download(
                    download_result,
                    root,
                )

            try:
                _upsert_audio_mappings(root, mappings_to_write)
            except Exception as error:
                for url in stored_by_url:
                    result_by_url[url] = {
                        "url": url,
                        "save_path": str(audio_unique_dir(root)),
                        "success": False,
                        "error": str(error),
                        "failure_kind": "audio_store",
                    }
            else:
                for url, stored in stored_by_url.items():
                    result_by_url[url] = {
                        "url": url,
                        "save_path": str(stored.mapping.path(root)),
                        "success": True,
                    }

        for url, (key, claim) in owner_by_url.items():
            result = result_by_url[url]
            _release_audio_claim(key, claim, result=result)
    except BaseException as error:
        for key, claim in owner_by_url.values():
            if not claim.completed.is_set():
                _release_audio_claim(key, claim, error=error)
        raise

    waiter_results: list[DownloadFileResult] = []
    for task, claim in waiters:
        claim.completed.wait()
        if claim.error is not None:
            raise RuntimeError(f"共享音频下载失败：{task['url']}") from claim.error
        if claim.result is None:
            raise RuntimeError(f"共享音频下载没有结果：{task['url']}")
        copied = claim.result.copy()
        copied["url"] = task["url"]
        waiter_results.append(copied)

    ordered_results: list[DownloadFileResult] = []
    waiter_iter = iter(waiter_results)
    owner_emitted: set[str] = set()
    for task in normalized_tasks:
        url = task["url"]
        if url in existing or (url in owner_by_url and url not in owner_emitted):
            result = result_by_url[url]
            owner_emitted.add(url)
        elif url in owner_by_url:
            result = result_by_url[url].copy()
        else:
            result = next(waiter_iter)
        ordered_results.append(result)
    ordered_results.extend(invalid_results)

    if on_progress is not None:
        for completed, result in enumerate(ordered_results, start=1):
            on_progress(completed, len(ordered_results), result)
    return {
        "succeeded": [result for result in ordered_results if result["success"]],
        "failed": [result for result in ordered_results if not result["success"]],
    }

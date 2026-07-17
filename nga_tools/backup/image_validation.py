from __future__ import annotations

import os
import threading
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from nga_tools.backup import image_validation_store
from nga_tools.backup.image_validation_store import PersistentValidationEntry
from nga_tools.core.image_formats import image_file_is_valid


ImageValidationSource = Literal["memory", "persistent", "deep", "missing"]


@dataclass(frozen=True)
class ImageValidationOutcome:
    valid: bool
    source: ImageValidationSource


@dataclass(frozen=True)
class _ImageFileFingerprint:
    canonical_path: str
    size: int
    mtime_ns: int


def canonical_image_path(path: Path) -> Path:
    try:
        resolved_path = path.resolve(strict=False)
    except (OSError, RuntimeError):
        resolved_path = Path(os.path.abspath(path))
    return Path(os.path.normcase(str(resolved_path)))


def canonical_image_path_key(path: Path) -> str:
    return str(canonical_image_path(path))


def _image_file_fingerprint(path: Path) -> _ImageFileFingerprint | None:
    try:
        file_stat = path.stat()
    except OSError:
        return None
    return _ImageFileFingerprint(
        canonical_path=str(path),
        size=file_stat.st_size,
        mtime_ns=file_stat.st_mtime_ns,
    )


class ImageValidationCache:
    """Command-scoped, thread-safe cache for expensive image validation.

    Backed by an optional persistent SQLite store that survives across
    process invocations. Runtime memory uses canonical paths; persistence
    converts them to output-relative paths plus ``(size, mtime_ns)``.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._results: dict[_ImageFileFingerprint, bool] = {}
        self._result_keys_by_path: dict[str, set[_ImageFileFingerprint]] = {}
        self._in_flight: dict[
            _ImageFileFingerprint,
            Future[bool | None],
        ] = {}
        self._path_generations: dict[str, int] = {}
        self._persistent: dict[str, tuple[int, int, bool]] = {}
        self._new_entries: list[PersistentValidationEntry] = []
        self._preloaded_paths: set[str] = set()
        self._preload_in_flight: dict[str, Future[None]] = {}

    def preload(self, paths: set[Path]) -> int:
        if not paths:
            return 0
        path_keys: set[str] = set()
        for raw_path in paths:
            canonical = canonical_image_path(raw_path)
            key = str(canonical)
            path_keys.add(key)

        owned_futures: dict[str, Future[None]] = {}
        owned_generations: dict[str, int] = {}
        futures_to_wait: set[Future[None]] = set()
        with self._lock:
            for key in path_keys:
                if key in self._preloaded_paths:
                    continue
                existing_future = self._preload_in_flight.get(key)
                if existing_future is not None:
                    futures_to_wait.add(existing_future)
                    continue
                future = Future[None]()
                self._preload_in_flight[key] = future
                owned_futures[key] = future
                owned_generations[key] = self._path_generations.get(key, 0)

        owned_paths = set(owned_futures)
        if owned_paths:
            try:
                loaded = image_validation_store.load_persistent_validation_cache(
                    owned_paths
                )
            except BaseException as error:
                with self._lock:
                    for key, future in owned_futures.items():
                        self._preload_in_flight.pop(key, None)
                        future.set_exception(error)
                raise

            with self._lock:
                for key, future in owned_futures.items():
                    if self._path_generations.get(key, 0) == owned_generations[key]:
                        entry = loaded.get(key)
                        if entry is not None:
                            self._persistent[key] = entry
                        self._preloaded_paths.add(key)
                    self._preload_in_flight.pop(key, None)
                    future.set_result(None)

        for future in futures_to_wait:
            future.result()
        return len(owned_paths)

    def flush_new_entries(self) -> None:
        with self._lock:
            entries = list(self._new_entries)
            self._new_entries.clear()
        if entries:
            image_validation_store.save_persistent_validation_entries(entries)

    def validate(self, path: Path) -> ImageValidationOutcome:
        canonical_path = canonical_image_path(path)
        path_key = str(canonical_path)

        while True:
            fingerprint = _image_file_fingerprint(canonical_path)
            if fingerprint is None:
                return ImageValidationOutcome(
                    valid=False,
                    source="missing",
                )

            is_leader = False
            validation_generation = 0
            preload_future: Future[None] | None = None
            validation_future: Future[bool | None] | None = None
            with self._lock:
                if fingerprint in self._results:
                    cached_result = self._results[fingerprint]
                    return ImageValidationOutcome(
                        valid=cached_result,
                        source="memory",
                    )

                preload_future = self._preload_in_flight.get(path_key)
                if preload_future is None:
                    persistent = self._persistent.get(path_key)
                    if (
                        persistent is not None
                        and persistent[0] == fingerprint.size
                        and persistent[1] == fingerprint.mtime_ns
                        and path_key in self._preloaded_paths
                    ):
                        self._results[fingerprint] = persistent[2]
                        self._result_keys_by_path.setdefault(path_key, set()).add(
                            fingerprint
                        )
                        return ImageValidationOutcome(
                            valid=persistent[2],
                            source="persistent",
                        )

                    validation_future = self._in_flight.get(fingerprint)
                    if validation_future is None:
                        validation_future = Future[bool | None]()
                        self._in_flight[fingerprint] = validation_future
                        validation_generation = self._path_generations.get(
                            path_key,
                            0,
                        )
                        is_leader = True

            if preload_future is not None:
                preload_future.result()
                continue

            if validation_future is None:
                raise RuntimeError("图片校验任务状态无效。")
            if not is_leader:
                shared_result = validation_future.result()
                if shared_result is None:
                    continue
                return ImageValidationOutcome(
                    valid=shared_result,
                    source="memory",
                )

            try:
                validation_result = image_file_is_valid(canonical_path)
            except BaseException as error:
                with self._lock:
                    self._in_flight.pop(fingerprint, None)
                    validation_future.set_exception(error)
                raise

            after_fingerprint = _image_file_fingerprint(canonical_path)
            with self._lock:
                unchanged = (
                    after_fingerprint == fingerprint
                    and self._path_generations.get(path_key, 0)
                    == validation_generation
                )
                self._in_flight.pop(fingerprint, None)
                if unchanged:
                    self._results[fingerprint] = validation_result
                    self._result_keys_by_path.setdefault(path_key, set()).add(
                        fingerprint
                    )
                    self._new_entries.append(
                        PersistentValidationEntry(
                            canonical_path=path_key,
                            size=fingerprint.size,
                            mtime_ns=fingerprint.mtime_ns,
                            valid=validation_result,
                        )
                    )
                    validation_future.set_result(validation_result)
                else:
                    validation_future.set_result(None)

            if unchanged:
                return ImageValidationOutcome(
                    valid=validation_result,
                    source="deep",
                )

    def invalidate(self, path: Path) -> None:
        path_key = canonical_image_path_key(path)
        with self._lock:
            self._path_generations[path_key] = (
                self._path_generations.get(path_key, 0) + 1
            )
            for fingerprint in self._result_keys_by_path.pop(path_key, set()):
                self._results.pop(fingerprint, None)
            self._persistent.pop(path_key, None)
            self._preloaded_paths.discard(path_key)
        image_validation_store.delete_persistent_validation_entry(path_key)


_CURRENT_IMAGE_VALIDATION_CACHE: ContextVar[ImageValidationCache | None] = (
    ContextVar("nga_tools_image_validation_cache", default=None)
)


def current_image_validation_cache() -> ImageValidationCache | None:
    return _CURRENT_IMAGE_VALIDATION_CACHE.get()


@contextmanager
def use_image_validation_cache(
    cache: ImageValidationCache | None = None,
) -> Generator[ImageValidationCache]:
    effective_cache = cache if cache is not None else ImageValidationCache()
    token = _CURRENT_IMAGE_VALIDATION_CACHE.set(effective_cache)
    try:
        yield effective_cache
    finally:
        _CURRENT_IMAGE_VALIDATION_CACHE.reset(token)


def invalidate_current_image_validation(path: Path) -> None:
    cache = current_image_validation_cache()
    if cache is not None:
        cache.invalidate(path)

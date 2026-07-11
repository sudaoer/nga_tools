from __future__ import annotations

import os
import threading
from collections.abc import Generator
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from nga_tools.core.image_formats import image_file_is_valid


@dataclass(frozen=True)
class ImageValidationOutcome:
    valid: bool
    cache_hit: bool
    deep_validated: bool


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
    """Command-scoped, thread-safe cache for expensive image validation."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._results: dict[_ImageFileFingerprint, bool] = {}
        self._result_keys_by_path: dict[str, set[_ImageFileFingerprint]] = {}
        self._in_flight: dict[
            _ImageFileFingerprint,
            Future[bool | None],
        ] = {}
        self._path_generations: dict[str, int] = {}

    def validate(self, path: Path) -> ImageValidationOutcome:
        canonical_path = canonical_image_path(path)
        path_key = str(canonical_path)

        while True:
            fingerprint = _image_file_fingerprint(canonical_path)
            if fingerprint is None:
                return ImageValidationOutcome(
                    valid=False,
                    cache_hit=False,
                    deep_validated=False,
                )

            is_leader = False
            validation_generation = 0
            with self._lock:
                if fingerprint in self._results:
                    cached_result = self._results[fingerprint]
                    return ImageValidationOutcome(
                        valid=cached_result,
                        cache_hit=True,
                        deep_validated=False,
                    )

                future = self._in_flight.get(fingerprint)
                if future is None:
                    future = Future[bool | None]()
                    self._in_flight[fingerprint] = future
                    validation_generation = self._path_generations.get(path_key, 0)
                    is_leader = True

            if not is_leader:
                shared_result = future.result()
                if shared_result is None:
                    continue
                return ImageValidationOutcome(
                    valid=shared_result,
                    cache_hit=True,
                    deep_validated=False,
                )

            try:
                validation_result = image_file_is_valid(canonical_path)
            except BaseException as error:
                with self._lock:
                    self._in_flight.pop(fingerprint, None)
                    future.set_exception(error)
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
                    future.set_result(validation_result)
                else:
                    future.set_result(None)

            if unchanged:
                return ImageValidationOutcome(
                    valid=validation_result,
                    cache_hit=False,
                    deep_validated=True,
                )

    def invalidate(self, path: Path) -> None:
        path_key = canonical_image_path_key(path)
        with self._lock:
            self._path_generations[path_key] = (
                self._path_generations.get(path_key, 0) + 1
            )
            for fingerprint in self._result_keys_by_path.pop(path_key, set()):
                self._results.pop(fingerprint, None)


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

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Literal


type ImageStorePhase = Literal[
    "source_validation",
    "fallback_hash",
    "format_detection",
    "target_selection",
    "content_compare",
    "file_placement",
    "final_validation",
    "mapping_submit",
    "mapping_wait",
]


@dataclass(frozen=True)
class ImageStoreMetrics:
    store_attempts: int
    stores_completed: int
    stores_failed: int
    reused_files: int
    collision_files: int
    precomputed_hash_hits: int
    precomputed_hash_rejections: int
    fallback_hashes: int
    mapping_submissions: int
    mapping_rows: int
    mapping_failures: int
    single_flight_waits: int
    source_validation_seconds: float
    fallback_hash_seconds: float
    format_detection_seconds: float
    target_selection_seconds: float
    content_compare_seconds: float
    file_placement_seconds: float
    final_validation_seconds: float
    mapping_submit_seconds: float
    mapping_wait_seconds: float
    single_flight_wait_seconds: float

    def as_dict(self) -> dict[str, int | float]:
        return {
            "store_attempts": self.store_attempts,
            "stores_completed": self.stores_completed,
            "stores_failed": self.stores_failed,
            "reused_files": self.reused_files,
            "collision_files": self.collision_files,
            "precomputed_hash_hits": self.precomputed_hash_hits,
            "precomputed_hash_rejections": self.precomputed_hash_rejections,
            "fallback_hashes": self.fallback_hashes,
            "mapping_submissions": self.mapping_submissions,
            "mapping_rows": self.mapping_rows,
            "mapping_failures": self.mapping_failures,
            "single_flight_waits": self.single_flight_waits,
            "source_validation_seconds": self.source_validation_seconds,
            "fallback_hash_seconds": self.fallback_hash_seconds,
            "format_detection_seconds": self.format_detection_seconds,
            "target_selection_seconds": self.target_selection_seconds,
            "content_compare_seconds": self.content_compare_seconds,
            "file_placement_seconds": self.file_placement_seconds,
            "final_validation_seconds": self.final_validation_seconds,
            "mapping_submit_seconds": self.mapping_submit_seconds,
            "mapping_wait_seconds": self.mapping_wait_seconds,
            "single_flight_wait_seconds": self.single_flight_wait_seconds,
        }


class ImageStoreMetricsCollector:
    """Thread-safe metrics shared by every worker in one command."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._store_attempts = 0
        self._stores_completed = 0
        self._stores_failed = 0
        self._reused_files = 0
        self._collision_files = 0
        self._precomputed_hash_hits = 0
        self._precomputed_hash_rejections = 0
        self._fallback_hashes = 0
        self._mapping_submissions = 0
        self._mapping_rows = 0
        self._mapping_failures = 0
        self._single_flight_waits = 0
        self._phase_seconds: dict[ImageStorePhase, float] = {
            "source_validation": 0.0,
            "fallback_hash": 0.0,
            "format_detection": 0.0,
            "target_selection": 0.0,
            "content_compare": 0.0,
            "file_placement": 0.0,
            "final_validation": 0.0,
            "mapping_submit": 0.0,
            "mapping_wait": 0.0,
        }
        self._single_flight_wait_seconds = 0.0

    def add_phase_seconds(
        self,
        phase: ImageStorePhase,
        seconds: float,
    ) -> None:
        with self._lock:
            self._phase_seconds[phase] += max(0.0, seconds)

    def record_store_attempt(self) -> None:
        with self._lock:
            self._store_attempts += 1

    def record_store_completed(self, *, reused: bool, collision: bool) -> None:
        with self._lock:
            self._stores_completed += 1
            if reused:
                self._reused_files += 1
            if collision:
                self._collision_files += 1

    def record_store_failed(self) -> None:
        with self._lock:
            self._stores_failed += 1

    def record_hash_source(
        self,
        *,
        precomputed: bool,
        rejected: bool,
    ) -> None:
        with self._lock:
            if precomputed:
                self._precomputed_hash_hits += 1
            else:
                self._fallback_hashes += 1
            if rejected:
                self._precomputed_hash_rejections += 1

    def record_mapping_submission(self, row_count: int) -> None:
        with self._lock:
            self._mapping_submissions += 1
            self._mapping_rows += row_count

    def record_mapping_failure(self) -> None:
        with self._lock:
            self._mapping_failures += 1

    def record_single_flight_wait(self, seconds: float) -> None:
        with self._lock:
            self._single_flight_waits += 1
            self._single_flight_wait_seconds += max(0.0, seconds)

    def snapshot(self) -> ImageStoreMetrics:
        with self._lock:
            return ImageStoreMetrics(
                store_attempts=self._store_attempts,
                stores_completed=self._stores_completed,
                stores_failed=self._stores_failed,
                reused_files=self._reused_files,
                collision_files=self._collision_files,
                precomputed_hash_hits=self._precomputed_hash_hits,
                precomputed_hash_rejections=self._precomputed_hash_rejections,
                fallback_hashes=self._fallback_hashes,
                mapping_submissions=self._mapping_submissions,
                mapping_rows=self._mapping_rows,
                mapping_failures=self._mapping_failures,
                single_flight_waits=self._single_flight_waits,
                source_validation_seconds=self._phase_seconds[
                    "source_validation"
                ],
                fallback_hash_seconds=self._phase_seconds["fallback_hash"],
                format_detection_seconds=self._phase_seconds[
                    "format_detection"
                ],
                target_selection_seconds=self._phase_seconds[
                    "target_selection"
                ],
                content_compare_seconds=self._phase_seconds["content_compare"],
                file_placement_seconds=self._phase_seconds["file_placement"],
                final_validation_seconds=self._phase_seconds[
                    "final_validation"
                ],
                mapping_submit_seconds=self._phase_seconds["mapping_submit"],
                mapping_wait_seconds=self._phase_seconds["mapping_wait"],
                single_flight_wait_seconds=self._single_flight_wait_seconds,
            )


_scope_lock = threading.RLock()
_active_collector: ImageStoreMetricsCollector | None = None
_scope_depth = 0
_last_metrics: ImageStoreMetrics | None = None


def _current_collector() -> ImageStoreMetricsCollector | None:
    with _scope_lock:
        return _active_collector


@contextmanager
def time_image_store_phase(phase: ImageStorePhase) -> Generator[None]:
    started_at = perf_counter()
    try:
        yield
    finally:
        collector = _current_collector()
        if collector is not None:
            collector.add_phase_seconds(phase, perf_counter() - started_at)


def record_image_store_attempt() -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_store_attempt()


def record_image_store_completed(*, reused: bool, collision: bool) -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_store_completed(reused=reused, collision=collision)


def record_image_store_failed() -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_store_failed()


def record_image_hash_source(*, precomputed: bool, rejected: bool = False) -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_hash_source(
            precomputed=precomputed,
            rejected=rejected,
        )


def record_image_mapping_submission(row_count: int) -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_mapping_submission(row_count)


def record_image_mapping_failure() -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_mapping_failure()


def record_image_single_flight_wait(seconds: float) -> None:
    collector = _current_collector()
    if collector is not None:
        collector.record_single_flight_wait(seconds)


def image_store_metrics() -> ImageStoreMetrics | None:
    with _scope_lock:
        collector = _active_collector
        if collector is not None:
            return collector.snapshot()
        return _last_metrics


@contextmanager
def use_image_store_metrics() -> Generator[None]:
    global _active_collector, _scope_depth, _last_metrics
    with _scope_lock:
        if _active_collector is None:
            _active_collector = ImageStoreMetricsCollector()
        _scope_depth += 1
    try:
        yield
    finally:
        completed_collector: ImageStoreMetricsCollector | None = None
        with _scope_lock:
            _scope_depth -= 1
            if _scope_depth == 0:
                completed_collector = _active_collector
                _active_collector = None
        if completed_collector is not None:
            with _scope_lock:
                _last_metrics = completed_collector.snapshot()

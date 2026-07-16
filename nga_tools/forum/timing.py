from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from time import perf_counter
from typing import Literal, TypeAlias


ForumSyncTimingPhase = Literal[
    "setup",
    "fetch",
    "forum_page_request",
    "rate_limit_wait",
    "watermark_read",
    "database_upsert",
    "screening",
    "database_read",
    "author_page_request",
    "config_merge",
    "config_save",
    "reporting",
]
Clock: TypeAlias = Callable[[], float]

_PHASES: tuple[ForumSyncTimingPhase, ...] = (
    "setup",
    "fetch",
    "forum_page_request",
    "rate_limit_wait",
    "watermark_read",
    "database_upsert",
    "screening",
    "database_read",
    "author_page_request",
    "config_merge",
    "config_save",
    "reporting",
)


@dataclass(frozen=True)
class ForumSyncTimingSnapshot:
    total_seconds: float
    setup_seconds: float
    fetch_seconds: float
    forum_page_request_seconds: float
    rate_limit_wait_seconds: float
    watermark_read_seconds: float
    database_upsert_seconds: float
    screening_seconds: float
    database_read_seconds: float
    author_page_request_seconds: float
    config_merge_seconds: float
    config_save_seconds: float
    reporting_seconds: float
    successful_page_count: int
    forum_page_request_attempt_count: int
    rate_limit_retry_count: int
    fetched_thread_count: int
    scanned_thread_count: int
    author_page_request_count: int
    config_saved: bool


class ForumSyncTimingCollector:
    def __init__(self, *, clock: Clock = perf_counter) -> None:
        self._clock = clock
        self._started_at = clock()
        self._seconds: dict[ForumSyncTimingPhase, float] = {
            phase: 0.0 for phase in _PHASES
        }
        self._successful_page_count = 0
        self._forum_page_request_attempt_count = 0
        self._rate_limit_retry_count = 0
        self._fetched_thread_count = 0
        self._scanned_thread_count = 0
        self._author_page_request_count = 0
        self._config_saved = False

    @contextmanager
    def measure(self, phase: ForumSyncTimingPhase) -> Generator[None]:
        started_at = self._clock()
        try:
            yield
        finally:
            elapsed_seconds = max(0.0, self._clock() - started_at)
            self._seconds[phase] += elapsed_seconds

    def record_forum_page_request_attempt(self) -> None:
        self._forum_page_request_attempt_count += 1

    def record_successful_forum_page(self, thread_count: int) -> None:
        self._successful_page_count += 1
        self._fetched_thread_count += thread_count

    def record_rate_limit_retry(self) -> None:
        self._rate_limit_retry_count += 1

    def record_scanned_threads(self, thread_count: int) -> None:
        self._scanned_thread_count += thread_count

    def record_author_page_request(self) -> None:
        self._author_page_request_count += 1

    def record_config_saved(self) -> None:
        self._config_saved = True

    def snapshot(self) -> ForumSyncTimingSnapshot:
        return ForumSyncTimingSnapshot(
            total_seconds=max(0.0, self._clock() - self._started_at),
            setup_seconds=self._seconds["setup"],
            fetch_seconds=self._seconds["fetch"],
            forum_page_request_seconds=self._seconds["forum_page_request"],
            rate_limit_wait_seconds=self._seconds["rate_limit_wait"],
            watermark_read_seconds=self._seconds["watermark_read"],
            database_upsert_seconds=self._seconds["database_upsert"],
            screening_seconds=self._seconds["screening"],
            database_read_seconds=self._seconds["database_read"],
            author_page_request_seconds=self._seconds["author_page_request"],
            config_merge_seconds=self._seconds["config_merge"],
            config_save_seconds=self._seconds["config_save"],
            reporting_seconds=self._seconds["reporting"],
            successful_page_count=self._successful_page_count,
            forum_page_request_attempt_count=(
                self._forum_page_request_attempt_count
            ),
            rate_limit_retry_count=self._rate_limit_retry_count,
            fetched_thread_count=self._fetched_thread_count,
            scanned_thread_count=self._scanned_thread_count,
            author_page_request_count=self._author_page_request_count,
            config_saved=self._config_saved,
        )

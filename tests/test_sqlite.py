from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from nga_tools.config import get_config, use_config_override
from nga_tools.core.sqlite import (
    effective_backup_sqlite_read_concurrency,
    sqlite_operation,
    use_backup_sqlite_concurrency,
)


def test_sqlite_concurrency_gate_limits_peak_to_configured_value() -> None:
    active = 0
    peak = 0
    active_lock = threading.Lock()
    saturated = threading.Event()
    release = threading.Event()

    def worker() -> None:
        nonlocal active, peak
        with sqlite_operation():
            with active_lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    saturated.set()
            assert release.wait(timeout=5)
            with active_lock:
                active -= 1

    limited_config = replace(get_config(), backup_sqlite_concurrency=2)
    with use_config_override(limited_config):
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker) for _index in range(4)]
            assert saturated.wait(timeout=5)
            release.set()
            for future in futures:
                future.result(timeout=5)

    assert peak == 2


def test_sqlite_concurrency_gate_is_thread_reentrant() -> None:
    limited_config = replace(get_config(), backup_sqlite_concurrency=1)

    def nested() -> bool:
        with sqlite_operation():
            with sqlite_operation():
                return True

    with use_config_override(limited_config):
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(nested).result(timeout=5)


def test_sqlite_read_gate_scales_with_workers_and_caps_at_eight() -> None:
    limited_config = replace(get_config(), backup_sqlite_concurrency=2)
    with use_config_override(limited_config):
        assert effective_backup_sqlite_read_concurrency(1) == 2
        assert effective_backup_sqlite_read_concurrency(6) == 6
        assert effective_backup_sqlite_read_concurrency(20) == 8


def test_sqlite_reads_do_not_wait_for_saturated_write_gate() -> None:
    write_started = threading.Event()
    release_write = threading.Event()
    read_completed = threading.Event()
    limited_config = replace(get_config(), backup_sqlite_concurrency=1)

    def write_worker() -> None:
        with sqlite_operation("write"):
            write_started.set()
            assert release_write.wait(timeout=5)

    def read_worker() -> None:
        with sqlite_operation("read"):
            read_completed.set()

    with (
        use_config_override(limited_config),
        use_backup_sqlite_concurrency(4),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        write_future = executor.submit(write_worker)
        assert write_started.wait(timeout=2)
        read_future = executor.submit(read_worker)
        assert read_completed.wait(timeout=2)
        read_future.result(timeout=2)
        release_write.set()
        write_future.result(timeout=2)


def test_sqlite_read_gate_uses_command_worker_limit() -> None:
    active = 0
    peak = 0
    active_lock = threading.Lock()
    saturated = threading.Event()
    release = threading.Event()
    limited_config = replace(get_config(), backup_sqlite_concurrency=2)

    def worker() -> None:
        nonlocal active, peak
        with sqlite_operation("read"):
            with active_lock:
                active += 1
                peak = max(peak, active)
                if active == 4:
                    saturated.set()
            assert release.wait(timeout=5)
            with active_lock:
                active -= 1

    with (
        use_config_override(limited_config),
        use_backup_sqlite_concurrency(4),
        ThreadPoolExecutor(max_workers=6) as executor,
    ):
        futures = [executor.submit(worker) for _index in range(6)]
        assert saturated.wait(timeout=5)
        release.set()
        for future in futures:
            future.result(timeout=5)

    assert peak == 4

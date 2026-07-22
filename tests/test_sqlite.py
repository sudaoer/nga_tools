from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from nga_tools.config import get_config, use_config_override
from nga_tools.core.sqlite import sqlite_operation


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

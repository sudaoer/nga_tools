from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from nga_tools.backup import image_store
from nga_tools.backup.image_index_writer import (
    image_index_writer_metrics,
    use_image_index_writer,
)
from nga_tools.backup.image_store_metrics import (
    image_store_metrics,
    record_image_store_attempt,
    record_image_store_completed,
    time_image_store_phase,
    use_image_store_metrics,
)
from nga_tools.core.image_download_runtime import (
    ImageDownloadRuntime,
    _AttemptFailure,
)


def _download_task(name: str, tmp_path: Path) -> image_store.utils.DownloadTask:
    return {
        "url": f"https://example.com/{name}.png",
        "save_path": str(tmp_path / name),
    }


def _success_result(
    item: image_store.utils.DownloadTask,
) -> image_store.utils.DownloadFileResult:
    return {
        "url": item["url"],
        "save_path": item["save_path"],
        "success": True,
    }


def _image_url(name: str) -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202607/13/{name}.png"
    )


class ImageDownloadRuntimeTest:
    def test_result_delivery_and_callback_metrics_return_to_zero(
        self,
        tmp_path: Path,
    ) -> None:
        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult:
            return _success_result(item)

        runtime = ImageDownloadRuntime(2)
        runtime._download_attempt = MethodType(fake_attempt, runtime)
        try:
            runtime.download_streaming(
                [_download_task(str(index), tmp_path) for index in range(8)],
                retries=0,
                backoff_factor=0,
                retry_statuses=(),
                batch_limit=2,
                on_progress=lambda _current, _total, _result: time.sleep(0.002),
            )
            metrics = runtime.snapshot()
        finally:
            runtime.close()

        assert metrics.pending_results == 0
        assert metrics.peak_pending_results >= 1
        assert metrics.result_delivery_wait_seconds > 0
        assert metrics.progress_callback_seconds >= 0.008

    def test_cancelled_callback_discards_pending_result_metrics(
        self,
        tmp_path: Path,
    ) -> None:
        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult:
            return _success_result(item)

        def fail_callback(
            _current: int,
            _total: int,
            _result: image_store.utils.DownloadFileResult,
        ) -> None:
            raise RuntimeError("stop consuming")

        runtime = ImageDownloadRuntime(4)
        runtime._download_attempt = MethodType(fake_attempt, runtime)
        with pytest.raises(RuntimeError, match="stop consuming"):
            runtime.download_streaming(
                [_download_task(str(index), tmp_path) for index in range(100)],
                retries=0,
                backoff_factor=0,
                retry_statuses=(),
                batch_limit=4,
                on_progress=fail_callback,
            )
        runtime.close()

        metrics = runtime.snapshot()
        assert metrics.active_downloads == 0
        assert metrics.pending_results == 0

    def test_large_batch_uses_only_fixed_worker_tasks(self, tmp_path: Path) -> None:
        original_create_task = asyncio.create_task
        created_tasks = 0
        create_lock = threading.Lock()

        def counted_create_task(coroutine):
            nonlocal created_tasks
            with create_lock:
                created_tasks += 1
            return original_create_task(coroutine)

        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult:
            return _success_result(item)

        with patch(
            "nga_tools.core.image_download_runtime.asyncio.create_task",
            side_effect=counted_create_task,
        ):
            runtime = ImageDownloadRuntime(4)
            runtime._download_attempt = MethodType(fake_attempt, runtime)
            try:
                summary = runtime.download(
                    [_download_task(str(index), tmp_path) for index in range(500)],
                    retries=0,
                    backoff_factor=0,
                    retry_statuses=(),
                    batch_limit=4,
                    on_progress=None,
                )
                runtime_threads = [
                    thread
                    for thread in threading.enumerate()
                    if thread.name == "nga-image-runtime"
                ]
                assert len(summary["succeeded"]) == 500
                assert created_tasks == 4
                assert len(runtime_threads) == 1
            finally:
                runtime.close()

    def test_streaming_mode_reports_without_retaining_results(
        self,
        tmp_path: Path,
    ) -> None:
        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult:
            return _success_result(item)

        completed = 0

        def on_progress(
            current: int,
            total: int,
            _result: image_store.utils.DownloadFileResult,
        ) -> None:
            nonlocal completed
            completed = current
            assert total == 500

        runtime = ImageDownloadRuntime(4)
        runtime._download_attempt = MethodType(fake_attempt, runtime)
        try:
            result = runtime.download_streaming(
                [_download_task(str(index), tmp_path) for index in range(500)],
                retries=0,
                backoff_factor=0,
                retry_statuses=(),
                batch_limit=4,
                on_progress=on_progress,
            )
        finally:
            runtime.close()

        assert result is None
        assert completed == 500

    def test_batches_are_scheduled_fairly(self, tmp_path: Path) -> None:
        initial_started = threading.Event()
        release_initial = threading.Event()
        starts: list[str] = []
        start_lock = threading.Lock()

        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult:
            with start_lock:
                starts.append(item["url"])
                if len(starts) >= 4:
                    initial_started.set()
            if item["url"].endswith(("large-1.png", "large-2.png", "large-3.png", "large-4.png")):
                while not release_initial.is_set():
                    await asyncio.sleep(0.001)
            return _success_result(item)

        runtime = ImageDownloadRuntime(4)
        runtime._download_attempt = MethodType(fake_attempt, runtime)
        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                large = executor.submit(
                    runtime.download,
                    [
                        _download_task(f"large-{index}", tmp_path)
                        for index in range(1, 21)
                    ],
                    retries=0,
                    backoff_factor=0,
                    retry_statuses=(),
                    batch_limit=4,
                    on_progress=None,
                )
                assert initial_started.wait(timeout=2)
                small = executor.submit(
                    runtime.download,
                    [_download_task("small", tmp_path)],
                    retries=0,
                    backoff_factor=0,
                    retry_statuses=(),
                    batch_limit=4,
                    on_progress=None,
                )
                release_initial.set()
                assert len(large.result(timeout=3)["succeeded"]) == 20
                assert len(small.result(timeout=3)["succeeded"]) == 1
        finally:
            runtime.close()

        small_index = next(
            index for index, url in enumerate(starts) if url.endswith("small.png")
        )
        assert small_index - 4 < 4

    def test_retry_delay_releases_worker_for_another_batch(
        self,
        tmp_path: Path,
    ) -> None:
        first_failure = threading.Event()
        starts: list[str] = []
        attempts: dict[str, int] = {}

        async def fake_attempt(
            _runtime: ImageDownloadRuntime,
            _session: object,
            item: image_store.utils.DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> image_store.utils.DownloadFileResult | _AttemptFailure:
            name = item["url"]
            attempts[name] = attempts.get(name, 0) + 1
            starts.append(name)
            if name.endswith("retry.png") and attempts[name] == 1:
                first_failure.set()
                return _AttemptFailure(
                    error=RuntimeError("temporary"),
                    failure_kind="connection",
                    http_status=None,
                    retryable=True,
                )
            return _success_result(item)

        callback_threads: list[int] = []
        caller_thread = threading.get_ident()
        runtime = ImageDownloadRuntime(1)
        runtime._download_attempt = MethodType(fake_attempt, runtime)
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                retry_future = executor.submit(
                    runtime.download,
                    [_download_task("retry", tmp_path)],
                    retries=1,
                    backoff_factor=0.2,
                    retry_statuses=(),
                    batch_limit=1,
                    on_progress=None,
                )
                assert first_failure.wait(timeout=2)
                direct = runtime.download(
                    [_download_task("direct", tmp_path)],
                    retries=0,
                    backoff_factor=0,
                    retry_statuses=(),
                    batch_limit=1,
                    on_progress=lambda _completed, _total, _result: (
                        callback_threads.append(threading.get_ident())
                    ),
                )
                assert len(direct["succeeded"]) == 1
                assert len(retry_future.result(timeout=3)["succeeded"]) == 1
        finally:
            runtime.close()

        assert [url.rsplit("/", 1)[-1] for url in starts] == [
            "retry.png",
            "direct.png",
            "retry.png",
        ]
        assert callback_threads == [caller_thread]


class ImageIndexWriterTest:
    def test_burst_is_committed_in_at_most_four_transactions(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        mappings = [
            (
                _image_url(f"burst-{index}"),
                output_dir / "images_unique" / f"{index}.png",
            )
            for index in range(1000)
        ]
        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            use_image_index_writer(),
        ):
            _image_mappings, future = image_store.enqueue_image_mappings(mappings)
            future.result(timeout=5)
            metrics = image_index_writer_metrics()
            assert metrics is not None
            assert metrics.rows_written == 1000
            assert metrics.transactions <= 4
            assert metrics.requests_submitted == 1
            assert metrics.max_transaction_rows <= 256
            assert metrics.queue_put_seconds >= 0
            assert metrics.coalesce_wait_seconds >= 0
            assert metrics.transaction_seconds >= 0

        with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM image_mappings"
            ).fetchone()[0]
        assert count == 1000

    def test_transaction_failure_never_reports_success(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        with patch("nga_tools.backup.image_store.get_config", return_value=config):
            image_store.upsert_image_mapping(
                _image_url("seed"),
                output_dir / "images_unique" / "seed.png",
            )
            with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_mapping
                    BEFORE INSERT ON image_mappings
                    BEGIN
                        SELECT RAISE(FAIL, 'forced mapping failure');
                    END
                    """
                )
                connection.commit()

            with use_image_index_writer():
                _mappings, future = image_store.enqueue_image_mappings(
                    [
                        (
                            _image_url("rejected"),
                            output_dir / "images_unique" / "rejected.png",
                        )
                    ]
                )
                with pytest.raises(sqlite3.IntegrityError):
                    future.result(timeout=2)

            with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
                rejected = connection.execute(
                    "SELECT 1 FROM image_mappings WHERE url = ?",
                    (_image_url("rejected"),),
                ).fetchone()
        assert rejected is None


class ImageStoreMetricsTest:
    def test_metrics_aggregate_across_worker_threads(self) -> None:
        def record_one(index: int) -> None:
            record_image_store_attempt()
            with time_image_store_phase("source_validation"):
                time.sleep(0.0001)
            record_image_store_completed(
                reused=index % 2 == 0,
                collision=index % 5 == 0,
            )

        with use_image_store_metrics():
            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(record_one, range(100)))
            metrics = image_store_metrics()

        assert metrics is not None
        assert metrics.store_attempts == 100
        assert metrics.stores_completed == 100
        assert metrics.reused_files == 50
        assert metrics.collision_files == 20
        assert metrics.source_validation_seconds > 0

    def test_store_phases_and_mapping_wait_are_command_scoped(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        source = tmp_path / "source.png"
        Image.new("RGB", (2, 2), color="white").save(source)

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            use_image_index_writer(),
            use_image_store_metrics(),
        ):
            first = image_store.store_existing_image(
                source,
                _image_url("metrics-first"),
            )
            second = image_store.store_existing_image(
                source,
                _image_url("metrics-second"),
            )
            metrics = image_store_metrics()

        assert first["reused"] is False
        assert second["reused"] is True
        assert metrics is not None
        assert metrics.store_attempts == 2
        assert metrics.stores_completed == 2
        assert metrics.stores_failed == 0
        assert metrics.reused_files == 1
        assert metrics.mapping_submissions == 2
        assert metrics.mapping_rows == 2
        assert metrics.mapping_failures == 0
        assert metrics.source_validation_seconds > 0
        assert metrics.fallback_hash_seconds > 0
        assert metrics.format_detection_seconds > 0
        assert metrics.target_selection_seconds > 0
        assert metrics.content_compare_seconds > 0
        assert metrics.file_placement_seconds > 0
        assert metrics.final_validation_seconds > 0
        assert metrics.mapping_submit_seconds > 0
        assert metrics.mapping_wait_seconds > 0

    def test_empty_scope_reports_zero_metrics(self) -> None:
        with use_image_store_metrics():
            metrics = image_store_metrics()

        assert metrics is not None
        assert metrics.store_attempts == 0
        assert metrics.mapping_rows == 0
        assert metrics.single_flight_waits == 0


class ImageSingleFlightTest:
    def test_shared_condition_does_not_complete_another_claim(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        first_done = threading.Event()
        second_done = threading.Event()

        def wait_for_claim(
            claim: image_store._ImageURLClaim,
            done: threading.Event,
        ) -> None:
            image_store._wait_image_url_claim(claim)
            done.set()

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            use_image_store_metrics(),
        ):
            first_key, first_claim, first_owner = image_store._claim_image_url(
                _image_url("shared-condition-first")
            )
            second_key, second_claim, second_owner = image_store._claim_image_url(
                _image_url("shared-condition-second")
            )
            assert first_owner is True
            assert second_owner is True
            assert not hasattr(first_claim, "event")

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_waiter = executor.submit(
                    wait_for_claim,
                    first_claim,
                    first_done,
                )
                second_waiter = executor.submit(
                    wait_for_claim,
                    second_claim,
                    second_done,
                )
                image_store._release_image_url_claim(
                    first_key,
                    first_claim,
                    error=RuntimeError("first failed"),
                )
                assert first_done.wait(timeout=2)
                assert not second_done.wait(timeout=0.05)
                image_store._release_image_url_claim(
                    second_key,
                    second_claim,
                    error=RuntimeError("second failed"),
                )
                assert second_done.wait(timeout=2)
                first_waiter.result(timeout=2)
                second_waiter.result(timeout=2)
            metrics = image_store_metrics()

        assert metrics is not None
        assert metrics.single_flight_waits >= 1
        assert metrics.single_flight_wait_seconds > 0

    def test_duplicate_url_downloads_once_and_waits_for_durable_mapping(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("single-flight")
        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_claimed = threading.Event()
        calls = 0
        call_lock = threading.Lock()
        real_claim = image_store._claim_image_url

        def observed_claim(image_url):
            result = real_claim(image_url)
            if not result[2]:
                waiter_claimed.set()
            return result

        def fake_download(tasks, on_progress=None, **_kwargs):
            nonlocal calls
            with call_lock:
                calls += 1
            owner_started.set()
            assert release_owner.wait(timeout=2)
            task = tasks[0]
            Image.new("RGB", (2, 2), color="white").save(
                task["save_path"],
                format="PNG",
            )
            result = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": True,
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [result], "failed": []}

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store._claim_image_url",
                side_effect=observed_claim,
            ),
            use_image_index_writer(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                owner = executor.submit(image_store.download_image_tasks, [{"url": url}])
                assert owner_started.wait(timeout=2)
                waiter = executor.submit(
                    image_store.download_image_tasks_compact,
                    [{"url": url}],
                )
                assert waiter_claimed.wait(timeout=2)
                release_owner.set()
                owner_result = owner.result(timeout=3)
                waiter_result = waiter.result(timeout=3)

            assert calls == 1
            assert len(owner_result["succeeded"]) == 1
            assert waiter_result["succeeded_count"] == 1
            assert waiter_result["failed"] == []
            assert image_store.mapped_image_path_for_url(url) is not None

    def test_failed_owner_wakes_waiter_and_later_call_retries(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("single-flight-retry")
        calls = 0

        def fake_download(tasks, on_progress=None, **_kwargs):
            nonlocal calls
            calls += 1
            task = tasks[0]
            if calls == 1:
                result = {
                    "url": task["url"],
                    "save_path": task["save_path"],
                    "success": False,
                    "error": "temporary",
                    "failure_kind": "connection",
                }
                if on_progress is not None:
                    on_progress(1, 1, result)
                return {"succeeded": [], "failed": [result]}
            Image.new("RGB", (2, 2), color="white").save(
                task["save_path"],
                format="PNG",
            )
            result = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": True,
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [result], "failed": []}

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.download_files_streaming",
                side_effect=fake_download,
            ),
            use_image_index_writer(),
        ):
            first = image_store.download_image_tasks([{"url": url}])
            second = image_store.download_image_tasks([{"url": url}])

        assert len(first["failed"]) == 1
        assert len(second["succeeded"]) == 1
        assert calls == 2

    def test_failed_owner_result_is_shared_with_waiter(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("single-flight-shared-failure")
        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_claimed = threading.Event()
        calls = 0
        real_claim = image_store._claim_image_url

        def observed_claim(image_url):
            result = real_claim(image_url)
            if not result[2]:
                waiter_claimed.set()
            return result

        def fake_download(tasks, on_progress=None, **_kwargs):
            nonlocal calls
            calls += 1
            owner_started.set()
            assert release_owner.wait(timeout=2)
            task = tasks[0]
            result = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": False,
                "error": "temporary",
                "failure_kind": "connection",
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [], "failed": [result]}

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store._claim_image_url",
                side_effect=observed_claim,
            ),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                owner = executor.submit(image_store.download_image_tasks, [{"url": url}])
                assert owner_started.wait(timeout=2)
                waiter = executor.submit(image_store.download_image_tasks, [{"url": url}])
                assert waiter_claimed.wait(timeout=2)
                release_owner.set()
                owner_result = owner.result(timeout=3)
                waiter_result = waiter.result(timeout=3)

        assert calls == 1
        assert len(owner_result["failed"]) == 1
        assert len(waiter_result["failed"]) == 1

    def test_cancelled_owner_wakes_waiter_and_releases_claim(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("single-flight-cancel")
        owner_started = threading.Event()
        release_owner = threading.Event()
        waiter_claimed = threading.Event()
        calls = 0
        real_claim = image_store._claim_image_url

        def observed_claim(image_url):
            result = real_claim(image_url)
            if not result[2]:
                waiter_claimed.set()
            return result

        def fake_download(tasks, on_progress=None, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                owner_started.set()
                assert release_owner.wait(timeout=2)
                raise RuntimeError("owner cancelled")
            task = tasks[0]
            Image.new("RGB", (2, 2), color="white").save(
                task["save_path"],
                format="PNG",
            )
            result = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": True,
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [result], "failed": []}

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store._claim_image_url",
                side_effect=observed_claim,
            ),
            use_image_index_writer(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                owner = executor.submit(image_store.download_image_tasks, [{"url": url}])
                assert owner_started.wait(timeout=2)
                waiter = executor.submit(image_store.download_image_tasks, [{"url": url}])
                assert waiter_claimed.wait(timeout=2)
                release_owner.set()
                with pytest.raises(RuntimeError, match="owner cancelled"):
                    owner.result(timeout=3)
                with pytest.raises(RuntimeError, match="共享图片下载失败"):
                    waiter.result(timeout=3)

            retried = image_store.download_image_tasks([{"url": url}])

        assert len(retried["succeeded"]) == 1
        assert calls == 2


class ImageHashStripeTest:
    def test_different_hashes_place_files_concurrently(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        first = tmp_path / "first.png"
        second = tmp_path / "second.png"
        Image.new("RGB", (2, 2), color="white").save(first)
        Image.new("RGB", (2, 2), color="black").save(second)
        hashes = {
            str(first): "00" + "a" * 62,
            str(second): "01" + "b" * 62,
        }
        barrier = threading.Barrier(2)
        real_target = image_store._target_path_for_download

        def tracked_target(temp_path, image_hash, extension):
            barrier.wait(timeout=2)
            return real_target(temp_path, image_hash, extension)

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.sha256",
                side_effect=lambda path: hashes[path],
            ),
            patch(
                "nga_tools.backup.image_store._target_path_for_download",
                side_effect=tracked_target,
            ),
            use_image_index_writer(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    executor.submit(
                        image_store.store_existing_image,
                        path,
                        _image_url(f"stripe-{index}"),
                    )
                    for index, path in enumerate((first, second), start=1)
                ]
                assert all(future.result(timeout=3) for future in results)

    def test_same_hash_is_serialized_for_collision_selection(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        first = tmp_path / "same-first.png"
        second = tmp_path / "same-second.png"
        Image.new("RGB", (2, 2), color="white").save(first)
        Image.new("RGB", (2, 2), color="black").save(second)
        active = 0
        peak_active = 0
        active_lock = threading.Lock()
        real_target = image_store._target_path_for_download

        def tracked_target(temp_path, image_hash, extension):
            nonlocal active, peak_active
            with active_lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                time.sleep(0.03)
                return real_target(temp_path, image_hash, extension)
            finally:
                with active_lock:
                    active -= 1

        with (
            patch("nga_tools.backup.image_store.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.utils.sha256",
                return_value="aa" + "c" * 62,
            ),
            patch(
                "nga_tools.backup.image_store._target_path_for_download",
                side_effect=tracked_target,
            ),
            use_image_index_writer(),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                results = [
                    executor.submit(
                        image_store.store_existing_image,
                        path,
                        _image_url(f"same-stripe-{index}"),
                    )
                    for index, path in enumerate((first, second), start=1)
                ]
                assert all(future.result(timeout=3) for future in results)

        assert peak_active == 1

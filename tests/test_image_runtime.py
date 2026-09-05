from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import aiohttp
import pytest
from PIL import Image

from nga_tools.backup import image_index, image_store
from nga_tools.backup.image_retry import (
    pending_image_retries_after_attempt,
    select_image_retries,
)
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
from nga_tools.backup.image_store_runtime import (
    image_store_runtime_metrics,
    use_image_store_runtime,
)
from nga_tools.core.image_download_runtime import (
    _ATTACHMENT_FALLBACK_PROBE_TIMEOUT_SECONDS,
    DownloadRuntime,
    _AttemptFailure,
)
from nga_tools.core.download_types import DownloadFileResult, DownloadTask
from nga_tools.replay.offline import use_replay_network_policy


def _download_task(name: str, tmp_path: Path) -> DownloadTask:
    return {
        "url": f"https://example.com/{name}.png",
        "save_path": str(tmp_path / name),
    }


def _success_result(
    item: DownloadTask,
) -> DownloadFileResult:
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


class _FakeChunkedContent:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def iter_chunked(self, _chunk_size: int):
        async def chunks():
            yield self._payload

        return chunks()


class _FakeResponse:
    status = 200

    def __init__(
        self,
        payload: bytes,
        *,
        status: int = 200,
        url: str = "https://example.com/image.png",
    ) -> None:
        self.status = status
        self.content_length = len(payload)
        self.headers: dict[str, str] = {}
        self.content = _FakeChunkedContent(payload)
        self.request_info = SimpleNamespace(real_url=url)
        self.history: tuple[object, ...] = ()

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _RaisingContext:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    async def __aenter__(self) -> object:
        raise self._error

    async def __aexit__(self, *_args: object) -> None:
        return None


type _FakeSessionResponse = _RaisingContext | _FakeResponse
type _FallbackOutcome = BaseException | int | str


def _dns_failure() -> aiohttp.ClientConnectorDNSError:
    return aiohttp.ClientConnectorDNSError(
        SimpleNamespace(host="img.nga.cn", port=443, ssl=True),
        OSError("DNS failure"),
    )


class _PayloadFailureResponse(_FakeResponse):
    def __init__(self, *, url: str) -> None:
        super().__init__(b"payload", url=url)
        self.content_length += 1


class _FallbackSession:
    def __init__(
        self,
        *,
        fail_host: str | None = "img.nga.178.com",
        error: BaseException | None = None,
        fail_all: bool = False,
        outcomes: dict[str, _FallbackOutcome] | None = None,
    ) -> None:
        self.fail_host = fail_host
        self.error = error
        self.fail_all = fail_all
        self.outcomes = outcomes or {}
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> _FakeSessionResponse:
        self.calls.append((url, kwargs))
        outcome = self.outcomes.get(urlsplit(url).hostname or "")
        if outcome is not None:
            if isinstance(outcome, BaseException):
                return _RaisingContext(outcome)
            if outcome == "payload":
                return _PayloadFailureResponse(url=url)
            if isinstance(outcome, int):
                return _FakeResponse(b"", status=outcome, url=url)
            raise AssertionError(f"unknown fallback outcome: {outcome!r}")
        if (
            self.error is not None
            and (
                self.fail_all
                or (
                    self.fail_host is not None
                    and urlsplit(url).hostname == self.fail_host
                )
            )
        ):
            return _RaisingContext(self.error)
        return _FakeResponse(b"payload", url=url)


class ImageDownloadRuntimeTest:
    def test_connection_disconnect_is_retryable_connection_failure(
        self,
        tmp_path: Path,
    ) -> None:
        class DisconnectContext:
            async def __aenter__(self):
                raise aiohttp.ServerDisconnectedError

            async def __aexit__(self, *_args: object) -> None:
                return None

        class DisconnectSession:
            def get(self, *_args: object, **_kwargs: object) -> DisconnectContext:
                return DisconnectContext()

        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(
                    DisconnectSession(),
                    _download_task("disconnect", tmp_path),
                    (),
                )
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "connection"
        assert result.retryable is True

    def test_content_length_mismatch_is_retryable_payload_failure(
        self,
        tmp_path: Path,
    ) -> None:
        class ShortContent:
            def iter_chunked(self, _chunk_size: int):
                async def chunks():
                    yield b"short"

                return chunks()

        class ShortResponse:
            status = 200
            content_length = 10
            headers: dict[str, str] = {}
            content = ShortContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

        class ShortSession:
            def get(self, *_args: object, **_kwargs: object) -> ShortResponse:
                return ShortResponse()

        task = _download_task("short-payload", tmp_path)
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(ShortSession(), task, ())
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "payload"
        assert result.retryable is True
        assert not Path(task["save_path"]).exists()

    def test_primary_connection_failure_falls_back_to_alias(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-connection", tmp_path)
        task["url"] = _image_url("fallback-connection")
        session = _FallbackSession(
            fail_host="img.nga.178.com",
            error=aiohttp.ServerDisconnectedError(),
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert result["url"] == task["url"]
        assert Path(task["save_path"]).read_bytes() == b"payload"
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == ["img.nga.178.com", "img.nga.cn"]

    def test_primary_timeout_falls_back_to_alias(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-timeout", tmp_path)
        task["url"] = _image_url("fallback-timeout")
        session = _FallbackSession(
            fail_host="img.nga.178.com",
            error=asyncio.TimeoutError(),
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert Path(task["save_path"]).read_bytes() == b"payload"
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == ["img.nga.178.com", "img.nga.cn"]

    def test_primary_missing_attachment_falls_back_to_alias(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-404-alias", tmp_path)
        task["url"] = _image_url("fallback-404-alias")
        session = _FallbackSession(
            outcomes={"img.nga.178.com": 404},
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(runtime._download_attempt(session, task, ()))
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert Path(task["save_path"]).read_bytes() == b"payload"
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == ["img.nga.178.com", "img.nga.cn"]

    def test_new_domain_failure_falls_back_to_legacy(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-reverse", tmp_path)
        task["url"] = (
            "https://img.nga.cn/attachments/"
            "mon_202607/13/fallback-reverse.png"
        )
        session = _FallbackSession(
            fail_host="img.nga.cn",
            error=aiohttp.ServerDisconnectedError(),
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == ["img.nga.cn", "img.nga.178.com"]

    def test_both_hosts_failing_returns_first_same_rank_failure(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-both", tmp_path)
        task["url"] = _image_url("fallback-both")
        first_error = asyncio.TimeoutError("legacy first")
        alias_error = aiohttp.ClientConnectionError("new second")
        session = _FallbackSession(
            outcomes={
                "img.nga.178.com": first_error,
                "img.nga.cn": alias_error,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "timeout"
        assert str(result.error).startswith(
            "All download URLs failed: "
            "https://img.nga.178.com/attachments/mon_202607/13/fallback-both.png "
            "(timeout): legacy first; "
            "https://img.nga.cn/attachments/mon_202607/13/fallback-both.png "
            "(connection): new second"
        )
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == ["img.nga.178.com", "img.nga.cn"]
        assert not Path(task["save_path"]).exists()

    @pytest.mark.parametrize(
        "logical_host",
        ["img.nga.178.com", "img.nga.cn"],
    )
    @pytest.mark.parametrize(
        ("primary_outcome", "alias_outcome", "expected_kind", "expected_status"),
        [
            (
                404,
                _dns_failure(),
                "http_4xx",
                404,
            ),
            (
                _dns_failure(),
                404,
                "http_4xx",
                404,
            ),
        ],
    )
    def test_missing_attachment_beats_unreachable_alias(
        self,
        tmp_path: Path,
        logical_host: str,
        primary_outcome: _FallbackOutcome,
        alias_outcome: _FallbackOutcome,
        expected_kind: str,
        expected_status: int,
    ) -> None:
        task = _download_task("fallback-404-network", tmp_path)
        task["url"] = (
            f"https://{logical_host}/attachments/"
            "mon_202607/13/fallback-404-network.png"
        )
        alias_host = (
            "img.nga.cn"
            if logical_host == "img.nga.178.com"
            else "img.nga.178.com"
        )
        session = _FallbackSession(
            outcomes={
                logical_host: primary_outcome,
                alias_host: alias_outcome,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(runtime._download_attempt(session, task, (429, 503)))
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == expected_kind
        assert result.http_status == expected_status
        assert result.retryable is False
        assert "img.nga.178.com" in str(result.error)
        assert "img.nga.cn" in str(result.error)
        assert "HTTP 404" in str(result.error)
        assert [
            urlsplit(call_url).hostname for call_url, _kwargs in session.calls
        ] == [logical_host, alias_host]

    def test_503_is_not_retryable_when_retry_statuses_are_empty(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-503-default", tmp_path)
        task["url"] = _image_url("fallback-503-default")
        session = _FallbackSession(
            outcomes={
                "img.nga.178.com": 503,
                "img.nga.cn": 404,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(runtime._download_attempt(session, task, ()))
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "http_5xx"
        assert result.http_status == 503
        assert result.retryable is False

    @pytest.mark.parametrize("temporary_status", [429, 503])
    @pytest.mark.parametrize("temporary_is_primary", [True, False])
    def test_retryable_http_failure_beats_missing_attachment(
        self,
        tmp_path: Path,
        temporary_status: int,
        temporary_is_primary: bool,
    ) -> None:
        task = _download_task("fallback-temporary", tmp_path)
        task["url"] = _image_url("fallback-temporary")
        primary_outcome: _FallbackOutcome = (
            temporary_status if temporary_is_primary else 404
        )
        alias_outcome: _FallbackOutcome = (
            404 if temporary_is_primary else temporary_status
        )
        session = _FallbackSession(
            outcomes={
                "img.nga.178.com": primary_outcome,
                "img.nga.cn": alias_outcome,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, (429, 503))
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.http_status == temporary_status
        assert result.failure_kind == (
            "http_4xx" if temporary_status < 500 else "http_5xx"
        )
        assert result.retryable is True
        assert "HTTP 404" in str(result.error)
        assert f"HTTP {temporary_status}" in str(result.error)

    def test_payload_failure_beats_non_retryable_http_failure(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-payload", tmp_path)
        task["url"] = _image_url("fallback-payload")
        session = _FallbackSession(
            outcomes={
                "img.nga.178.com": "payload",
                "img.nga.cn": 404,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(runtime._download_attempt(session, task, ()))
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "payload"
        assert result.http_status is None
        assert result.retryable is True
        assert "payload" in str(result.error)
        assert "HTTP 404" in str(result.error)

    def test_retryable_http_failure_beats_payload_failure(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-http-payload", tmp_path)
        task["url"] = _image_url("fallback-http-payload")
        session = _FallbackSession(
            outcomes={
                "img.nga.178.com": "payload",
                "img.nga.cn": 503,
            },
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, (503,))
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "http_5xx"
        assert result.http_status == 503
        assert result.retryable is True
        assert "payload" in str(result.error)
        assert "HTTP 503" in str(result.error)

    @pytest.mark.parametrize(
        ("case", "expected_calls", "expected_retry_count"),
        [("missing", 2, 0), ("network", 4, 1)],
    )
    def test_worker_fallback_failure_controls_retry_count(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        case: str,
        expected_calls: int,
        expected_retry_count: int,
    ) -> None:
        if case == "missing":
            outcomes: dict[str, _FallbackOutcome] = {
                "img.nga.178.com": 404,
                "img.nga.cn": _dns_failure(),
            }
        else:
            outcomes = {
                "img.nga.178.com": aiohttp.ClientConnectionError("legacy"),
                "img.nga.cn": aiohttp.ClientConnectionError("new"),
            }
        session = _FallbackSession(outcomes=outcomes)

        class SessionContext:
            async def __aenter__(self) -> _FallbackSession:
                return session

            async def __aexit__(self, *_args: object) -> None:
                return None

        def fake_client_session(**_kwargs: object) -> SessionContext:
            return SessionContext()

        monkeypatch.setattr(
            "nga_tools.core.image_download_runtime.aiohttp.ClientSession",
            fake_client_session,
        )
        task = _download_task(f"worker-{case}", tmp_path)
        task["url"] = _image_url(f"worker-{case}")
        runtime = DownloadRuntime(1)
        try:
            summary = runtime.download(
                [task],
                retries=1,
                backoff_factor=0,
                retry_statuses=(429, 503),
                batch_limit=1,
                on_progress=None,
            )
            metrics = runtime.snapshot()
        finally:
            runtime.close()

        assert len(summary["failed"]) == 1
        assert len(session.calls) == expected_calls
        assert metrics.retry_count == expected_retry_count
        if case == "missing":
            attempted_at = datetime(2026, 9, 5, tzinfo=timezone.utc)
            pending = pending_image_retries_after_attempt(
                (),
                summary["failed"],
                attempted_at=attempted_at,
            )
            assert pending[0].failure_kind == "http_4xx"
            assert pending[0].http_status == 404
            with patch(
                "nga_tools.backup.image_retry.shared_media_retry_ticket",
                return_value=0.99,
            ):
                selection = select_image_retries(
                    pending,
                    thread_target_key="123:456",
                    now=attempted_at + timedelta(hours=1),
                    max_interval=timedelta(hours=168),
                    force=False,
                )
            assert selection.due == ()
            assert selection.deferred == pending

    def test_primary_success_skips_alias(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-primary", tmp_path)
        task["url"] = _image_url("fallback-primary")
        session = _FallbackSession()
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert len(session.calls) == 1
        assert urlsplit(session.calls[0][0]).hostname == "img.nga.178.com"

    def test_legacy_host_gets_probe_timeout_and_alias_does_not(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-probe", tmp_path)
        task["url"] = _image_url("fallback-probe")
        session = _FallbackSession(
            fail_host="img.nga.178.com",
            error=aiohttp.ServerDisconnectedError(),
        )
        runtime = DownloadRuntime(1)
        try:
            asyncio.run(runtime._download_attempt(session, task, ()))
        finally:
            runtime.close()

        old_url, old_kwargs = session.calls[0]
        assert urlsplit(old_url).hostname == "img.nga.178.com"
        timeout = old_kwargs["timeout"]
        assert isinstance(timeout, aiohttp.ClientTimeout)
        assert timeout.total == _ATTACHMENT_FALLBACK_PROBE_TIMEOUT_SECONDS
        _alias_url, alias_kwargs = session.calls[1]
        assert "timeout" not in alias_kwargs

    def test_explicit_request_url_disables_alias_fallback(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-explicit", tmp_path)
        task["url"] = _image_url("fallback-explicit")
        explicit_url = "https://example.com/proxy.png"
        task["request_url"] = explicit_url
        explicit_error = aiohttp.ServerDisconnectedError("explicit")
        session = _FallbackSession(
            fail_host="example.com",
            error=explicit_error,
        )
        runtime = DownloadRuntime(1)
        try:
            result = asyncio.run(
                runtime._download_attempt(session, task, ())
            )
        finally:
            runtime.close()

        assert isinstance(result, _AttemptFailure)
        assert result.failure_kind == "connection"
        assert result.error is explicit_error
        assert [call_url for call_url, _kwargs in session.calls] == [
            explicit_url,
        ]

    def test_replay_policy_never_uses_external_alias(
        self,
        tmp_path: Path,
    ) -> None:
        task = _download_task("fallback-replay", tmp_path)
        task["url"] = _image_url("fallback-replay")
        session = _FallbackSession()
        runtime = DownloadRuntime(1)
        try:
            with use_replay_network_policy("http://127.0.0.1:8765"):
                result = asyncio.run(
                    runtime._download_attempt(session, task, ())
                )
        finally:
            runtime.close()

        assert not isinstance(result, _AttemptFailure)
        assert result["success"] is True
        assert len(session.calls) == 1
        assert session.calls[0][0].startswith(
            "http://127.0.0.1:8765/__replay__/image?"
        )

    def test_result_delivery_and_callback_metrics_return_to_zero(
        self,
        tmp_path: Path,
    ) -> None:
        async def fake_attempt(
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult:
            return _success_result(item)

        runtime = DownloadRuntime(2)
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
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult:
            return _success_result(item)

        def fail_callback(
            _current: int,
            _total: int,
            _result: DownloadFileResult,
        ) -> None:
            raise RuntimeError("stop consuming")

        runtime = DownloadRuntime(4)
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
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult:
            return _success_result(item)

        with patch(
            "nga_tools.core.image_download_runtime.asyncio.create_task",
            side_effect=counted_create_task,
        ):
            runtime = DownloadRuntime(4)
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
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult:
            return _success_result(item)

        completed = 0

        def on_progress(
            current: int,
            total: int,
            _result: DownloadFileResult,
        ) -> None:
            nonlocal completed
            completed = current
            assert total == 500

        runtime = DownloadRuntime(4)
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
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult:
            with start_lock:
                starts.append(item["url"])
                if len(starts) >= 4:
                    initial_started.set()
            if item["url"].endswith(("large-1.png", "large-2.png", "large-3.png", "large-4.png")):
                while not release_initial.is_set():
                    await asyncio.sleep(0.001)
            return _success_result(item)

        runtime = DownloadRuntime(4)
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
            _runtime: DownloadRuntime,
            _session: object,
            item: DownloadTask,
            _retry_statuses: tuple[int, ...],
        ) -> DownloadFileResult | _AttemptFailure:
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
        runtime = DownloadRuntime(1)
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
    def test_burst_is_committed_in_one_transaction(
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
            patch("nga_tools.config.get_config", return_value=config),
            use_image_index_writer(),
        ):
            _image_mappings, future = image_index.ImageIndexStore(
                output_dir
            ).enqueue_mappings(mappings)
            future.result(timeout=5)
            metrics = image_index_writer_metrics()
            assert metrics is not None
            assert metrics.rows_written == 1000
            assert metrics.transactions == 1
            assert metrics.requests_submitted == 1
            assert metrics.max_transaction_rows == 1000
            assert metrics.queue_put_seconds >= 0
            assert metrics.coalesce_wait_seconds == 0
            assert metrics.transaction_seconds >= 0

        with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM image_mappings"
            ).fetchone()[0]
        assert count == 1000

    def test_transaction_failure_never_reports_success(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        with patch("nga_tools.config.get_config", return_value=config):
            image_index.ImageIndexStore(output_dir).upsert_mapping(
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
                _mappings, future = image_index.ImageIndexStore(
                    output_dir
                ).enqueue_mappings(
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
            patch("nga_tools.config.get_config", return_value=config),
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
        assert metrics.precomputed_hash_hits == 0
        assert metrics.precomputed_hash_rejections == 0
        assert metrics.fallback_hashes == 2
        assert metrics.mapping_submissions == 2
        assert metrics.mapping_rows == 2
        assert metrics.mapping_failures == 0
        assert metrics.source_inspection_seconds > 0
        assert metrics.fallback_hash_seconds > 0
        assert metrics.source_validation_seconds == 0
        assert metrics.format_detection_seconds == 0
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

    def test_download_identity_skips_second_full_file_hash(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("precomputed-hash")

        def fake_download(tasks, on_progress=None, **_kwargs):
            task = tasks[0]
            Image.new("RGB", (2, 2), color="white").save(
                task["save_path"],
                format="PNG",
            )
            payload = Path(task["save_path"]).read_bytes()
            result: DownloadFileResult = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": True,
                "content_sha256": sha256(payload).hexdigest(),
                "content_bytes": len(payload),
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [result], "failed": []}

        with (
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store.sha256",
                side_effect=AssertionError("unexpected fallback hash"),
            ),
            use_image_index_writer(),
            use_image_store_metrics(),
        ):
            summary = image_store.download_image_tasks([{"url": url}])
            metrics = image_store_metrics()

        assert len(summary["succeeded"]) == 1
        assert metrics is not None
        assert metrics.precomputed_hash_hits == 1
        assert metrics.precomputed_hash_rejections == 0
        assert metrics.fallback_hashes == 0

    def test_size_mismatch_falls_back_to_file_hash(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        url = _image_url("fallback-hash")
        real_sha256 = image_store.sha256

        def fake_download(tasks, on_progress=None, **_kwargs):
            task = tasks[0]
            Image.new("RGB", (2, 2), color="white").save(
                task["save_path"],
                format="PNG",
            )
            payload = Path(task["save_path"]).read_bytes()
            result: DownloadFileResult = {
                "url": task["url"],
                "save_path": task["save_path"],
                "success": True,
                "content_sha256": sha256(payload).hexdigest(),
                "content_bytes": len(payload) + 1,
            }
            if on_progress is not None:
                on_progress(1, 1, result)
            return {"succeeded": [result], "failed": []}

        with (
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store.sha256",
                wraps=real_sha256,
            ) as fallback_hash,
            use_image_index_writer(),
            use_image_store_metrics(),
        ):
            summary = image_store.download_image_tasks([{"url": url}])
            metrics = image_store_metrics()

        assert len(summary["succeeded"]) == 1
        fallback_hash.assert_called_once()
        assert metrics is not None
        assert metrics.precomputed_hash_hits == 0
        assert metrics.precomputed_hash_rejections == 1
        assert metrics.fallback_hashes == 1


class ImageStoreRuntimeTest:
    def test_runtime_bounds_workers_and_queue(self) -> None:
        active = 0
        peak = 0
        lock = threading.Lock()
        saturated = threading.Event()
        release = threading.Event()

        def work(index: int) -> int:
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
                if active == 2:
                    saturated.set()
            assert release.wait(timeout=3)
            with lock:
                active -= 1
            return index

        with use_image_store_runtime(2) as runtime:
            futures = [
                runtime.submit(lambda index=index: work(index))
                for index in range(6)
            ]
            assert saturated.wait(timeout=2)
            metrics = image_store_runtime_metrics()
            assert metrics is not None
            assert metrics.worker_count == 2
            assert metrics.queue_capacity == 4
            assert metrics.peak_active_workers == 2
            release.set()
            assert [future.result(timeout=3) for future in futures] == list(
                range(6)
            )

        final_metrics = image_store_runtime_metrics()
        assert final_metrics is not None
        assert final_metrics.items_submitted == 6
        assert final_metrics.items_completed == 6
        assert final_metrics.active_workers == 0
        assert final_metrics.queued_items == 0
        assert peak == 2


class ImageMappingBatchTest:
    @pytest.mark.parametrize(
        ("task_count", "expected_batch_sizes"),
        [
            (65, [64, 1]),
            (129, [64, 64, 1]),
        ],
    )
    def test_successful_mappings_are_persisted_in_fixed_batches(
        self,
        tmp_path: Path,
        task_count: int,
        expected_batch_sizes: list[int],
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        payload_path = tmp_path / "payload.png"
        Image.new("RGB", (2, 2), color="white").save(payload_path)
        payload = payload_path.read_bytes()
        digest = sha256(payload).hexdigest()
        tasks = [
            {"url": _image_url(f"batch-{task_count}-{index}")}
            for index in range(task_count)
        ]
        progress: list[tuple[int, str]] = []
        progress_threads: list[int] = []
        caller_thread = threading.get_ident()

        def fake_download(download_tasks, on_progress=None, **_kwargs):
            results: list[DownloadFileResult] = []
            for current, download_task in enumerate(download_tasks, start=1):
                Path(download_task["save_path"]).write_bytes(payload)
                result: DownloadFileResult = {
                    "url": download_task["url"],
                    "save_path": download_task["save_path"],
                    "success": True,
                    "content_sha256": digest,
                    "content_bytes": len(payload),
                }
                results.append(result)
                if on_progress is not None:
                    on_progress(current, len(download_tasks), result)
            return {"succeeded": results, "failed": []}

        def record_progress(
            current: int,
            _total: int,
            result: DownloadFileResult,
        ) -> None:
            progress.append((current, result["url"]))
            progress_threads.append(threading.get_ident())

        real_enqueue = image_store._enqueue_image_mappings
        with (
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store._enqueue_image_mappings",
                wraps=real_enqueue,
            ) as enqueue_mock,
            use_image_index_writer(),
            use_image_store_metrics(),
            use_image_store_runtime(4),
            image_store.use_image_download_coordination(),
        ):
            summary = image_store.download_image_tasks(
                tasks,
                on_progress=record_progress,
            )
            metrics = image_store_metrics()
            writer_metrics = image_index_writer_metrics()
            mappings = image_index.ImageIndexStore(output_dir).mappings_for_urls(
                task["url"] for task in tasks
            )

        batch_sizes = [
            len(call.args[0])
            for call in enqueue_mock.call_args_list
        ]
        assert batch_sizes == expected_batch_sizes
        assert len(summary["succeeded"]) == task_count
        assert summary["failed"] == []
        assert progress == [
            (index, task["url"])
            for index, task in enumerate(tasks, start=1)
        ]
        assert progress_threads == [caller_thread] * task_count
        assert len(mappings) == task_count
        assert metrics is not None
        assert metrics.mapping_submissions == len(expected_batch_sizes)
        assert metrics.mapping_rows == task_count
        assert writer_metrics is not None
        assert writer_metrics.rows_written == task_count
        assert writer_metrics.transactions <= len(expected_batch_sizes)
    def test_download_failure_flushes_earlier_successes_in_completion_order(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        payload_path = tmp_path / "payload.png"
        Image.new("RGB", (2, 2), color="white").save(payload_path)
        payload = payload_path.read_bytes()
        digest = sha256(payload).hexdigest()
        tasks = [
            {"url": _image_url(f"ordered-result-{index}")}
            for index in range(3)
        ]
        progress_urls: list[str] = []

        def fake_download(download_tasks, on_progress=None, **_kwargs):
            results: list[DownloadFileResult] = []
            for current, download_task in enumerate(download_tasks, start=1):
                if current == 2:
                    result: DownloadFileResult = {
                        "url": download_task["url"],
                        "save_path": download_task["save_path"],
                        "success": False,
                        "error": "forced download failure",
                        "failure_kind": "connection",
                    }
                else:
                    Path(download_task["save_path"]).write_bytes(payload)
                    result = {
                        "url": download_task["url"],
                        "save_path": download_task["save_path"],
                        "success": True,
                        "content_sha256": digest,
                        "content_bytes": len(payload),
                    }
                results.append(result)
                if on_progress is not None:
                    on_progress(current, len(download_tasks), result)
            return {
                "succeeded": [result for result in results if result["success"]],
                "failed": [result for result in results if not result["success"]],
            }

        real_enqueue = image_store._enqueue_image_mappings
        with (
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
                side_effect=fake_download,
            ),
            patch(
                "nga_tools.backup.image_store._enqueue_image_mappings",
                wraps=real_enqueue,
            ) as enqueue_mock,
            use_image_index_writer(),
            image_store.use_image_download_coordination(),
        ):
            summary = image_store.download_image_tasks(
                tasks,
                on_progress=lambda _current, _total, result: (
                    progress_urls.append(result["url"])
                ),
            )

        assert progress_urls == [task["url"] for task in tasks]
        assert len(summary["succeeded"]) == 2
        assert len(summary["failed"]) == 1
        assert [
            len(call.args[0])
            for call in enqueue_mock.call_args_list
        ] == [1, 1]

    def test_transaction_failure_marks_the_whole_batch_and_allows_retry(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        payload_path = tmp_path / "payload.png"
        Image.new("RGB", (2, 2), color="white").save(payload_path)
        payload = payload_path.read_bytes()
        digest = sha256(payload).hexdigest()
        tasks = [
            {"url": _image_url(f"mapping-failure-{index}")}
            for index in range(3)
        ]

        def fake_download(download_tasks, on_progress=None, **_kwargs):
            results: list[DownloadFileResult] = []
            for current, download_task in enumerate(download_tasks, start=1):
                Path(download_task["save_path"]).write_bytes(payload)
                result: DownloadFileResult = {
                    "url": download_task["url"],
                    "save_path": download_task["save_path"],
                    "success": True,
                    "content_sha256": digest,
                    "content_bytes": len(payload),
                }
                results.append(result)
                if on_progress is not None:
                    on_progress(current, len(download_tasks), result)
            return {"succeeded": results, "failed": []}

        with patch("nga_tools.config.get_config", return_value=config):
            image_index.ImageIndexStore(output_dir).upsert_mapping(
                _image_url("mapping-failure-seed"),
                output_dir / "images_unique" / "seed.png",
            )
            with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_mapping_batch
                    BEFORE INSERT ON image_mappings
                    BEGIN
                        SELECT RAISE(FAIL, 'forced batch failure');
                    END
                    """
                )
                connection.commit()

            with (
                patch(
                    "nga_tools.backup.image_store.downloads.download_files_streaming",
                    side_effect=fake_download,
                ),
                use_image_index_writer(),
                use_image_store_metrics(),
                image_store.use_image_download_coordination(),
            ):
                failed_summary = image_store.download_image_tasks(tasks)
                failed_metrics = image_store_metrics()
                failed_mappings = image_index.ImageIndexStore(
                    output_dir
                ).mappings_for_urls(
                    task["url"] for task in tasks
                )

            with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
                connection.execute("DROP TRIGGER reject_mapping_batch")
                connection.commit()

            with (
                patch(
                    "nga_tools.backup.image_store.downloads.download_files_streaming",
                    side_effect=fake_download,
                ),
                use_image_index_writer(),
                use_image_store_metrics(),
                image_store.use_image_download_coordination(),
            ):
                retry_summary = image_store.download_image_tasks(tasks)
                retry_mappings = image_index.ImageIndexStore(
                    output_dir
                ).mappings_for_urls(
                    task["url"] for task in tasks
                )

        assert failed_summary["succeeded"] == []
        assert len(failed_summary["failed"]) == len(tasks)
        assert all(
            result["failure_kind"] == "image_store"
            for result in failed_summary["failed"]
        )
        assert failed_metrics is not None
        assert failed_metrics.mapping_submissions == 1
        assert failed_metrics.mapping_rows == len(tasks)
        assert failed_metrics.mapping_failures == 1
        assert failed_mappings == {}
        assert len(retry_summary["succeeded"]) == len(tasks)
        assert retry_summary["failed"] == []
        assert len(retry_mappings) == len(tasks)

    def test_cancelled_download_flushes_tail_without_reporting_success(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        config = SimpleNamespace(output_dir=str(output_dir))
        payload_path = tmp_path / "payload.png"
        Image.new("RGB", (2, 2), color="white").save(payload_path)
        payload = payload_path.read_bytes()
        digest = sha256(payload).hexdigest()
        tasks = [
            {"url": _image_url(f"cancelled-tail-{index}")}
            for index in range(4)
        ]
        progress: list[DownloadFileResult] = []

        def cancelled_download(download_tasks, on_progress=None, **_kwargs):
            for current, download_task in enumerate(download_tasks[:3], start=1):
                Path(download_task["save_path"]).write_bytes(payload)
                result: DownloadFileResult = {
                    "url": download_task["url"],
                    "save_path": download_task["save_path"],
                    "success": True,
                    "content_sha256": digest,
                    "content_bytes": len(payload),
                }
                if on_progress is not None:
                    on_progress(current, len(download_tasks), result)
            raise asyncio.CancelledError

        with (
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
                side_effect=cancelled_download,
            ),
            use_image_index_writer(),
            use_image_store_metrics(),
            image_store.use_image_download_coordination(),
        ):
            with pytest.raises(asyncio.CancelledError):
                image_store.download_image_tasks(
                    tasks,
                    on_progress=lambda _current, _total, result: progress.append(
                        result
                    ),
                )
            metrics = image_store_metrics()
            persisted = image_index.ImageIndexStore(output_dir).mappings_for_urls(
                task["url"] for task in tasks
            )

        assert progress == []
        assert set(persisted) == {task["url"] for task in tasks[:3]}
        assert metrics is not None
        assert metrics.mapping_submissions == 1
        assert metrics.mapping_rows == 3


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
            patch("nga_tools.config.get_config", return_value=config),
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
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
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
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
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
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
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
            patch("nga_tools.config.get_config", return_value=config),
            patch(
                "nga_tools.backup.image_store.downloads.download_files_streaming",
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

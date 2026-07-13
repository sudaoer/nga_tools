from __future__ import annotations

import inspect
import tempfile
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nga_tools import utils
from nga_tools.network_limits import (
    configure_network_limits,
    get_api_concurrency,
    get_image_concurrency,
)
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.api_runtime import use_api_runtime
from nga_tools.core.image_download_runtime import image_download_runtime_metrics


class _ApiResponse:
    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {"code": 0, "totalPage": 1, "result": []}


class _ChunkedContent:
    async def iter_chunked(self, _chunk_size: int):
        yield b"image"


class _DownloadResponse:
    status = 200
    content = _ChunkedContent()

    async def __aenter__(self) -> "_DownloadResponse":
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        return

    async def read(self) -> bytes:
        return b"image"


class _FailedDownloadResponse(_DownloadResponse):
    status = 404
    request_info = MagicMock()
    history: tuple[object, ...] = ()
    headers = None


class _ClientSession:
    instance_count = 0

    def __init__(self, **kwargs: object) -> None:
        type(self).instance_count += 1
        self.connector = kwargs.get("connector")

    async def __aenter__(self) -> "_ClientSession":
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        close = getattr(self.connector, "close", None)
        if close is not None:
            close_result = close()
            if inspect.isawaitable(close_result):
                await close_result
        return

    def get(self, url: str) -> _DownloadResponse:
        del url
        return _DownloadResponse()


class _FailedClientSession(_ClientSession):
    def get(self, url: str) -> _FailedDownloadResponse:
        del url
        return _FailedDownloadResponse()


class NetworkLimitsTest:
    def test_configure_network_limits_sets_separate_limits(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=7)

        assert get_api_concurrency() == 2
        assert get_image_concurrency() == 7

    def test_cannot_reconfigure_api_limit_while_runtime_is_active(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=7)

        with use_api_runtime(2):
            try:
                configure_network_limits(api_concurrency=3, image_concurrency=7)
            except RuntimeError as error:
                assert "活动期间不能修改API并发数" in str(error)
            else:
                raise AssertionError("expected active runtime reconfiguration failure")

    def test_nga_client_uses_api_request_slot(self) -> None:
        config = SimpleNamespace(
            base_url="https://bbs.nga.cn",
            user_agent="test-agent",
            nga_passport_uid="uid",
            nga_passport_cid="cid",
        )
        api_slot = MagicMock()

        with (
            patch("nga_tools.ngaclient.client.get_config", return_value=config),
            patch(
                "nga_tools.ngaclient.client.api_request_slot",
                return_value=api_slot,
            ) as api_slot_factory,
        ):
            client = NGAClient()
            client.session.post = MagicMock(return_value=_ApiResponse())

            client.get_page(123, None, 1)

        api_slot_factory.assert_called_once_with()
        api_slot.__enter__.assert_called_once_with()
        api_slot.__exit__.assert_called_once()

    def test_download_files_reuses_one_command_runtime_session(self) -> None:
        _ClientSession.instance_count = 0
        configure_network_limits(api_concurrency=4, image_concurrency=1)

        with tempfile.TemporaryDirectory() as temp_dir_name:
            target_path = Path(temp_dir_name) / "image"
            with patch(
                "nga_tools.core.image_download_runtime.aiohttp.ClientSession",
                _ClientSession,
            ):
                result = utils.download_files(
                    [
                        {
                            "url": "https://example.com/image.png",
                            "save_path": str(target_path),
                        }
                    ]
                )

        assert len(result['succeeded']) == 1
        assert result["succeeded"][0]["content_sha256"] == sha256(
            b"image"
        ).hexdigest()
        assert result["succeeded"][0]["content_bytes"] == len(b"image")
        assert _ClientSession.instance_count == 1
        metrics = image_download_runtime_metrics()
        assert metrics is not None
        assert metrics.downloaded_bytes == len(b"image")
        assert metrics.in_flight_requests == 0
        assert metrics.peak_in_flight_requests == 1
        assert metrics.request_to_headers_seconds >= 0
        assert metrics.response_body_read_seconds >= 0
        assert metrics.temp_file_write_seconds >= 0
        assert metrics.atomic_replace_seconds >= 0

    def test_default_download_concurrency_uses_configured_image_limit(self) -> None:
        configure_network_limits(api_concurrency=4, image_concurrency=100)

        assert utils._effective_download_concurrency(None) == 100
        assert utils._effective_download_concurrency(50) == 50

    def test_final_download_failure_is_structured_without_bottom_layer_warning(
        self,
        tmp_path: Path,
    ) -> None:
        with (
            patch(
                "nga_tools.core.image_download_runtime.aiohttp.ClientSession",
                _FailedClientSession,
            ),
            patch(
                "nga_tools.core.image_download_runtime.report_warning"
            ) as warning_mock,
        ):
            result = utils.download_files(
                [
                    {
                        "url": "https://example.com/missing.png",
                        "save_path": str(tmp_path / "missing.png"),
                    }
                ],
                retries=0,
            )

        warning_mock.assert_not_called()
        assert result["succeeded"] == []
        assert result["failed"][0]["failure_kind"] == "http_4xx"
        assert result["failed"][0]["http_status"] == 404
        assert "content_sha256" not in result["failed"][0]
        assert "content_bytes" not in result["failed"][0]

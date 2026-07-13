from __future__ import annotations

import tempfile
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


class _ApiResponse:
    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {"code": 0, "totalPage": 1, "result": []}


class _AsyncSlot:
    entered_count = 0

    async def __aenter__(self) -> None:
        type(self).entered_count += 1

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        return


class _DownloadResponse:
    status = 200

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
    def __init__(self, **kwargs: object) -> None:
        del kwargs

    async def __aenter__(self) -> "_ClientSession":
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
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

    def test_download_files_uses_image_download_slot(self) -> None:
        _AsyncSlot.entered_count = 0
        configure_network_limits(api_concurrency=4, image_concurrency=1)

        with tempfile.TemporaryDirectory() as temp_dir_name:
            target_path = Path(temp_dir_name) / "image"
            with (
                patch("nga_tools.core.downloads.aiohttp.ClientSession", _ClientSession),
                patch(
                    "nga_tools.core.downloads.network_limits.image_download_slot",
                    side_effect=_AsyncSlot,
                ),
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
        assert _AsyncSlot.entered_count == 1

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
                "nga_tools.core.downloads.aiohttp.ClientSession",
                _FailedClientSession,
            ),
            patch("nga_tools.core.downloads.report_warning") as warning_mock,
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

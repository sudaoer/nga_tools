from __future__ import annotations

import tempfile
import unittest
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


class NetworkLimitsTest(unittest.TestCase):
    def test_configure_network_limits_sets_separate_limits(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=7)

        self.assertEqual(get_api_concurrency(), 2)
        self.assertEqual(get_image_concurrency(), 7)

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
                patch("nga_tools.utils.aiohttp.ClientSession", _ClientSession),
                patch(
                    "nga_tools.utils.network_limits.image_download_slot",
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

        self.assertEqual(len(result["succeeded"]), 1)
        self.assertEqual(_AsyncSlot.entered_count, 1)

    def test_default_download_concurrency_uses_configured_image_limit(self) -> None:
        configure_network_limits(api_concurrency=4, image_concurrency=100)

        self.assertEqual(utils._effective_download_concurrency(None), 100)
        self.assertEqual(utils._effective_download_concurrency(50), 50)


if __name__ == "__main__":
    unittest.main()

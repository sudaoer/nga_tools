from __future__ import annotations

from nga_tools.backup.image_index_writer import image_index_writer_metrics
from nga_tools.backup.image_store_metrics import image_store_metrics
from nga_tools.backup.image_store_runtime import image_store_runtime_metrics
from nga_tools.core.image_download_runtime import (
    audio_download_runtime_metrics,
    image_download_runtime_metrics,
)
from nga_tools.ngaclient.api_runtime import api_runtime_metrics


def client_runtime_metrics() -> dict[str, object]:
    api_metrics = api_runtime_metrics()
    image_metrics = image_download_runtime_metrics()
    audio_metrics = audio_download_runtime_metrics()
    writer_metrics = image_index_writer_metrics()
    store_metrics = image_store_metrics()
    store_runtime_metrics = image_store_runtime_metrics()
    return {
        "api": None if api_metrics is None else api_metrics.as_dict(),
        "image": None if image_metrics is None else image_metrics.as_dict(),
        "audio": None if audio_metrics is None else audio_metrics.as_dict(),
        "image_store": (
            None if store_metrics is None else store_metrics.as_dict()
        ),
        "image_store_runtime": (
            None
            if store_runtime_metrics is None
            else store_runtime_metrics.as_dict()
        ),
        "image_index_writer": (
            None if writer_metrics is None else writer_metrics.as_dict()
        ),
    }

from __future__ import annotations

from nga_tools.backup.image_index_writer import image_index_writer_metrics
from nga_tools.core.image_download_runtime import image_download_runtime_metrics
from nga_tools.ngaclient.api_runtime import api_runtime_metrics


def client_runtime_metrics() -> dict[str, object]:
    api_metrics = api_runtime_metrics()
    image_metrics = image_download_runtime_metrics()
    writer_metrics = image_index_writer_metrics()
    return {
        "api": None if api_metrics is None else api_metrics.as_dict(),
        "image": None if image_metrics is None else image_metrics.as_dict(),
        "image_index_writer": (
            None if writer_metrics is None else writer_metrics.as_dict()
        ),
    }

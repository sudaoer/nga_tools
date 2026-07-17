from __future__ import annotations

from collections.abc import Callable
from typing import Literal, NotRequired, TypedDict

DownloadResourceKind = Literal["image", "audio"]


class DownloadTask(TypedDict):
    url: str
    request_url: NotRequired[str]
    save_path: str


DownloadFailureKind = Literal[
    "http_3xx",
    "http_4xx",
    "http_5xx",
    "timeout",
    "connection",
    "payload",
    "unexpected_download",
    "image_store",
    "audio_store",
    "audio_validation",
]
DOWNLOAD_FAILURE_KINDS: frozenset[DownloadFailureKind] = frozenset(
    {
        "http_3xx",
        "http_4xx",
        "http_5xx",
        "timeout",
        "connection",
        "payload",
        "unexpected_download",
        "image_store",
        "audio_store",
        "audio_validation",
    }
)


class DownloadFileResult(TypedDict):
    url: str
    save_path: str
    success: bool
    error: NotRequired[str]
    failure_kind: NotRequired[DownloadFailureKind]
    http_status: NotRequired[int]
    content_sha256: NotRequired[str]
    content_bytes: NotRequired[int]


class DownloadSummary(TypedDict):
    succeeded: list[DownloadFileResult]
    failed: list[DownloadFileResult]


DownloadProgressCallback = Callable[[int, int, DownloadFileResult], None]

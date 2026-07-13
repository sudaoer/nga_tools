from __future__ import annotations

import sys
from typing import NoReturn

from nga_tools.bbcode_convert import strip_bbcode_tags
from nga_tools.config import get_config as get_config
from nga_tools.console import report_info
from nga_tools.core.downloads import (
    DownloadFileResult as DownloadFileResult,
    DownloadProgressCallback as DownloadProgressCallback,
    DownloadSummary as DownloadSummary,
    DownloadTask as DownloadTask,
    download_files as download_files,
    download_files_streaming as download_files_streaming,
    effective_download_concurrency,
)
from nga_tools.core.hashing import sha256 as sha256
from nga_tools.core.nga_images import NGA_img_link_verify as NGA_img_link_verify
from nga_tools.core.paths import (
    TIMING_LOG_FILENAME as TIMING_LOG_FILENAME,
    WARNING_LOG_FILENAME as WARNING_LOG_FILENAME,
    get_folder as get_folder,
    list_files_in_folder as list_files_in_folder,
    timing_log_path as timing_log_path,
    warning_log_path as warning_log_path,
)


def delete_bbcode_tags(text: str) -> str:
    return strip_bbcode_tags(text)


def TODO(message: str) -> NoReturn:
    report_info(f"TODO: {message}")
    sys.exit(1)


def _effective_download_concurrency(  # pyright: ignore[reportUnusedFunction]
    max_concurrency: int | None,
) -> int:
    return effective_download_concurrency(max_concurrency)


if __name__ == "__main__":
    sample_text = "[b]Bold Text[/b] and [url=http://example.com]Example Link[/url]"
    cleaned_text = delete_bbcode_tags(sample_text)
    report_info(f"Original Text: {sample_text}")
    report_info(f"Cleaned Text: {cleaned_text}")
    report_info(f"Word Count: {len(cleaned_text.split())}")

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from nga_tools import config
from nga_tools.core import paths


@pytest.fixture(autouse=True)
def isolate_default_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    output_dir = tmp_path / "output"
    app_config = config.AppConfig(
        base_url="https://bbs.nga.cn",
        user_agent="test-agent",
        output_dir=str(output_dir),
        thread_config_file=str(tmp_path / "thread_configs.json"),
        pdf_page_size="A4",
        pdf_page_margin="12mm",
        pdf_long_image_min_width=800,
        pdf_long_image_min_ratio=4.0,
        pdf_long_image_slice_ratio=1.35,
        pdf_speaker_portrait_max_dimension=640,
        pdf_speaker_portrait_max_ratio=3.0,
        pdf_speaker_portrait_size="14mm",
        html_pre="<div>",
        html_post="</div>",
        html_font_family="sans-serif",
        nga_passport_uid="uid",
        nga_passport_cid="cid",
        api_concurrency=config.DEFAULT_API_CONCURRENCY,
        image_concurrency=config.DEFAULT_IMAGE_CONCURRENCY,
        audio_concurrency=config.DEFAULT_AUDIO_CONCURRENCY,
        backup_configs_workers=config.DEFAULT_BACKUP_CONFIGS_WORKERS,
        backup_sqlite_concurrency=config.DEFAULT_BACKUP_SQLITE_CONCURRENCY,
        timing_log_enabled=config.DEFAULT_TIMING_LOG_ENABLED,
        ankebak_full_backup_interval_hours=(
            config.DEFAULT_ANKEBAK_FULL_BACKUP_INTERVAL_HOURS
        ),
        ankebak_missing_floor_immediate_retry_hours=(
            config.DEFAULT_ANKEBAK_MISSING_FLOOR_IMMEDIATE_RETRY_HOURS
        ),
        ankebak_missing_floor_retry_max_interval_hours=(
            config.DEFAULT_ANKEBAK_MISSING_FLOOR_RETRY_MAX_INTERVAL_HOURS
        ),
        backup_image_retry_max_interval_hours=(
            config.DEFAULT_BACKUP_IMAGE_RETRY_MAX_INTERVAL_HOURS
        ),
        backup_audio_retry_max_interval_hours=(
            config.DEFAULT_BACKUP_AUDIO_RETRY_MAX_INTERVAL_HOURS
        ),
    )

    paths._CREATED_FOLDERS.clear()
    monkeypatch.setattr(config, "load_config", lambda *args, **kwargs: app_config)
    try:
        yield
    finally:
        paths._CREATED_FOLDERS.clear()

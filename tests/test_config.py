from __future__ import annotations

import pytest
import json
import tempfile
from pathlib import Path

from nga_tools.config import (
    load_config,
    load_timing_log_enabled,
)


def _config_data(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "base_url": "https://bbs.nga.cn",
        "user_agent": "test-agent",
        "output_dir": "output",
        "thread_config_file": "thread_configs.json",
        "pdf_page_size": "A4",
        "pdf_page_margin": "12mm",
        "pdf_long_image_min_width": 800,
        "pdf_long_image_min_ratio": 4.0,
        "pdf_long_image_slice_ratio": 1.35,
        "pdf_speaker_portrait_max_dimension": 640,
        "pdf_speaker_portrait_max_ratio": 3.0,
        "pdf_speaker_portrait_size": "14mm",
        "html_pre": "<div>",
        "html_post": "</div>",
        "html_font_family": "sans-serif",
    }
    data.update(overrides)
    return data


def _secrets_data() -> dict[str, object]:
    return {
        "nga_passport_uid": "uid",
        "nga_passport_cid": "cid",
    }


class ConfigConcurrencyTest:
    def _write_config_files(
        self,
        temp_dir: Path,
        config_overrides: dict[str, object] | None = None,
    ) -> tuple[Path, Path]:
        config_path = temp_dir / "config.json"
        secrets_path = temp_dir / "secrets.json"
        config_path.write_text(
            json.dumps(_config_data(**(config_overrides or {}))),
            encoding="utf-8",
        )
        secrets_path.write_text(json.dumps(_secrets_data()), encoding="utf-8")
        return config_path, secrets_path

    @pytest.mark.parametrize(
        "config_overrides",
        [
            {"api_concurrency": 0},
            {"image_concurrency": 0},
            {"audio_concurrency": 0},
            {"backup_configs_workers": 0},
            {"timing_log_enabled": "yes"},
            {"ankebak_full_backup_interval_hours": 0},
            {"ankebak_missing_floor_immediate_retry_hours": 0},
            {"ankebak_missing_floor_retry_max_interval_hours": 0},
            {"backup_image_retry_max_interval_hours": 0},
            {"backup_audio_retry_max_interval_hours": 0},
        ],
    )
    def test_load_config_rejects_invalid_optional_values(
        self,
        config_overrides: dict[str, object],
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            config_path, secrets_path = self._write_config_files(
                Path(temp_dir_name),
                config_overrides,
            )

            with pytest.raises(ValueError):
                load_config(config_path, secrets_path)

    def test_load_timing_log_enabled_reads_config_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            config_path = Path(temp_dir_name) / "config.json"
            config_path.write_text(
                json.dumps(_config_data(timing_log_enabled=False)),
                encoding="utf-8",
            )

            assert load_timing_log_enabled(config_path) is False

    def test_load_timing_log_enabled_defaults_to_enabled_without_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            missing_path = Path(temp_dir_name) / "missing.json"

            assert load_timing_log_enabled(missing_path) is True

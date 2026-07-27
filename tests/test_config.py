from __future__ import annotations

import pytest
import json
from pathlib import Path

from nga_tools.config import (
    load_config,
    load_timing_log_enabled,
    load_timing_log_retention_days,
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
            {"backup_sqlite_concurrency": 0},
            {"timing_log_enabled": "yes"},
            {"timing_log_retention_days": 0},
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
        tmp_path: Path,
    ) -> None:
        config_path, secrets_path = self._write_config_files(
            tmp_path,
            config_overrides,
        )

        with pytest.raises(ValueError):
            load_config(config_path, secrets_path)

    def test_load_timing_log_enabled_reads_config_without_secrets(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(_config_data(timing_log_enabled=False)),
            encoding="utf-8",
        )

        assert load_timing_log_enabled(config_path) is False

    def test_load_timing_log_enabled_defaults_to_enabled_without_config(
        self,
        tmp_path: Path,
    ) -> None:
        missing_path = tmp_path / "missing.json"

        assert load_timing_log_enabled(missing_path) is True

    def test_load_timing_log_retention_days_reads_config(
        self,
        tmp_path: Path,
    ) -> None:
        config_path = tmp_path / "config.json"
        config_path.write_text(
            json.dumps(_config_data(timing_log_retention_days=14)),
            encoding="utf-8",
        )

        assert load_timing_log_retention_days(config_path) == 14

    def test_load_timing_log_retention_days_defaults_without_config(
        self,
        tmp_path: Path,
    ) -> None:
        missing_path = tmp_path / "missing.json"

        assert load_timing_log_retention_days(missing_path) == 7

    def test_load_config_defaults_timing_log_retention_to_seven_days(
        self,
        tmp_path: Path,
    ) -> None:
        config_path, secrets_path = self._write_config_files(tmp_path)

        loaded = load_config(config_path, secrets_path)

        assert loaded.timing_log_retention_days == 7

    def test_load_config_reads_timing_log_retention_days(
        self,
        tmp_path: Path,
    ) -> None:
        config_path, secrets_path = self._write_config_files(
            tmp_path,
            {"timing_log_retention_days": 14},
        )

        loaded = load_config(config_path, secrets_path)

        assert loaded.timing_log_retention_days == 14

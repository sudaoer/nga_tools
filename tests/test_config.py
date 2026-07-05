from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nga_tools.config import (
    DEFAULT_API_CONCURRENCY,
    DEFAULT_BACKUP_CONFIGS_WORKERS,
    DEFAULT_IMAGE_CONCURRENCY,
    load_config,
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


class ConfigConcurrencyTest(unittest.TestCase):
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

    def test_load_config_uses_default_concurrency_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            config_path, secrets_path = self._write_config_files(Path(temp_dir_name))

            app_config = load_config(config_path, secrets_path)

        self.assertEqual(app_config.api_concurrency, DEFAULT_API_CONCURRENCY)
        self.assertEqual(app_config.image_concurrency, DEFAULT_IMAGE_CONCURRENCY)
        self.assertEqual(
            app_config.backup_configs_workers,
            DEFAULT_BACKUP_CONFIGS_WORKERS,
        )

    def test_load_config_accepts_custom_concurrency_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            config_path, secrets_path = self._write_config_files(
                Path(temp_dir_name),
                {
                    "api_concurrency": 2,
                    "image_concurrency": 20,
                    "backup_configs_workers": 3,
                },
            )

            app_config = load_config(config_path, secrets_path)

        self.assertEqual(app_config.api_concurrency, 2)
        self.assertEqual(app_config.image_concurrency, 20)
        self.assertEqual(app_config.backup_configs_workers, 3)

    def test_load_config_rejects_non_positive_concurrency_values(self) -> None:
        invalid_configs = [
            {"api_concurrency": 0},
            {"image_concurrency": 0},
            {"backup_configs_workers": 0},
        ]

        for config_overrides in invalid_configs:
            with self.subTest(config_overrides=config_overrides):
                with tempfile.TemporaryDirectory() as temp_dir_name:
                    config_path, secrets_path = self._write_config_files(
                        Path(temp_dir_name),
                        config_overrides,
                    )

                    with self.assertRaises(ValueError):
                        load_config(config_path, secrets_path)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from nga_tools import config
from nga_tools.core import paths


_BASE_TEST_APP_CONFIG = config.load_config(
    config.PROJECT_ROOT / "config.example.json",
    config.PROJECT_ROOT / "secrets.example.json",
)


def _test_app_config(tmp_path: Path) -> config.AppConfig:
    return replace(
        _BASE_TEST_APP_CONFIG,
        user_agent="test-agent",
        output_dir=str(tmp_path / "output"),
        thread_config_file=str(tmp_path / "thread_configs.json"),
        html_pre="<div>",
        html_font_family="sans-serif",
        nga_passport_uid="uid",
        nga_passport_cid="cid",
    )


@pytest.fixture(autouse=True)
def isolate_default_output_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    app_config = _test_app_config(tmp_path)

    paths._CREATED_FOLDERS.clear()
    monkeypatch.setattr(config, "load_config", lambda *args, **kwargs: app_config)
    try:
        yield
    finally:
        paths._CREATED_FOLDERS.clear()

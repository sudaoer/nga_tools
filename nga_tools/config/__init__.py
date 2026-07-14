from __future__ import annotations

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, TypeAlias, cast

JsonObject: TypeAlias = dict[str, object]
PathValue: TypeAlias = str | Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"
DEFAULT_SECRETS_PATH = PROJECT_ROOT / "secrets.json"
DEFAULT_API_CONCURRENCY = 4
DEFAULT_IMAGE_CONCURRENCY = 50
DEFAULT_BACKUP_CONFIGS_WORKERS = 4
DEFAULT_TIMING_LOG_ENABLED = True
DEFAULT_ANKEBAK_FULL_BACKUP_INTERVAL_HOURS = 168
DEFAULT_BACKUP_IMAGE_RETRY_MAX_INTERVAL_HOURS = 168


@dataclass(frozen=True)
class AppConfig:
    base_url: str
    user_agent: str
    output_dir: str
    thread_config_file: str
    pdf_page_size: str
    pdf_page_margin: str
    pdf_long_image_min_width: int
    pdf_long_image_min_ratio: float
    pdf_long_image_slice_ratio: float
    pdf_speaker_portrait_max_dimension: int
    pdf_speaker_portrait_max_ratio: float
    pdf_speaker_portrait_size: str
    html_pre: str
    html_post: str
    html_font_family: str
    nga_passport_uid: str
    nga_passport_cid: str
    api_concurrency: int
    image_concurrency: int
    backup_configs_workers: int
    timing_log_enabled: bool
    ankebak_full_backup_interval_hours: int
    backup_image_retry_max_interval_hours: int

    @property
    def html_style(self) -> str:
        return f"""
<style>
@page {{
    size: {self.pdf_page_size};
    margin: {self.pdf_page_margin};
}}

.bbcode_container, html, body {{
  background-color: #ffffff;
}}

body{{
  color: #111111;
  font-size: 10.5pt;
  line-height: 1.55;
  margin: 0;
  padding: 0;
}}

.bbcode_container {{
  width: 100%;
  max-width: 100%;
  margin: 0;
}}
.bbcode_container img {{
  display: block;
  max-width: 100%;
  height: auto;
  margin: 0.35em 0;
  break-inside: avoid;
  page-break-inside: avoid;
}}
.bbcode_container .speaker-portrait {{
  display: inline-block;
  max-width: {self.pdf_speaker_portrait_size};
  max-height: {self.pdf_speaker_portrait_size};
  width: auto;
  height: auto;
  vertical-align: middle;
  margin: 0 0.35em 0.1em 0;
}}
.long-image-slices {{
  display: block;
  margin: 0.45em 0;
}}
.long-image-slice {{
  display: block;
  width: 100%;
  max-width: 100%;
  height: auto;
  margin: 0 0 4mm 0;
  break-inside: avoid;
  page-break-inside: avoid;
}}
.long-image-slice:last-child {{
  margin-bottom: 0;
}}
blockquote {{
  background: #ffffff;
  border: 1px solid #d6d6d6;
  border-left: 3px solid #bdbdbd;
  margin: 0.55em 0;
  padding: 0.45em 0.8em;
}}
blockquote.nga-quote {{
  background: #f8f8f8;
}}
.nga-collapse {{
  border-top: 1px solid #d6d6d6;
  border-bottom: 1px solid #d6d6d6;
  margin: 0.55em 0;
  padding: 0.2em 0;
}}
.nga-collapse summary {{
  font-weight: bold;
}}
.nga-collapse-content {{
  border-top: 1px solid #e5e5e5;
  margin-top: 0.25em;
  padding-top: 0.35em;
}}
.nga-bbcode-heading {{
  display: inline;
  font-size: 1.08em;
  margin: 0;
}}
.skyblue   {{ color: skyblue; }}
.royalblue {{ color: royalblue; }}
.blue      {{ color: blue; }}
.darkblue  {{ color: darkblue; }}

.orange    {{ color: orange; }}
.orangered {{ color: orangered; }}
.crimson   {{ color: crimson; }}
.red       {{ color: red; }}
.firebrick {{ color: firebrick; }}
.darkred   {{ color: darkred; }}

.green     {{ color: green; }}
.limegreen {{ color: limegreen; }}
.seagreen  {{ color: seagreen; }}
.teal      {{ color: teal; }}

.deeppink  {{ color: deeppink; }}
.tomato    {{ color: tomato; }}
.coral     {{ color: coral; }}

.purple    {{ color: purple; }}
.indigo    {{ color: indigo; }}

.burlywood  {{ color: burlywood; }}
.sandybrown {{ color: sandybrown; }}
.sienna     {{ color: sienna; }}
.chocolate  {{ color: chocolate; }}

.silver    {{ color: silver; }}

em, i {{ font-style: italic; }}
em *, i * {{ font-style: inherit; }}
h2 {{ margin: 0 0 0.5em 0; page-break-after: avoid; }}
hr {{ border: none; border-top: 1px solid #d6d6d6; margin: 0.9em 0; }}

body, textarea, select, input, button {{font-family:{self.html_font_family}}}


</style>
"""


def _read_json_object(path: Path) -> JsonObject:
    try:
        raw_data: object = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise FileNotFoundError(f"配置文件不存在：{path}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"配置文件不是有效JSON：{path}") from error

    if not isinstance(raw_data, dict):
        raise ValueError(f"配置文件顶层必须是JSON对象：{path}")

    data = cast(dict[object, object], raw_data)
    if not all(isinstance(key, str) for key in data):
        raise ValueError(f"配置文件的键必须都是字符串：{path}")

    return cast(JsonObject, data)


def _required_str(data: JsonObject, key: str, source: Path) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value
    raise ValueError(f"{source} 缺少字符串配置项：{key}")


def _required_int(data: JsonObject, key: str, source: Path) -> int:
    value = data.get(key)
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise ValueError(f"{source} 缺少整数配置项：{key}")


def _required_float(data: JsonObject, key: str, source: Path) -> float:
    value = data.get(key)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    raise ValueError(f"{source} 缺少数字配置项：{key}")


def _optional_positive_int(
    data: JsonObject,
    key: str,
    source: Path,
    default: int,
) -> int:
    value = data.get(key, default)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    raise ValueError(f"{source} 配置项必须是大于0的整数：{key}")


def _optional_bool(
    data: JsonObject,
    key: str,
    source: Path,
    default: bool,
) -> bool:
    value = data.get(key, default)
    if type(value) is bool:
        return value
    raise ValueError(f"{source} 配置项必须是布尔值：{key}")


def _path_or_default(path: Optional[PathValue], default_path: Path) -> Path:
    if path is None:
        return default_path
    return Path(path)


def load_config(
    config_path: Optional[PathValue] = None,
    secrets_path: Optional[PathValue] = None,
) -> AppConfig:
    resolved_config_path = _path_or_default(config_path, DEFAULT_CONFIG_PATH)
    resolved_secrets_path = _path_or_default(secrets_path, DEFAULT_SECRETS_PATH)
    config_data = _read_json_object(resolved_config_path)
    secrets_data = _read_json_object(resolved_secrets_path)

    return AppConfig(
        base_url=_required_str(config_data, "base_url", resolved_config_path),
        user_agent=_required_str(config_data, "user_agent", resolved_config_path),
        output_dir=_required_str(config_data, "output_dir", resolved_config_path),
        thread_config_file=_required_str(
            config_data, "thread_config_file", resolved_config_path
        ),
        pdf_page_size=_required_str(config_data, "pdf_page_size", resolved_config_path),
        pdf_page_margin=_required_str(
            config_data, "pdf_page_margin", resolved_config_path
        ),
        pdf_long_image_min_width=_required_int(
            config_data, "pdf_long_image_min_width", resolved_config_path
        ),
        pdf_long_image_min_ratio=_required_float(
            config_data, "pdf_long_image_min_ratio", resolved_config_path
        ),
        pdf_long_image_slice_ratio=_required_float(
            config_data, "pdf_long_image_slice_ratio", resolved_config_path
        ),
        pdf_speaker_portrait_max_dimension=_required_int(
            config_data, "pdf_speaker_portrait_max_dimension", resolved_config_path
        ),
        pdf_speaker_portrait_max_ratio=_required_float(
            config_data, "pdf_speaker_portrait_max_ratio", resolved_config_path
        ),
        pdf_speaker_portrait_size=_required_str(
            config_data, "pdf_speaker_portrait_size", resolved_config_path
        ),
        html_pre=_required_str(config_data, "html_pre", resolved_config_path),
        html_post=_required_str(config_data, "html_post", resolved_config_path),
        html_font_family=_required_str(
            config_data, "html_font_family", resolved_config_path
        ),
        nga_passport_uid=_required_str(
            secrets_data, "nga_passport_uid", resolved_secrets_path
        ),
        nga_passport_cid=_required_str(
            secrets_data, "nga_passport_cid", resolved_secrets_path
        ),
        api_concurrency=_optional_positive_int(
            config_data,
            "api_concurrency",
            resolved_config_path,
            DEFAULT_API_CONCURRENCY,
        ),
        image_concurrency=_optional_positive_int(
            config_data,
            "image_concurrency",
            resolved_config_path,
            DEFAULT_IMAGE_CONCURRENCY,
        ),
        backup_configs_workers=_optional_positive_int(
            config_data,
            "backup_configs_workers",
            resolved_config_path,
            DEFAULT_BACKUP_CONFIGS_WORKERS,
        ),
        timing_log_enabled=_optional_bool(
            config_data,
            "timing_log_enabled",
            resolved_config_path,
            DEFAULT_TIMING_LOG_ENABLED,
        ),
        ankebak_full_backup_interval_hours=_optional_positive_int(
            config_data,
            "ankebak_full_backup_interval_hours",
            resolved_config_path,
            DEFAULT_ANKEBAK_FULL_BACKUP_INTERVAL_HOURS,
        ),
        backup_image_retry_max_interval_hours=_optional_positive_int(
            config_data,
            "backup_image_retry_max_interval_hours",
            resolved_config_path,
            DEFAULT_BACKUP_IMAGE_RETRY_MAX_INTERVAL_HOURS,
        ),
    )


def load_timing_log_enabled(config_path: Optional[PathValue] = None) -> bool:
    resolved_config_path = _path_or_default(config_path, DEFAULT_CONFIG_PATH)
    try:
        config_data = _read_json_object(resolved_config_path)
    except FileNotFoundError:
        return DEFAULT_TIMING_LOG_ENABLED
    return _optional_bool(
        config_data,
        "timing_log_enabled",
        resolved_config_path,
        DEFAULT_TIMING_LOG_ENABLED,
    )


_CONFIG_OVERRIDE_LOCK = threading.RLock()
_config_override: AppConfig | None = None


@lru_cache(maxsize=1)
def _get_default_config() -> AppConfig:
    return load_config()


def get_config() -> AppConfig:
    with _CONFIG_OVERRIDE_LOCK:
        override = _config_override
    return _get_default_config() if override is None else override


@contextmanager
def use_config_override(config: AppConfig) -> Generator[None]:
    """Install a process-wide temporary config before worker threads are created."""

    global _config_override
    with _CONFIG_OVERRIDE_LOCK:
        previous = _config_override
        if previous is not None:
            raise RuntimeError("运行时配置覆盖已经启用。")
        _config_override = config
    try:
        yield
    finally:
        with _CONFIG_OVERRIDE_LOCK:
            _config_override = previous

from __future__ import annotations

import pytest
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nga_tools.forum.thread_configs import NGAThreadConfigs


def _app_config(config_path: Path) -> SimpleNamespace:
    return SimpleNamespace(thread_config_file=str(config_path))


class ThreadConfigsTest:
    def test_loads_required_fields_and_arbitrary_scalar_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "thread_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ThreadList": [
                            {
                                "thread_name": "name",
                                "tid": 101,
                                "aid": 201,
                                "link": "https://bbs.nga.cn/read.php?tid=101",
                                "replies": 600,
                                "score": 1.5,
                                "active": True,
                                "description": None,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "nga_tools.forum.thread_configs.get_config",
                return_value=_app_config(config_path),
            ):
                configs = NGAThreadConfigs().get_thread_configs()

        assert configs[0]['thread_name'] == 'name'
        assert configs[0]['tid'] == 101
        assert configs[0]['description'] == None

    @pytest.mark.parametrize("value", [["tag"], {"nested": True}])
    def test_rejects_nested_thread_config_values(self, value: object) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "thread_configs.json"
            config_path.write_text(
                json.dumps(
                    {
                        "ThreadList": [
                            {
                                "thread_name": "name",
                                "tid": 101,
                                "extra": value,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "nga_tools.forum.thread_configs.get_config",
                    return_value=_app_config(config_path),
                ),
                pytest.raises(ValueError, match='不能是数组或对象'),
            ):
                NGAThreadConfigs()

    @pytest.mark.parametrize(
        "item",
        [
            {"tid": 101},
            {"thread_name": "name"},
            {"thread_name": "name", "tid": True},
            {"thread_name": "name", "tid": 101, "aid": "201"},
        ],
    )
    def test_rejects_missing_required_thread_fields(
        self,
        item: dict[str, object],
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "thread_configs.json"
            config_path.write_text(
                json.dumps({"ThreadList": [item]}),
                encoding="utf-8",
            )

            with (
                patch(
                    "nga_tools.forum.thread_configs.get_config",
                    return_value=_app_config(config_path),
                ),
                pytest.raises(ValueError),
            ):
                NGAThreadConfigs()

    def test_add_thread_omits_optional_empty_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "thread_configs.json"
            with patch(
                "nga_tools.forum.thread_configs.get_config",
                return_value=_app_config(config_path),
            ):
                configs = NGAThreadConfigs()
                configs.add_thread("name", 101)
                configs.save_configs()

            data = json.loads(config_path.read_text(encoding="utf-8"))

        assert data['ThreadList'] == [{'thread_name': 'name', 'tid': 101}]

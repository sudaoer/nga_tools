from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nga_tools.thread_configs import NGAThreadConfigs


def _app_config(config_path: Path) -> SimpleNamespace:
    return SimpleNamespace(thread_config_file=str(config_path))


class ThreadConfigsTest(unittest.TestCase):
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
                "nga_tools.thread_configs.get_config",
                return_value=_app_config(config_path),
            ):
                configs = NGAThreadConfigs().get_thread_configs()

        self.assertEqual(configs[0]["thread_name"], "name")
        self.assertEqual(configs[0]["tid"], 101)
        self.assertEqual(configs[0]["description"], None)

    def test_rejects_nested_thread_config_values(self) -> None:
        invalid_values: list[object] = [["tag"], {"nested": True}]

        for value in invalid_values:
            with self.subTest(value=value):
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
                            "nga_tools.thread_configs.get_config",
                            return_value=_app_config(config_path),
                        ),
                        self.assertRaisesRegex(ValueError, "不能是数组或对象"),
                    ):
                        NGAThreadConfigs()

    def test_rejects_missing_required_thread_fields(self) -> None:
        invalid_items = [
            {"tid": 101},
            {"thread_name": "name"},
            {"thread_name": "name", "tid": True},
            {"thread_name": "name", "tid": 101, "aid": "201"},
        ]

        for item in invalid_items:
            with self.subTest(item=item):
                with tempfile.TemporaryDirectory() as tmp_dir:
                    config_path = Path(tmp_dir) / "thread_configs.json"
                    config_path.write_text(
                        json.dumps({"ThreadList": [item]}),
                        encoding="utf-8",
                    )

                    with (
                        patch(
                            "nga_tools.thread_configs.get_config",
                            return_value=_app_config(config_path),
                        ),
                        self.assertRaises(ValueError),
                    ):
                        NGAThreadConfigs()

    def test_add_thread_omits_optional_empty_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "thread_configs.json"
            with patch(
                "nga_tools.thread_configs.get_config",
                return_value=_app_config(config_path),
            ):
                configs = NGAThreadConfigs()
                configs.add_thread("name", 101)
                configs.save_configs()

            data = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(data["ThreadList"], [{"thread_name": "name", "tid": 101}])


if __name__ == "__main__":
    unittest.main()

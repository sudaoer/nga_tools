from __future__ import annotations

import io
import unittest
from unittest.mock import call, patch

from nga_tools.cli import args_parse
from nga_tools.commands.backup import backup_configs
from nga_tools.thread_configs import ThreadConfig


def _thread_config(
    *,
    name: str,
    tid: int,
    aid: int | None,
) -> ThreadConfig:
    return {
        "thread_name": name,
        "tid": tid,
        "aid": aid,
        "description": "",
    }


class BackupConfigsCliTest(unittest.TestCase):
    def test_backup_configs_parses_without_thread_target(self) -> None:
        args = args_parse(["backup", "configs"])

        self.assertEqual(args["command"], "backup")
        self.assertEqual(args["action"], "configs")

    def test_backup_configs_rejects_single_thread_arguments(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as context:
                args_parse(["backup", "configs", "--name", "帖子名"])

        self.assertEqual(context.exception.code, 2)


class BackupConfigsHandlerTest(unittest.TestCase):
    def test_runs_sub_backup_for_each_thread_config_in_order(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch("builtins.print"),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_configs({})

        self.assertEqual(
            backup_mock.call_args_list,
            [call(101, 201), call(102, None)],
        )

    def test_empty_thread_config_list_does_not_run_backup(self) -> None:
        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch("builtins.print") as print_mock,
        ):
            configs_cls.return_value.get_thread_configs.return_value = []

            backup_configs({})

        backup_mock.assert_not_called()
        print_mock.assert_called_once_with("没有找到任何帖子配置。")

    def test_continues_after_failure_and_exits_nonzero(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="broken", tid=102, aid=202),
            _thread_config(name="third", tid=103, aid=None),
        ]

        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch("builtins.print"),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            backup_mock.side_effect = [None, RuntimeError("boom"), None]

            with self.assertRaises(SystemExit) as context:
                backup_configs({})

        self.assertEqual(context.exception.code, 1)
        self.assertEqual(
            backup_mock.call_args_list,
            [call(101, 201), call(102, 202), call(103, None)],
        )


if __name__ == "__main__":
    unittest.main()

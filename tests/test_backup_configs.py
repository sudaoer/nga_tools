from __future__ import annotations

import io
import threading
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import call, patch

from rich.console import Console

from nga_tools.cli import args_parse
from nga_tools.console import ConsoleReporter, use_reporter
from nga_tools.commands.backup import backup_configs
from nga_tools.thread_configs import ThreadConfig


def _thread_config(
    *,
    name: str,
    tid: int,
    aid: int | None,
) -> ThreadConfig:
    thread_config: ThreadConfig = {"thread_name": name, "tid": tid}
    if aid is not None:
        thread_config["aid"] = aid
    return thread_config


class BackupConfigsCliTest(unittest.TestCase):
    def test_backup_configs_parses_without_thread_target(self) -> None:
        args = args_parse(["backup", "configs"])

        self.assertEqual(args["command"], "backup")
        self.assertEqual(args["action"], "configs")

    def test_backup_configs_parses_parallel_limits(self) -> None:
        args = args_parse(
            [
                "backup",
                "configs",
                "--workers",
                "2",
                "--api_concurrency",
                "3",
                "--image_concurrency",
                "20",
            ]
        )

        self.assertEqual(args["workers"], 2)
        self.assertEqual(args["api_concurrency"], 3)
        self.assertEqual(args["image_concurrency"], 20)

    def test_backup_configs_rejects_non_positive_parallel_limits(self) -> None:
        invalid_args = [
            ["backup", "configs", "--workers", "0"],
            ["backup", "configs", "--api_concurrency", "0"],
            ["backup", "configs", "--image_concurrency", "0"],
        ]

        for argv in invalid_args:
            with self.subTest(argv=argv):
                with patch("sys.stderr", new_callable=io.StringIO):
                    with self.assertRaises(SystemExit) as context:
                        args_parse(argv)
                self.assertEqual(context.exception.code, 2)

    def test_backup_configs_rejects_single_thread_arguments(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as context:
                args_parse(["backup", "configs", "--name", "帖子名"])

        self.assertEqual(context.exception.code, 2)


def _backup_config_app_config(workers: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        api_concurrency=4,
        image_concurrency=50,
        backup_configs_workers=workers,
    )


@contextmanager
def _captured_reporter() -> Iterator[io.StringIO]:
    output = io.StringIO()
    console = Console(
        file=output,
        force_terminal=False,
        color_system=None,
        width=120,
    )
    with use_reporter(ConsoleReporter(console)):
        yield output


class BackupConfigsHandlerTest(unittest.TestCase):
    def test_runs_sub_backup_for_each_thread_config_in_order_with_one_worker(
        self,
    ) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_configs({"workers": 1})

        self.assertEqual(
            backup_mock.call_args_list,
            [call(101, 201), call(102, None)],
        )

    def test_empty_thread_config_list_does_not_run_backup(self) -> None:
        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = []

            backup_configs({})

        backup_mock.assert_not_called()
        self.assertIn("没有找到任何帖子配置。", output.getvalue())

    def test_continues_after_failure_and_exits_nonzero(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="broken", tid=102, aid=202),
            _thread_config(name="third", tid=103, aid=None),
        ]

        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=2),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            def backup_side_effect(tid: int, aid: int | None) -> None:
                del aid
                if tid == 102:
                    raise RuntimeError("boom")

            backup_mock.side_effect = backup_side_effect

            with self.assertRaises(SystemExit) as context:
                backup_configs({})

        self.assertEqual(context.exception.code, 1)
        self.assertCountEqual(
            backup_mock.call_args_list,
            [call(101, 201), call(102, 202), call(103, None)],
        )
        output_text = output.getvalue()
        self.assertIn("批量备份完成：成功2个，失败1个。", output_text)
        self.assertIn("失败：broken (tid: 102, aid: 202)：boom", output_text)

    def test_default_workers_run_backups_in_parallel(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=202),
        ]
        active_count = 0
        max_active_count = 0
        lock = threading.Lock()
        release_event = threading.Event()

        def backup_side_effect(tid: int, aid: int | None) -> None:
            nonlocal active_count, max_active_count
            del tid, aid
            with lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
                if active_count == 2:
                    release_event.set()
            self.assertTrue(release_event.wait(timeout=2))
            with lock:
                active_count -= 1

        with (
            patch("nga_tools.commands.backup.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.backup_thread_sub",
                side_effect=backup_side_effect,
            ),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=4),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_configs({})

        self.assertEqual(max_active_count, 2)


if __name__ == "__main__":
    unittest.main()

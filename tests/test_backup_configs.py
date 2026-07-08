from __future__ import annotations

import pytest
import io
from pathlib import Path
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import call, patch

from rich.console import Console

from nga_tools.backup.pdf import PdfRenderPool
from nga_tools.cli import args_parse
from nga_tools.console import ConsoleReporter, report_warning, use_reporter
from nga_tools.commands.backup import (
    backup_all,
    backup_configs,
    backup_floors,
    backup_sub,
    pdf_generate,
)
from nga_tools.commands.stats import stats_words
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.stats.word_count import WordCountSummary


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


class BackupConfigsCliTest:
    def test_backup_configs_parses_without_thread_target(self) -> None:
        args = args_parse(["backup", "configs"])

        assert args['command'] == 'backup'
        assert args['action'] == 'configs'

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

        assert args['workers'] == 2
        assert args['api_concurrency'] == 3
        assert args['image_concurrency'] == 20

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "all", "--all-threads"],
            ["backup", "sub", "--all-threads"],
            ["backup", "floors", "--all-threads"],
            ["backup", "pdf", "--all-threads"],
            ["stats", "words", "--all-threads"],
        ],
    )
    def test_all_threads_parses_for_batch_thread_commands(
        self,
        argv: list[str],
    ) -> None:
        args = args_parse(argv)

        assert args['all_threads'] is True

    def test_all_threads_accepts_underscore_compat_alias(self) -> None:
        args = args_parse(["backup", "sub", "--all_threads"])

        assert args['all_threads'] is True

    def test_hyphenated_fetch_options_parse_to_existing_internal_names(self) -> None:
        args = args_parse(
            [
                "backup",
                "sub",
                "--all-threads",
                "--api-concurrency",
                "3",
                "--image-concurrency",
                "20",
                "--write-json",
            ]
        )

        assert args['api_concurrency'] == 3
        assert args['image_concurrency'] == 20
        assert args['write_json'] is True

    def test_hyphenated_pdf_and_stats_options_parse(self) -> None:
        pdf_args = args_parse(
            [
                "backup",
                "pdf",
                "--all-threads",
                "--lou-per-pdf",
                "50",
                "--pdf-workers",
                "2",
            ]
        )
        stats_args = args_parse(
            ["stats", "words", "--all-threads", "--min-body-chars", "80"]
        )

        assert pdf_args['lou_per_pdf'] == 50
        assert pdf_args['pdf_workers'] == 2
        assert stats_args['min_body_chars'] == 80

    def test_hyphenated_forum_full_postdate_options_parse(self) -> None:
        args = args_parse(
            [
                "forum",
                "sync",
                "--full-postdate",
                "--refresh",
                "--fid",
                "784",
                "--start-page",
                "544",
                "--page-delay-seconds",
                "5",
            ]
        )

        assert args['full_postdate'] is True
        assert args['start_page'] == 544
        assert args['page_delay_seconds'] == 5

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "all", "--tid", "123", "--write_json"],
            ["backup", "sub", "--tid", "123", "--write_json"],
            ["backup", "configs", "--write_json"],
        ],
    )
    def test_backup_write_json_parses_for_fetch_commands(
        self,
        argv: list[str],
    ) -> None:
        args = args_parse(argv)

        assert args['write_json'] is True

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "floors", "--tid", "123", "--write_json"],
            ["backup", "migrate-store", "--tid", "123", "--write_json"],
            ["backup", "pdf", "--tid", "123", "--write_json"],
        ],
    )
    def test_backup_write_json_is_rejected_for_non_fetch_commands(
        self,
        argv: list[str],
    ) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(argv)

        assert context.value.code == 2

    def test_all_threads_rejects_single_thread_arguments(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", "sub", "--all-threads", "--tid", "123"])

        assert context.value.code == 2

    def test_all_threads_is_rejected_for_unsupported_commands(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["image", "migrate", "--all-threads"])

        assert context.value.code == 2

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "configs", "--workers", "0"],
            ["backup", "configs", "--api_concurrency", "0"],
            ["backup", "configs", "--image_concurrency", "0"],
        ],
    )
    def test_backup_configs_rejects_non_positive_parallel_limits(
        self,
        argv: list[str],
    ) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(argv)
        assert context.value.code == 2

    def test_backup_configs_rejects_single_thread_arguments(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", "configs", "--name", "帖子名"])

        assert context.value.code == 2

    def test_backup_migrate_store_parses_all(self) -> None:
        args = args_parse(["backup", "migrate-store", "--all"])

        assert args['command'] == 'backup'
        assert args['action'] == 'migrate-store'
        assert args['all'] is True

    def test_backup_migrate_store_rejects_all_with_thread_target(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", "migrate-store", "--all", "--tid", "123"])

        assert context.value.code == 2


def _backup_config_app_config(workers: int = 4) -> SimpleNamespace:
    return SimpleNamespace(
        api_concurrency=4,
        image_concurrency=50,
        backup_configs_workers=workers,
    )


def _fake_get_folder(base_dir: Path) -> Callable[..., str]:
    def fake_get_folder(
        tid: int,
        aid: int | None,
        subfolder: str | None = None,
    ) -> str:
        aid_part = str(aid) if aid is not None else "all"
        path = base_dir / f"{tid}_{aid_part}"
        if subfolder is not None:
            path = path / subfolder
        path.mkdir(parents=True, exist_ok=True)
        return str(path)

    return fake_get_folder


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


class BackupWarningLogTest:
    @pytest.mark.parametrize(
        ("handler", "implementation_path"),
        [
            (backup_all, "nga_tools.commands.backup.backup_thread"),
            (backup_sub, "nga_tools.commands.backup.backup_thread_sub"),
        ],
    )
    def test_single_thread_backup_passes_write_json_flag(
        self,
        handler: Callable[[dict[str, object]], None],
        implementation_path: str,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)

            with (
                patch(
                    "nga_tools.commands.backup.configure_network_limits_from_args",
                    return_value=_backup_config_app_config(),
                ),
                patch(
                    "nga_tools.commands.backup.resolve_command_thread_target",
                    return_value=(101, None),
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=_fake_get_folder(base_dir),
                ),
                patch(implementation_path) as implementation_mock,
                _captured_reporter(),
            ):
                handler({"write_json": True})

            implementation_mock.assert_called_once_with(
                101,
                None,
                write_json=True,
            )

    @pytest.mark.parametrize(
        ("handler", "implementation_path"),
        [
            (backup_all, "nga_tools.commands.backup.backup_thread"),
            (backup_sub, "nga_tools.commands.backup.backup_thread_sub"),
            (
                backup_floors,
                "nga_tools.commands.backup.generate_floor_map_from_backup",
            ),
        ],
    )
    def test_single_thread_backup_commands_write_warning_log(
        self,
        handler: Callable[[dict[str, object]], None],
        implementation_path: str,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_all"
            thread_dir.mkdir()
            log_path = thread_dir / "warnings.log"
            log_path.write_text("旧日志\n", encoding="utf-8")

            def implementation(
                tid: int,
                aid: int | None,
                **kwargs: object,
            ) -> None:
                assert (tid, aid) == (101, None)
                if handler is backup_floors:
                    assert kwargs == {}
                else:
                    assert kwargs == {'write_json': False}
                report_warning("单帖警告")

            with (
                patch(
                    "nga_tools.commands.backup.configure_network_limits_from_args",
                    return_value=_backup_config_app_config(),
                ),
                patch(
                    "nga_tools.commands.backup.resolve_command_thread_target",
                    return_value=(101, None),
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=_fake_get_folder(base_dir),
                ),
                patch(implementation_path, side_effect=implementation),
                _captured_reporter() as output,
            ):
                handler({})

            assert log_path.read_text(encoding='utf-8') == '警告：单帖警告\n'
            assert '警告：单帖警告' in output.getvalue()

    def test_backup_configs_writes_per_thread_warning_logs(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            (base_dir / "101_201").mkdir()
            (base_dir / "102_all").mkdir()
            (base_dir / "101_201" / "warnings.log").write_text(
                "旧日志\n",
                encoding="utf-8",
            )
            (base_dir / "102_all" / "warnings.log").write_text(
                "旧日志\n",
                encoding="utf-8",
            )

            def backup_side_effect(
                tid: int,
                aid: int | None,
                *,
                write_json: bool,
            ) -> None:
                assert write_json is False
                report_warning(f"warning {tid} {aid}")

            with (
                patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
                patch(
                    "nga_tools.commands.backup.backup_thread_sub",
                    side_effect=backup_side_effect,
                ),
                patch(
                    "nga_tools.commands.backup.configure_network_limits_from_args",
                    return_value=_backup_config_app_config(workers=2),
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=_fake_get_folder(base_dir),
                ),
                _captured_reporter(),
            ):
                configs_cls.return_value.get_thread_configs.return_value = thread_configs

                backup_configs({})

            assert (base_dir / '101_201' / 'warnings.log').read_text(encoding='utf-8') == '警告：warning 101 201\n'
            assert (base_dir / '102_all' / 'warnings.log').read_text(encoding='utf-8') == '警告：warning 102 None\n'

    def test_pdf_generate_writes_thread_warning_log(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_201"
            thread_dir.mkdir()
            log_path = thread_dir / "warnings.log"
            log_path.write_text("旧日志\n", encoding="utf-8")

            def generate_pdf_side_effect(
                *,
                tid: int,
                aid: int | None,
                lou_per_pdf: int,
                pdf_workers: int | None,
            ) -> None:
                assert (tid, aid, lou_per_pdf, pdf_workers) == (101, 201, 50, 2)
                report_warning("PDF告警")

            with (
                patch(
                    "nga_tools.commands.backup.resolve_command_thread_target",
                    return_value=(101, 201),
                ),
                patch(
                    "nga_tools.commands.backup.generate_pdf",
                    side_effect=generate_pdf_side_effect,
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=_fake_get_folder(base_dir),
                ),
                _captured_reporter() as output,
            ):
                pdf_generate({"lou_per_pdf": 50, "pdf_workers": 2})

            assert log_path.read_text(encoding='utf-8') == '警告：PDF告警\n'
            assert '警告：PDF告警' in output.getvalue()


class BackupConfigsHandlerTest:
    def test_backup_sub_all_threads_uses_batch_sub_backup(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_sub({"all_threads": True, "workers": 1})

        assert backup_mock.call_args_list == [call(101, 201, write_json=False), call(102, None, write_json=False)]

    def test_backup_all_all_threads_uses_batch_full_backup(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_all({"all_threads": True, "workers": 1, "write_json": True})

        assert backup_mock.call_args_list == [call(101, 201, write_json=True), call(102, None, write_json=True)]

    def test_backup_floors_all_threads_generates_each_floor_map(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.generate_floor_map_from_backup",
            ) as floor_map_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_floors({"all_threads": True, "workers": 1})

        assert floor_map_mock.call_args_list == [call(101, 201), call(102, None)]

    def test_backup_pdf_all_threads_generates_each_pdf(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.generate_pdf") as pdf_mock,
            patch(
                "nga_tools.commands.backup.get_config",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            pdf_generate(
                {
                    "all_threads": True,
                    "workers": 1,
                    "lou_per_pdf": 50,
                    "pdf_workers": 2,
                }
            )

        first_renderer = pdf_mock.call_args_list[0].kwargs["pdf_renderer"]

        assert isinstance(first_renderer, PdfRenderPool)
        assert first_renderer.pdf_workers == 2
        assert pdf_mock.call_args_list == [
            call(
                tid=101,
                aid=201,
                lou_per_pdf=50,
                pdf_workers=2,
                pdf_renderer=first_renderer,
            ),
            call(
                tid=102,
                aid=None,
                lou_per_pdf=50,
                pdf_workers=2,
                pdf_renderer=first_renderer,
            ),
        ]

    def test_stats_words_all_threads_counts_each_archive(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        def count_side_effect(
            *,
            tid: int,
            aid: int | None,
            min_body_chars: int,
        ) -> WordCountSummary:
            assert min_body_chars == 80
            return WordCountSummary(
                tid=tid,
                aid=aid,
                archive_path=Path(f"/tmp/{tid}.sqlite3"),
                page_count=1,
                total_posts=10,
                body_posts=2,
                excluded_posts=8,
                min_body_chars=min_body_chars,
                chinese_chars=100,
                chinese_with_punctuation=120,
            )

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.core.paths.get_folder") as get_folder_mock,
            patch(
                "nga_tools.commands.stats.count_backup_words",
                side_effect=count_side_effect,
            ) as count_mock,
            patch(
                "nga_tools.commands.stats.get_config",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            get_folder_mock.side_effect = AssertionError(
                "stats batch must not resolve output folders"
            )

            stats_words(
                {
                    "all_threads": True,
                    "workers": 1,
                    "min_body_chars": 80,
                }
            )

        assert count_mock.call_args_list == [
            call(tid=101, aid=201, min_body_chars=80),
            call(tid=102, aid=None, min_body_chars=80),
        ]
        get_folder_mock.assert_not_called()
        output_text = output.getvalue()
        assert "first (tid: 101, aid: 201)：快照页数1" in output_text
        assert "批量统计完成：成功2个，失败0个。" in output_text

    def test_runs_sub_backup_for_each_thread_config_in_order_with_one_worker(
        self,
    ) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_configs({"workers": 1})

        assert backup_mock.call_args_list == [call(101, 201, write_json=False), call(102, None, write_json=False)]

    def test_backup_configs_passes_write_json_to_each_thread(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            backup_configs({"workers": 1, "write_json": True})

        assert backup_mock.call_args_list == [call(101, 201, write_json=True), call(102, None, write_json=True)]

    def test_empty_thread_config_list_does_not_run_backup(self) -> None:
        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
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
        assert '没有找到任何帖子配置。' in output.getvalue()

    def test_continues_after_failure_and_exits_nonzero(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="broken", tid=102, aid=202),
            _thread_config(name="third", tid=103, aid=None),
        ]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub") as backup_mock,
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=2),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs

            def backup_side_effect(
                tid: int,
                aid: int | None,
                *,
                write_json: bool,
            ) -> None:
                assert write_json is False
                del aid
                if tid == 102:
                    raise RuntimeError("boom")

            backup_mock.side_effect = backup_side_effect

            with pytest.raises(SystemExit) as context:
                backup_configs({})

        assert context.value.code == 1
        expected_calls = [
            call(101, 201, write_json=False),
            call(102, 202, write_json=False),
            call(103, None, write_json=False),
        ]
        assert len(backup_mock.call_args_list) == len(expected_calls)
        for expected_call in expected_calls:
            assert expected_call in backup_mock.call_args_list
        output_text = output.getvalue()
        assert '批量备份完成：成功2个，失败1个。' in output_text
        assert '失败：broken (tid: 102, aid: 202)：boom' in output_text

    def test_default_workers_run_backups_in_parallel(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=202),
        ]
        active_count = 0
        max_active_count = 0
        lock = threading.Lock()
        release_event = threading.Event()

        def backup_side_effect(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            nonlocal active_count, max_active_count
            assert write_json is False
            del tid, aid
            with lock:
                active_count += 1
                max_active_count = max(max_active_count, active_count)
                if active_count == 2:
                    release_event.set()
            assert release_event.wait(timeout=2)
            with lock:
                active_count -= 1

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
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

        assert max_active_count == 2

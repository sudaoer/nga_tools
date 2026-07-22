from __future__ import annotations

import pytest
import io
from pathlib import Path
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

from rich.console import Console

from nga_tools.backup.pdf import PdfRenderPool
from nga_tools.backup.image_validation import current_image_validation_cache
from nga_tools.cli import args_parse, format_command_help
from nga_tools.console import (
    ConsoleReporter,
    WarningCategory,
    report_warning,
    use_command_warning_summary,
    use_reporter,
)
from nga_tools.commands.backup import (
    backup_all,
    backup_sub,
    pdf_generate,
)
from nga_tools.commands.thread_batch import (
    run_thread_config_batch,
    thread_config_label,
)
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.ngaclient.client import NGAPageError
from nga_tools.ngaclient.api_runtime import current_api_runtime
from nga_tools.ngaclient.session import current_api_session
from nga_tools.timing import (
    record_timing,
    record_timing_label,
    record_timing_metric,
)


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


def _only_versioned_log(base_path: Path) -> Path:
    paths = list(
        base_path.parent.glob(f"{base_path.stem}-*{base_path.suffix}")
    )
    assert len(paths) == 1
    return paths[0]


class BackupCliTest:
    @pytest.mark.parametrize("action", ["configs", "floors"])
    def test_removed_backup_actions_are_rejected(self, action: str) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", action])

        assert context.value.code == 2

    def test_removed_backup_actions_are_absent_from_help(self) -> None:
        help_text = format_command_help("backup")

        assert "  configs " not in help_text
        assert "  floors " not in help_text

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "all", "--all-threads"],
            ["backup", "sub", "--all-threads"],
            ["backup", "pdf", "--all-threads"],
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
                "--audio-concurrency",
                "6",
                "--write-json",
            ]
        )

        assert args['api_concurrency'] == 3
        assert args['image_concurrency'] == 20
        assert args['audio_concurrency'] == 6
        assert args['write_json'] is True

    def test_hyphenated_pdf_options_parse(self) -> None:
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

        assert pdf_args['lou_per_pdf'] == 50
        assert pdf_args['pdf_workers'] == 2

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
            ["backup", "all", "--tid", "123", "--force-processing"],
            ["backup", "sub", "--tid", "123", "--force_processing"],
            ["backup", "all", "--all-threads", "--force-processing"],
        ],
    )
    def test_force_processing_parses_for_state_reusing_backups(
        self,
        argv: list[str],
    ) -> None:
        args = args_parse(argv)

        assert args["force_processing"] is True

    def test_backup_write_json_is_rejected_for_non_fetch_commands(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", "pdf", "--tid", "123", "--write_json"])

        assert context.value.code == 2

    @pytest.mark.parametrize(
        "action",
        ["migrate-store", "migrate-layout", "migrate-content"],
    )
    def test_removed_backup_migration_commands_are_rejected(
        self,
        action: str,
    ) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", action, "--all"])

        assert context.value.code == 2

    def test_all_threads_rejects_single_thread_arguments(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["backup", "sub", "--all-threads", "--tid", "123"])

        assert context.value.code == 2

    def test_all_threads_is_rejected_for_unsupported_commands(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["image", "verify", "--all-threads"])

        assert context.value.code == 2

    @pytest.mark.parametrize(
        "argv",
        [
            ["backup", "sub", "--all-threads", "--workers", "0"],
            ["backup", "sub", "--all-threads", "--api_concurrency", "0"],
            ["backup", "sub", "--all-threads", "--image_concurrency", "0"],
        ],
    )
    def test_backup_sub_all_threads_rejects_non_positive_parallel_limits(
        self,
        argv: list[str],
    ) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(argv)
        assert context.value.code == 2

def _backup_config_app_config(
    workers: int = 4,
    *,
    timing_log_enabled: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        api_concurrency=4,
        image_concurrency=50,
        audio_concurrency=8,
        backup_configs_workers=workers,
        timing_log_enabled=timing_log_enabled,
    )


def _fake_get_folder(base_dir: Path) -> Callable[..., str]:
    def fake_get_folder(
        tid: int,
        aid: int | None,
        subfolder: str | None = None,
        *,
        create: bool = True,
    ) -> str:
        aid_part = str(aid) if aid is not None else "all"
        path = base_dir / f"{tid}_{aid_part}"
        if subfolder is not None:
            path = path / subfolder
        if create:
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
    def test_thread_config_label_uses_subject_author_and_truncates_subject(self) -> None:
        thread_config = _thread_config(name="sample", tid=101, aid=201)
        thread_config["subject"] = "甲" * 31
        thread_config["author"] = "作者"

        expected_label = f"sample：（{'甲' * 29}…，作者）"

        assert thread_config_label(thread_config) == expected_label

    def test_thread_config_label_keeps_identifier_for_legacy_config(self) -> None:
        thread_config = _thread_config(name="legacy", tid=101, aid=None)

        assert thread_config_label(thread_config) == "legacy (tid: 101, aid: None)"

    def test_batch_warning_summaries_use_the_live_progress_console(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )
        thread_config = _thread_config(name="first", tid=101, aid=201)

        def action(config: ThreadConfig) -> None:
            assert config is thread_config
            report_warning(WarningCategory.POST_CONTENT, "detail")

        with (
            use_reporter(ConsoleReporter(console)),
            use_command_warning_summary(),
            patch(
                "nga_tools.commands.thread_batch.ConsoleReporter",
                side_effect=lambda active_console: ConsoleReporter(active_console),
            ) as summary_reporter_cls,
        ):
            run_thread_config_batch(
                action=action,
                progress_text="running",
                failure_text="failed",
                summary_name="test",
                worker_count=1,
                write_warning_log=False,
                lock_thread_output=False,
                thread_configs=[thread_config],
            )

        summary_reporter_cls.assert_called_once_with(console)
        assert "detail" not in output.getvalue()
        assert "警告汇总：first (tid: 101, aid: 201)" in output.getvalue()

    def test_single_thread_backup_passes_write_json_flag(
        self,
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
                patch(
                    "nga_tools.commands.backup.backup_thread"
                ) as implementation_mock,
                _captured_reporter(),
            ):
                backup_all({"write_json": True})

            implementation_mock.assert_called_once_with(
                101,
                None,
                write_json=True,
            )

    def test_single_thread_backup_uses_shared_api_runtime(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)

            def implementation(
                tid: int,
                aid: int | None,
                **kwargs: object,
            ) -> None:
                assert (tid, aid) == (101, None)
                assert kwargs == {"write_json": False}
                runtime = current_api_runtime()
                assert runtime is not None
                assert runtime.capacity == 4

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
                patch(
                    "nga_tools.commands.backup.backup_thread",
                    side_effect=implementation,
                ),
                _captured_reporter(),
            ):
                backup_all({})

    def test_single_thread_backup_passes_force_processing_flag(
        self,
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
                patch(
                    "nga_tools.commands.backup.backup_thread"
                ) as implementation_mock,
                _captured_reporter(),
            ):
                backup_all({"force_processing": True})

            implementation_mock.assert_called_once_with(
                101,
                None,
                write_json=False,
                force_processing=True,
            )

    def test_single_thread_backup_commands_write_warning_and_timing_logs(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_all"
            thread_dir.mkdir()
            log_path = thread_dir / "warnings.log"
            log_path.write_text("旧日志\n", encoding="utf-8")
            timing_path = thread_dir / "timing.log"
            timing_path.write_text("旧耗时\n", encoding="utf-8")

            def implementation(
                tid: int,
                aid: int | None,
                **kwargs: object,
            ) -> None:
                assert (tid, aid) == (101, None)
                assert kwargs == {'write_json': False}
                report_warning(WarningCategory.POST_CONTENT, "单帖警告")

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
                patch(
                    "nga_tools.commands.backup.backup_thread",
                    side_effect=implementation,
                ),
                _captured_reporter() as output,
            ):
                backup_all({})

            assert log_path.read_text(encoding='utf-8') == '警告：单帖警告\n'
            timing_text = _only_versioned_log(timing_path).read_text(
                encoding="utf-8"
            )
            assert timing_path.read_text(encoding="utf-8") == "旧耗时\n"
            assert "任务：backup all\n" in timing_text
            assert "目标：tid=101, aid=all\n" in timing_text
            assert "总耗时：" in timing_text
            assert "状态：完成" in timing_text
            assert "警告：单帖警告" not in output.getvalue()
            assert (
                "警告汇总：tid=101, aid=all：共1条；帖子内容1条。"
                in output.getvalue()
            )

    def test_single_thread_backup_respects_disabled_timing_log(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_all"
            thread_dir.mkdir()
            timing_path = thread_dir / "timing.log"
            timing_path.write_text("旧耗时\n", encoding="utf-8")

            with (
                patch(
                    "nga_tools.commands.backup.configure_network_limits_from_args",
                    return_value=_backup_config_app_config(timing_log_enabled=False),
                ),
                patch(
                    "nga_tools.commands.backup.resolve_command_thread_target",
                    return_value=(101, None),
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=_fake_get_folder(base_dir),
                ),
                patch("nga_tools.commands.backup.backup_thread_sub"),
                _captured_reporter(),
            ):
                backup_sub({})

            assert timing_path.read_text(encoding="utf-8") == "旧耗时\n"

    def test_backup_sub_batch_writes_per_thread_warning_and_timing_logs(self) -> None:
        first_thread_config = _thread_config(name="first", tid=101, aid=201)
        first_thread_config["subject"] = "first subject"
        first_thread_config["author"] = "first author"
        second_thread_config = _thread_config(name="second", tid=102, aid=None)
        second_thread_config["subject"] = "second subject"
        second_thread_config["author"] = "second author"
        thread_configs = [
            first_thread_config,
            second_thread_config,
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
                report_warning(
                    WarningCategory.POST_CONTENT,
                    f"warning {tid} {aid}",
                )

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
                _captured_reporter() as output,
                use_command_warning_summary(),
            ):
                configs_cls.return_value.get_thread_configs.return_value = thread_configs

                backup_sub({"all_threads": True})

            assert (base_dir / '101_201' / 'warnings.log').read_text(encoding='utf-8') == '警告：warning 101 201\n'
            assert (base_dir / '102_all' / 'warnings.log').read_text(encoding='utf-8') == '警告：warning 102 None\n'
            assert "警告：warning" not in output.getvalue()
            assert output.getvalue().count("警告汇总：") == 2
            assert (
                "警告汇总：first：（first subject，first author）：共1条；"
                "帖子内容1条。"
                in output.getvalue()
            )
            assert (
                "警告总计：共2条，涉及2个帖子；帖子内容2条。"
                in output.getvalue()
            )
            first_timing = _only_versioned_log(
                base_dir / "101_201" / "timing.log"
            ).read_text(encoding="utf-8")
            second_timing = _only_versioned_log(
                base_dir / "102_all" / "timing.log"
            ).read_text(encoding="utf-8")
            assert "任务：backup sub --all-threads\n" in first_timing
            assert "目标：first：（first subject，first author）\n" in first_timing
            assert "总耗时：" in first_timing
            assert "状态：完成" in first_timing
            assert "任务：backup sub --all-threads\n" in second_timing
            assert "目标：second：（second subject，second author）\n" in second_timing

    def test_pdf_generate_writes_thread_warning_and_timing_logs(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_201"
            thread_dir.mkdir()
            log_path = thread_dir / "warnings.log"
            log_path.write_text("旧日志\n", encoding="utf-8")
            timing_path = thread_dir / "timing.log"
            timing_path.write_text("旧耗时\n", encoding="utf-8")

            def generate_pdf_side_effect(
                *,
                tid: int,
                aid: int | None,
                lou_per_pdf: int,
                pdf_workers: int | None,
            ) -> None:
                assert (tid, aid, lou_per_pdf, pdf_workers) == (101, 201, 50, 2)
                report_warning(WarningCategory.PDF, "PDF告警")

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
                patch(
                    "nga_tools.commands.backup.load_timing_log_enabled",
                    return_value=True,
                ),
                _captured_reporter() as output,
            ):
                pdf_generate({"lou_per_pdf": 50, "pdf_workers": 2})

            assert log_path.read_text(encoding='utf-8') == '警告：PDF告警\n'
            timing_text = _only_versioned_log(timing_path).read_text(
                encoding="utf-8"
            )
            assert timing_path.read_text(encoding="utf-8") == "旧耗时\n"
            assert "任务：backup pdf\n" in timing_text
            assert "目标：tid=101, aid=201\n" in timing_text
            assert "总耗时：" in timing_text
            assert "状态：完成" in timing_text
            assert "警告：PDF告警" not in output.getvalue()
            assert (
                "警告汇总：tid=101, aid=201：共1条；PDF生成1条。"
                in output.getvalue()
            )


class BackupBatchHandlerTest:
    def test_parallel_fetch_batch_reuses_one_api_session_per_worker(
        self,
        tmp_path: Path,
    ) -> None:
        thread_configs = [
            _thread_config(name=f"thread-{tid}", tid=tid, aid=None)
            for tid in range(101, 105)
        ]
        first_workers_ready = threading.Barrier(2)
        observations: dict[int, list[object]] = {}
        observations_lock = threading.Lock()
        sessions = [MagicMock(), MagicMock()]

        def backup_side_effect(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            del aid, write_json
            if tid <= 102:
                first_workers_ready.wait()
            session = current_api_session()
            assert session is not None
            with observations_lock:
                observations.setdefault(threading.get_ident(), []).append(session)

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.backup_thread_sub",
                side_effect=backup_side_effect,
            ),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(
                    workers=2,
                    timing_log_enabled=False,
                ),
            ),
            patch(
                "nga_tools.ngaclient.session.create_api_session",
                side_effect=sessions,
            ),
            patch(
                "nga_tools.core.paths.get_folder",
                side_effect=_fake_get_folder(tmp_path),
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            backup_sub({"all_threads": True})

        assert len(observations) == 2
        sessions_by_worker = [set(map(id, items)) for items in observations.values()]
        assert sessions_by_worker[0] != sessions_by_worker[1]
        assert all(len(items) == 1 for items in sessions_by_worker)
        for session in sessions:
            session.close.assert_called_once_with()

    def test_hidden_threads_do_not_make_batch_exit_nonzero(self) -> None:
        thread_configs = [_thread_config(name="hidden", tid=101, aid=201)]

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.backup_thread_sub",
                side_effect=NGAPageError(None, "帖子被设为隐藏"),
            ),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=1),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            backup_sub({"all_threads": True})

        output_text = output.getvalue()
        assert "隐藏跳过1个，失败0个" in output_text
        assert "帖子被设为隐藏" in output_text

    def test_hidden_thread_does_not_mask_other_batch_failure(self) -> None:
        thread_configs = [
            _thread_config(name="hidden", tid=101, aid=201),
            _thread_config(name="broken", tid=102, aid=202),
        ]

        def backup_side_effect(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            del aid, write_json
            if tid == 101:
                raise NGAPageError(None, "帖子被设为隐藏")
            raise RuntimeError("boom")

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.backup_thread_sub",
                side_effect=backup_side_effect,
            ),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=1),
            ),
            _captured_reporter() as output,
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            with pytest.raises(SystemExit) as context:
                backup_sub({"all_threads": True})

        assert context.value.code == 1
        assert "隐藏跳过1个，失败1个" in output.getvalue()

    def test_backup_fetch_batch_writes_aggregated_timing_summary(
        self,
        tmp_path: Path,
    ) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=202),
        ]
        batch_path = tmp_path / "batch_timing.log"
        batch_path.write_text("旧汇总\n", encoding="utf-8")

        def backup_side_effect(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            del aid, write_json
            record_timing("共享阶段", 1.0 if tid == 101 else 4.0)
            if tid == 101:
                record_timing("共享阶段", 2.0)
                record_timing_label("处理状态复用结果", "hit")
                record_timing_metric("图片下载失败/http_4xx", 2)
            else:
                record_timing_label("处理状态复用结果", "archive_changed")

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
                "nga_tools.commands.thread_batch.batch_timing_log_path",
                return_value=batch_path,
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            backup_sub({"all_threads": True})

        summary = _only_versioned_log(batch_path).read_text(encoding="utf-8")
        assert batch_path.read_text(encoding="utf-8") == "旧汇总\n"
        assert "任务：backup sub --all-threads\n" in summary
        assert "墙钟时间：" in summary
        assert "帖子：总数2，成功2，失败0" in summary
        assert "状态：完成（含预期图片下载失败，等待后续重试）" in summary
        assert "处理状态复用：" in summary
        assert "命中1，未命中1，不适用0" in summary
        assert "- archive_changed: 1" in summary
        assert (
            "- 共享阶段: 样本2，线程秒总和=7.000s，"
            "P50=3.000s，P95=4.000s，P99=4.000s，max=4.000s"
            in summary
        )
        assert "主题总耗时（可用线程快照）：" in summary
        assert "最慢主题（Top 5）：" in summary
        assert "批次队列：" in summary
        assert "- 峰值未启动主题数：0" in summary
        assert "预期图片下载失败：2" in summary
        assert "- http_4xx: 2" in summary

    def test_failed_backup_batch_writes_summary_before_nonzero_exit(
        self,
        tmp_path: Path,
    ) -> None:
        thread_configs = [_thread_config(name="broken", tid=101, aid=201)]
        batch_path = tmp_path / "batch_timing.log"

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch(
                "nga_tools.commands.backup.backup_thread_sub",
                side_effect=RuntimeError("boom"),
            ),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(workers=1),
            ),
            patch(
                "nga_tools.commands.thread_batch.batch_timing_log_path",
                return_value=batch_path,
            ),
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            with pytest.raises(SystemExit) as context:
                backup_sub({"all_threads": True})

        assert context.value.code == 1
        summary = _only_versioned_log(batch_path).read_text(encoding="utf-8")
        assert "帖子：总数1，成功0，失败1" in summary
        assert "状态：失败" in summary
        assert "线程异常：1" in summary
        assert "- RuntimeError: 1" in summary

    def test_disabled_timing_keeps_existing_batch_summary_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        thread_configs = [_thread_config(name="first", tid=101, aid=201)]
        batch_path = tmp_path / "batch_timing.log"
        batch_path.write_text("旧汇总\n", encoding="utf-8")

        with (
            patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
            patch("nga_tools.commands.backup.backup_thread_sub"),
            patch(
                "nga_tools.commands.backup.configure_network_limits_from_args",
                return_value=_backup_config_app_config(
                    workers=1,
                    timing_log_enabled=False,
                ),
            ),
            patch(
                "nga_tools.commands.thread_batch.batch_timing_log_path",
                return_value=batch_path,
            ) as batch_path_mock,
            _captured_reporter(),
        ):
            configs_cls.return_value.get_thread_configs.return_value = thread_configs
            backup_sub({"all_threads": True})

        batch_path_mock.assert_not_called()
        assert batch_path.read_text(encoding="utf-8") == "旧汇总\n"

    def test_parallel_fetch_batch_shares_one_image_validation_cache(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="second", tid=102, aid=None),
        ]
        validation_cache_ids: list[int] = []

        def capture_validation_cache(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            del tid, aid, write_json
            validation_cache = current_image_validation_cache()
            assert validation_cache is not None
            validation_cache_ids.append(id(validation_cache))

        with TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            with (
                patch(
                    "nga_tools.commands.thread_batch.NGAThreadConfigs"
                ) as configs_cls,
                patch(
                    "nga_tools.commands.backup.backup_thread_sub",
                    side_effect=capture_validation_cache,
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
                backup_sub({"all_threads": True, "workers": 2})

        assert len(validation_cache_ids) == 2
        assert len(set(validation_cache_ids)) == 1

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

            backup_sub(
                {"all_threads": True, "workers": 1, "write_json": True}
            )

        assert backup_mock.call_args_list == [
            call(101, 201, write_json=True),
            call(102, None, write_json=True),
        ]

    def test_backup_all_all_threads_passes_force_processing(self) -> None:
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

            backup_all(
                {
                    "all_threads": True,
                    "workers": 1,
                    "force_processing": True,
                }
            )

        assert backup_mock.call_args_list == [
            call(101, 201, write_json=False, force_processing=True),
            call(102, None, write_json=False, force_processing=True),
        ]

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
            patch(
                "nga_tools.commands.thread_batch.batch_timing_log_path"
            ) as batch_path_mock,
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
        batch_path_mock.assert_not_called()

    def test_duplicate_parallel_thread_configs_fail_on_output_lock(self) -> None:
        thread_configs = [
            _thread_config(name="first", tid=101, aid=201),
            _thread_config(name="duplicate", tid=101, aid=201),
        ]
        release_event = threading.Event()
        entered_action = threading.Event()

        def backup_side_effect(
            tid: int,
            aid: int | None,
            *,
            write_json: bool,
        ) -> None:
            assert (tid, aid, write_json) == (101, 201, False)
            entered_action.set()
            assert release_event.wait(timeout=2)

        release_timer = threading.Timer(0.25, release_event.set)
        try:
            with (
                patch("nga_tools.commands.thread_batch.NGAThreadConfigs") as configs_cls,
                patch(
                    "nga_tools.commands.backup.backup_thread_sub",
                    side_effect=backup_side_effect,
                ) as backup_mock,
                patch(
                    "nga_tools.commands.backup.configure_network_limits_from_args",
                    return_value=_backup_config_app_config(workers=4),
                ),
                _captured_reporter() as output,
            ):
                configs_cls.return_value.get_thread_configs.return_value = thread_configs
                release_timer.start()

                with pytest.raises(SystemExit) as context:
                    backup_sub({"all_threads": True, "workers": 2})
        finally:
            release_event.set()
            release_timer.cancel()

        assert context.value.code == 1
        assert entered_action.is_set()
        assert backup_mock.call_count == 1
        output_text = output.getvalue()
        assert "输出目录正在被另一个任务使用" in output_text
        assert "批量备份完成：成功1个，失败1个。" in output_text

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

            backup_sub({"all_threads": True})

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
                backup_sub({"all_threads": True})

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

            backup_sub({"all_threads": True})

        assert max_active_count == 2

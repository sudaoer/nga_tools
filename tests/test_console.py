from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console
from rich.progress import TimeElapsedColumn

from nga_tools.console import (
    BackupConfigsProgressDisplay,
    ConsoleReporter,
    InlineProgress,
    report_info,
    report_warning,
    use_reporter,
    use_warning_log,
)


class InlineProgressTest:
    def test_updates_reuse_current_line_and_finish_once(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.update("abcdef")
        progress.update("xy")
        progress.finish()
        progress.finish()

        assert output.getvalue() == '\rabcdef\rxy    \rxy\n'

    def test_finish_without_update_is_noop(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.finish()

        assert output.getvalue() == ''


class ConsoleReporterTest:
    def test_plain_messages_are_written_without_markup_parsing(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )
        reporter = ConsoleReporter(console)

        reporter.info("[攻略] foo [/x]")
        reporter.warning("[公告] bar [/b]")

        assert output.getvalue() == '[攻略] foo [/x]\n警告：[公告] bar [/b]\n'

    def test_warning_log_mirrors_warnings_and_overwrites_file(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )

        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "thread" / "warnings.log"
            log_path.parent.mkdir()
            log_path.write_text("旧日志\n", encoding="utf-8")

            with (
                use_reporter(ConsoleReporter(console)),
                use_warning_log(log_path),
            ):
                report_info("[攻略] foo [/x]")
                report_warning("[公告] bar [/b]")

            assert log_path.read_text(encoding='utf-8') == '警告：[公告] bar [/b]\n'

        assert output.getvalue() == '[攻略] foo [/x]\n警告：[公告] bar [/b]\n'


class BackupConfigsProgressDisplayTest:
    def test_finished_thread_task_is_hidden_and_warning_is_written(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )

        with BackupConfigsProgressDisplay(2, console=console) as display:
            reporter = display.start_thread(index=1, total=2, label="first")
            reporter.warning("图片链接无效")
            reporter.progress("正在获取第1页", completed=0, total=2)
            display.finish_thread(reporter, status="完成")
            visible_tasks = display.visible_task_descriptions()

        assert visible_tasks == ['总进度']
        assert '警告：first：图片链接无效' in output.getvalue()

    def test_stage_completion_does_not_finish_thread_task(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=160,
        )

        with BackupConfigsProgressDisplay(1, console=console) as display:
            reporter = display.start_thread(index=1, total=1, label="first")
            task = display._progress.tasks[reporter.task_id]

            reporter.progress("页面获取完成", completed=1, total=1)

            assert task.total is None
            assert task.completed == 0
            assert task.fields["stage_completed"] == 1
            assert task.fields["stage_total"] == 1
            assert task.finished is False
            assert task.finished_time is None

            reporter.progress("继续处理")

            assert task.fields["stage_completed"] is None
            assert task.fields["stage_total"] is None
            assert task.fields["progress_text"] == ""
            assert task.finished is False

    def test_zero_total_stage_does_not_finish_thread_task(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=160,
        )

        with BackupConfigsProgressDisplay(1, console=console) as display:
            reporter = display.start_thread(index=1, total=1, label="first")
            task = display._progress.tasks[reporter.task_id]

            reporter.progress("没有图片", completed=0, total=0)

            assert task.total is None
            assert task.completed == 0
            assert task.fields["progress_text"] == "0/0"
            assert task.finished is False
            assert task.finished_time is None

    def test_elapsed_time_keeps_advancing_after_stage_completion(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=160,
        )
        elapsed_column = TimeElapsedColumn()

        with BackupConfigsProgressDisplay(1, console=console) as display:
            reporter = display.start_thread(index=1, total=1, label="first")
            task = display._progress.tasks[reporter.task_id]
            assert task.start_time is not None
            start_time = task.start_time

            reporter.progress("页面获取完成", completed=1, total=1)
            task._get_time = lambda: start_time + 7
            first_elapsed = str(elapsed_column.render(task))
            task._get_time = lambda: start_time + 12
            second_elapsed = str(elapsed_column.render(task))

            assert first_elapsed == "0:00:07"
            assert second_elapsed == "0:00:12"
            assert task.finished is False

    def test_progress_runtime_text_is_rendered_without_markup_parsing(
        self,
    ) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=240,
        )

        with BackupConfigsProgressDisplay(1, console=console) as display:
            reporter = display.start_thread(
                index=1,
                total=1,
                label="[攻] foo [/x]",
            )
            reporter.progress("取 [p] [/x]", completed=1, total=2)
            reporter.warning("坏 [b] [/x]")
            visible_tasks = display.visible_task_descriptions()

        output_text = output.getvalue()
        assert '[1/1] [攻] foo [/x]' in visible_tasks
        assert '警告：[攻] foo [/x]：坏 [b] [/x]' in output_text

    def test_long_task_label_stays_on_one_named_progress_row(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=True,
            color_system=None,
            width=160,
        )
        label = (
            "这是一个非常长的NGA主题标题包含很多中文字符以及tid aid "
            "(tid: 12345678, aid: 87654321)"
        )

        with BackupConfigsProgressDisplay(235, console=console) as display:
            reporter = display.start_thread(index=12, total=235, label=label)
            reporter.progress(
                "正在获取第123页，准备下载图片和生成HTML",
                completed=3,
                total=235,
            )

        output_text = output.getvalue()
        assert '[12/235]' in output_text
        assert '这是一个非常长的NGA主题标题' in output_text
        assert '\n  …' not in output_text

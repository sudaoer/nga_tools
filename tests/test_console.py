from __future__ import annotations

import io
from pathlib import Path
from tempfile import TemporaryDirectory

from rich.console import Console

from nga_tools.console import (
    BackupConfigsProgressDisplay,
    ConsoleReporter,
    WarningCategory,
    report_info,
    report_warning,
    use_command_warning_summary,
    use_reporter,
    use_thread_warning_summary,
    use_warning_log,
)


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
        reporter.warning(WarningCategory.POST_CONTENT, "[公告] bar [/b]")

        assert output.getvalue() == '[攻略] foo [/x]\n警告：[公告] bar [/b]\n'

    def test_command_and_thread_warning_summaries_hide_details(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )

        with (
            use_reporter(ConsoleReporter(console)),
            use_command_warning_summary(),
        ):
            report_warning(WarningCategory.CACHE, "命令级缓存详情")
            with use_thread_warning_summary("thread-1"):
                report_warning(WarningCategory.IMAGE_DOWNLOAD, "url-1")
                report_warning(WarningCategory.IMAGE_DOWNLOAD, "url-2")
                report_warning(WarningCategory.FLOOR_MAP, "floor-1")

        output_text = output.getvalue()
        assert "命令级缓存详情" not in output_text
        assert "url-1" not in output_text
        assert "floor-1" not in output_text
        assert (
            "警告汇总：thread-1：共3条；图片下载2条，楼层映射1条。"
            in output_text
        )
        assert (
            "警告总计：共4条，涉及1个帖子；"
            "图片下载2条，楼层映射1条，缓存1条。"
            in output_text
        )

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
                report_warning(WarningCategory.POST_CONTENT, "[公告] bar [/b]")

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
            reporter.warning(WarningCategory.IMAGE_DOWNLOAD, "图片链接无效")
            reporter.progress("正在获取第1页", completed=0, total=2)
            display.finish_thread(reporter, status="完成")
            visible_tasks = display.visible_task_descriptions()

        assert visible_tasks == ['总进度']
        assert (
            "警告汇总：first：共1条；图片下载1条。" in output.getvalue()
        )

    def test_stage_updates_keep_thread_visible_until_explicit_finish(
        self,
    ) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=160,
        )

        with BackupConfigsProgressDisplay(1, console=console) as display:
            reporter = display.start_thread(index=1, total=1, label="first")

            reporter.progress("页面获取完成", completed=1, total=1)
            assert display.visible_task_descriptions() == [
                "总进度",
                "[1/1] first",
            ]
            reporter.progress("继续处理")
            reporter.progress("没有图片", completed=0, total=0)
            assert display.visible_task_descriptions() == [
                "总进度",
                "[1/1] first",
            ]
            display.finish_thread(reporter, status="完成")
            assert display.visible_task_descriptions() == ["总进度"]

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
            reporter.warning(WarningCategory.POST_CONTENT, "坏 [b] [/x]")
            visible_tasks = display.visible_task_descriptions()
            display.finish_thread(reporter, status="完成")

        output_text = output.getvalue()
        assert '[1/1] [攻] foo [/x]' in visible_tasks
        assert (
            "警告汇总：[攻] foo [/x]：共1条；帖子内容1条。"
            in output_text
        )

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

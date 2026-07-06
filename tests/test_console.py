from __future__ import annotations

import io
import unittest

from rich.console import Console

from nga_tools.console import (
    BackupConfigsProgressDisplay,
    ConsoleReporter,
    InlineProgress,
)


class InlineProgressTest(unittest.TestCase):
    def test_updates_reuse_current_line_and_finish_once(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.update("abcdef")
        progress.update("xy")
        progress.finish()
        progress.finish()

        self.assertEqual(output.getvalue(), "\rabcdef\rxy    \rxy\n")

    def test_finish_without_update_is_noop(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.finish()

        self.assertEqual(output.getvalue(), "")


class ConsoleReporterTest(unittest.TestCase):
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

        self.assertEqual(
            output.getvalue(),
            "[攻略] foo [/x]\n警告：[公告] bar [/b]\n",
        )


class BackupConfigsProgressDisplayTest(unittest.TestCase):
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

        self.assertEqual(visible_tasks, ["总进度"])
        self.assertIn("警告：first：图片链接无效", output.getvalue())

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
        self.assertIn("[1/1] [攻] foo [/x]", visible_tasks)
        self.assertIn("警告：[攻] foo [/x]：坏 [b] [/x]", output_text)

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
        self.assertIn("[12/235]", output_text)
        self.assertIn("这是一个非常长的NGA主题标题", output_text)
        self.assertNotIn("\n  …", output_text)


if __name__ == "__main__":
    unittest.main()

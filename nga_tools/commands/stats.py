from __future__ import annotations

from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.thread_batch import (
    run_thread_config_batch,
    thread_config_label,
)
from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    required_int,
)
from nga_tools.forum.thread_configs import (
    ThreadConfig,
    thread_config_aid,
    thread_config_tid,
)
from nga_tools.stats import count_backup_words


def stats_words(args: CommandArgs) -> None:
    min_body_chars = required_int(args, "min_body_chars")
    if optional_bool(args, "all_threads"):
        worker_arg = optional_int(args, "workers")
        worker_count = (
            get_config().backup_configs_workers if worker_arg is None else worker_arg
        )

        def action(thread_config: ThreadConfig) -> str:
            summary = count_backup_words(
                tid=thread_config_tid(thread_config),
                aid=thread_config_aid(thread_config),
                min_body_chars=min_body_chars,
            )
            return (
                f"{thread_config_label(thread_config)}："
                f"快照页数{summary.page_count}，"
                f"总楼层数{summary.total_posts}，"
                f"正文楼层数{summary.body_posts}，"
                f"中文汉字数{summary.chinese_chars}，"
                f"中文+中文标点数{summary.chinese_with_punctuation}"
            )

        run_thread_config_batch(
            action=action,
            progress_text="正在统计字数",
            failure_text="统计失败",
            summary_name="统计",
            worker_count=worker_count,
            write_warning_log=False,
        )
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    summary = count_backup_words(
        tid=thread_tid,
        aid=thread_aid,
        min_body_chars=min_body_chars,
    )

    aid_text = summary.aid if summary.aid is not None else "all"
    report_info(f"统计目标：tid={summary.tid}, aid={aid_text}")
    report_info(f"Archive库：{summary.archive_path}")
    report_info(f"快照页数：{summary.page_count}")
    report_info(f"总楼层数：{summary.total_posts}")
    report_info(f"正文楼层数：{summary.body_posts}")
    report_info(f"排除楼层数：{summary.excluded_posts}")
    report_info(f"正文判定阈值：中文+中文标点 >= {summary.min_body_chars}")
    report_info(f"中文汉字数：{summary.chinese_chars}")
    report_info(f"中文+中文标点数：{summary.chinese_with_punctuation}")

from __future__ import annotations

from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, required_int
from nga_tools.stats import count_backup_words


def stats_words(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    min_body_chars = required_int(args, "min_body_chars")
    summary = count_backup_words(
        tid=thread_tid,
        aid=thread_aid,
        min_body_chars=min_body_chars,
    )

    aid_text = summary.aid if summary.aid is not None else "all"
    print(f"统计目标：tid={summary.tid}, aid={aid_text}")
    print(f"JSON目录：{summary.json_folder}")
    print(f"JSON页数：{summary.page_count}")
    print(f"总楼层数：{summary.total_posts}")
    print(f"正文楼层数：{summary.body_posts}")
    print(f"排除楼层数：{summary.excluded_posts}")
    print(f"正文判定阈值：中文+中文标点 >= {summary.min_body_chars}")
    print(f"中文汉字数：{summary.chinese_chars}")
    print(f"中文+中文标点数：{summary.chinese_with_punctuation}")

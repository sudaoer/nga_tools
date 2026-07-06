from __future__ import annotations

from nga_tools.console import report_info
from nga_tools.commands.types import (
    CommandArgs,
    optional_int,
    optional_str,
    required_int,
    required_str,
)
from nga_tools.thread_configs import (
    NGAThreadConfigs,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)


def handle_thread_add(args: CommandArgs) -> None:
    name = required_str(args, "name")
    tid = required_int(args, "tid")
    aid = optional_int(args, "aid")
    description = optional_str(args, "description") or ""

    thread_configs = NGAThreadConfigs()
    added = thread_configs.add_thread(
        thread_name=name,
        tid=tid,
        aid=aid,
        description=description,
    )
    if not added:
        report_info("该帖子配置已存在，跳过添加。")
        return
    thread_configs.save_configs()
    report_info(f"已添加帖子配置：{name} (tid: {tid}, aid: {aid})")


def handle_thread_list(args: CommandArgs) -> None:
    del args

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        report_info("没有找到任何帖子配置。")
        return

    for thread in thread_configs:
        description = thread.get("description", "")
        link = thread.get("link")
        link_text = f", 链接: {link}" if isinstance(link, str) else ""
        report_info(
            f"名称: {thread_config_name(thread)}, tid: {thread_config_tid(thread)}, "
            f"aid: {thread_config_aid(thread)}, 描述: {description}{link_text}"
        )

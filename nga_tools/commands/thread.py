from __future__ import annotations

from nga_tools.commands.types import (
    CommandArgs,
    optional_int,
    optional_str,
    required_int,
    required_str,
)
from nga_tools.thread_configs import NGAThreadConfigs


def handle_thread_add(args: CommandArgs) -> None:
    name = required_str(args, "name")
    tid = required_int(args, "tid")
    aid = optional_int(args, "aid")
    description = optional_str(args, "description") or ""

    thread_configs = NGAThreadConfigs()
    thread_configs.add_thread(
        thread_name=name,
        tid=tid,
        aid=aid,
        description=description,
    )
    thread_configs.save_configs()
    print(f"已添加帖子配置：{name} (tid: {tid}, aid: {aid})")


def handle_thread_list(args: CommandArgs) -> None:
    del args

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        print("没有找到任何帖子配置。")
        return

    for thread in thread_configs:
        print(
            f"名称: {thread['thread_name']}, tid: {thread['tid']}, "
            f"aid: {thread.get('aid')}, 描述: {thread.get('description','')}"
        )

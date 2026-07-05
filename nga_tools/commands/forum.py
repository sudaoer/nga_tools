from __future__ import annotations

from collections import Counter

from nga_tools.commands.types import (
    CommandArgs,
    optional_str,
    required_int,
)
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.forum_watch import (
    DEFAULT_WATCH_CONFIG_PATH,
    collect_matching_threads,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
)
from nga_tools.ngaclient import NGAClient
from nga_tools.thread_configs import NGAThreadConfigs


def handle_forum_list(args: CommandArgs) -> None:
    fid = required_int(args, "fid")
    pages = required_int(args, "pages")
    configure_network_limits_from_args(args)

    client = NGAClient()
    total_threads = 0
    for page in range(1, pages + 1):
        threads = client.get_forum_threads(fid, page)
        total_threads += len(threads)
        print(f"第{page}页：{len(threads)}个主题")
        for thread in threads:
            print(
                f"tid: {thread['tid']}, aid: {thread['authorid']}, "
                f"replies: {thread['replies']}, title: {thread['subject']}"
            )

    print(f"共扫描{total_threads}个主题。")


def handle_forum_sync(args: CommandArgs) -> None:
    watch_config_path = optional_str(args, "watch_config") or DEFAULT_WATCH_CONFIG_PATH
    watch_configs = load_forum_watch_configs(watch_config_path)
    if not watch_configs:
        print("没有找到任何版面监控配置。")
        return

    configure_network_limits_from_args(args)
    client = NGAClient()
    scanned_count, matches = collect_matching_threads(client, watch_configs)

    thread_configs = NGAThreadConfigs()
    outcomes = sync_matches_to_thread_list(thread_configs.ThreadList, matches)
    status_counts = Counter(outcome.status for outcome in outcomes)
    if status_counts["added"] > 0:
        thread_configs.save_configs()

    print(
        f"扫描{scanned_count}个主题，匹配{len(matches)}个；"
        f"新增{status_counts['added']}个，跳过{status_counts['skipped']}个，"
        f"冲突{status_counts['conflict']}个。"
    )
    for outcome in outcomes:
        thread = outcome.match.thread
        print(
            f"[{outcome.status}] {outcome.match.thread_name} "
            f"(tid={thread['tid']}, aid={thread['authorid']}) - {outcome.message}"
        )

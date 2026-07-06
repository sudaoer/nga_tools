from __future__ import annotations

from collections import Counter
from pathlib import Path

from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    optional_str,
    required_int,
)
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.forum_watch import (
    DEFAULT_WATCH_CONFIG_PATH,
    ForumScanProgress,
    collect_matching_threads,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
)
from nga_tools.forum_export import (
    DEFAULT_PAGE_DELAY_SECONDS,
    ForumPostdateScanProgress,
    default_postdate_scan_output_path,
    scan_postdate_forum_threads,
    unique_fids,
)
from nga_tools.console import InlineProgress
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


def _postdate_scan_fids(args: CommandArgs) -> list[int]:
    fid = optional_int(args, "fid")
    if fid is not None:
        return [fid]

    watch_config_path = optional_str(args, "watch_config") or DEFAULT_WATCH_CONFIG_PATH
    watch_configs = load_forum_watch_configs(watch_config_path)
    fids = unique_fids(watch_config["fid"] for watch_config in watch_configs)
    if not fids:
        raise ValueError("没有找到可扫描的版面fid。")
    return fids


def _postdate_scan_output_path(args: CommandArgs) -> Path:
    output_path = optional_str(args, "scan_output")
    if output_path is not None:
        return Path(output_path)
    return default_postdate_scan_output_path()


def _handle_forum_sync_full_postdate(args: CommandArgs) -> None:
    configure_network_limits_from_args(args)
    client = NGAClient()
    fids = _postdate_scan_fids(args)
    output_path = _postdate_scan_output_path(args)
    page_delay_arg = optional_int(args, "page_delay_seconds")
    page_delay_seconds = (
        DEFAULT_PAGE_DELAY_SECONDS if page_delay_arg is None else page_delay_arg
    )
    progress_display = InlineProgress()

    def update_progress(progress: ForumPostdateScanProgress) -> None:
        total_pages = "?" if progress.total_pages is None else str(progress.total_pages)
        progress_display.update(
            f"正在按发布时间扫描 fid={progress.fid} "
            f"第{progress.page}/{total_pages}页，已写"
            f"{progress.written_count}个，{progress.message}"
        )

    try:
        result = scan_postdate_forum_threads(
            client,
            fids=fids,
            output_path=output_path,
            page_delay_seconds=page_delay_seconds,
            progress_callback=update_progress,
        )
    finally:
        progress_display.finish()

    fid_text = ", ".join(str(fid) for fid in result.fids)
    print(
        f"发布时间扫描完成：fid={fid_text}，扫描{result.page_count}页，"
        f"写入{result.thread_count}个主题。"
    )
    print(f"输出：{result.output_path}")


def handle_forum_sync(args: CommandArgs) -> None:
    if optional_bool(args, "full_postdate"):
        _handle_forum_sync_full_postdate(args)
        return

    if optional_int(args, "fid") is not None or optional_str(args, "scan_output"):
        raise ValueError("--fid和--scan_output仅在--full_postdate模式下可用。")

    watch_config_path = optional_str(args, "watch_config") or DEFAULT_WATCH_CONFIG_PATH
    watch_configs = load_forum_watch_configs(watch_config_path)
    if not watch_configs:
        print("没有找到任何版面监控配置。")
        return

    configure_network_limits_from_args(args)
    client = NGAClient()
    thread_configs = NGAThreadConfigs()
    progress_display = InlineProgress()

    def update_progress(progress: ForumScanProgress) -> None:
        progress_display.update(
            f"正在扫描 {progress.watch_name} fid={progress.fid} "
            f"第{progress.page}/{progress.pages}页，已扫描"
            f"{progress.scanned_count}个，匹配{progress.matched_count}个"
        )

    try:
        scanned_count, matches = collect_matching_threads(
            client,
            watch_configs,
            progress_callback=update_progress,
            existing_thread_list=thread_configs.ThreadList,
        )
    finally:
        progress_display.finish()

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
        if outcome.status == "skipped":
            continue
        thread = outcome.match.thread
        print(
            f"[{outcome.status}] {outcome.match.thread_name} "
            f"(tid={thread['tid']}, aid={thread['authorid']}) - {outcome.message}"
        )

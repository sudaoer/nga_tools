from __future__ import annotations

from collections import Counter

from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    optional_str,
    required_int,
)
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.forum.watch import (
    DEFAULT_WATCH_CONFIG_PATH,
    ForumDatabaseScanProgress,
    ForumWatchConfig,
    collect_matching_threads_from_thread_source,
    load_forum_watch_configs,
    sync_matches_to_thread_list,
)
from nga_tools.forum.export import (
    DEFAULT_PAGE_DELAY_SECONDS,
    ForumPostdateScanProgress,
    sync_postdate_forum_threads_to_db,
    unique_fids,
)
from nga_tools.forum.thread_store import ForumThreadStore
from nga_tools.console import InlineProgress, report_info
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import ForumThread
from nga_tools.forum.thread_configs import NGAThreadConfigs


def handle_forum_list(args: CommandArgs) -> None:
    fid = required_int(args, "fid")
    pages = required_int(args, "pages")
    configure_network_limits_from_args(args)

    client = NGAClient()
    total_threads = 0
    for page in range(1, pages + 1):
        threads = client.get_forum_threads(fid, page)
        total_threads += len(threads)
        report_info(f"第{page}页：{len(threads)}个主题")
        for thread in threads:
            report_info(
                f"tid: {thread['tid']}, aid: {thread['authorid']}, "
                f"replies: {thread['replies']}, title: {thread['subject']}"
            )

    report_info(f"共扫描{total_threads}个主题。")


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


def _handle_forum_sync_full_postdate(args: CommandArgs) -> None:
    configure_network_limits_from_args(args)
    client = NGAClient()
    fids = _postdate_scan_fids(args)
    page_delay_arg = optional_int(args, "page_delay_seconds")
    page_delay_seconds = (
        DEFAULT_PAGE_DELAY_SECONDS if page_delay_arg is None else page_delay_arg
    )
    refresh = optional_bool(args, "refresh")
    start_page_arg = optional_int(args, "start_page")
    start_page = 1 if start_page_arg is None else start_page_arg
    if start_page_arg is not None and not refresh:
        raise ValueError("--start_page仅支持与--full_postdate --refresh一起使用。")
    forum_store = ForumThreadStore()
    progress_display = InlineProgress()

    def update_progress(progress: ForumPostdateScanProgress) -> None:
        total_pages = "?" if progress.total_pages is None else str(progress.total_pages)
        progress_display.update(
            f"正在按发布时间扫描 fid={progress.fid} "
            f"第{progress.page}/{total_pages}页，已保存"
            f"{progress.written_count}个，{progress.message}"
        )

    try:
        result = sync_postdate_forum_threads_to_db(
            client,
            fids=fids,
            store=forum_store,
            start_page=start_page,
            page_delay_seconds=page_delay_seconds,
            refresh=refresh,
            progress_callback=update_progress,
        )
    finally:
        progress_display.finish()

    fid_text = ", ".join(str(fid) for fid in result.fids)
    report_info(
        f"发布时间扫描完成：fid={fid_text}，扫描{result.page_count}页，"
        f"保存{result.thread_count}个主题；"
        f"新增{result.inserted_count}个，更新{result.updated_count}个。"
    )
    if result.stopped_existing_count > 0:
        report_info(f"遇到{result.stopped_existing_count}个数据库已有主题，已停止后续扫描。")
    report_info(f"数据库：{result.db_path}")


def _max_default_pages_by_fid(
    watch_configs: list[ForumWatchConfig],
) -> dict[int, int]:
    pages_by_fid: dict[int, int] = {}
    for watch_config in watch_configs:
        fid = watch_config["fid"]
        pages = watch_config["pages"]
        pages_by_fid[fid] = max(pages_by_fid.get(fid, 0), pages)
    return pages_by_fid


def _fetch_default_forum_pages_to_db(
    client: NGAClient,
    forum_store: ForumThreadStore,
    watch_configs: list[ForumWatchConfig],
    progress_display: InlineProgress,
) -> tuple[int, int, int]:
    fetched_count = 0
    db_inserted_count = 0
    db_updated_count = 0

    for fid, pages in _max_default_pages_by_fid(watch_configs).items():
        for page in range(1, pages + 1):
            progress_display.update(
                f"正在抓取 fid={fid} 第{page}/{pages}页，"
                f"已保存{fetched_count}个"
            )
            threads = client.get_forum_threads(fid, page)
            result = forum_store.upsert_threads(fid, threads)
            fetched_count += len(threads)
            db_inserted_count += result.inserted_count
            db_updated_count += result.updated_count
            progress_display.update(
                f"正在抓取 fid={fid} 第{page}/{pages}页，"
                f"已保存{fetched_count}个"
            )

    return fetched_count, db_inserted_count, db_updated_count


def handle_forum_sync(args: CommandArgs) -> None:
    if optional_bool(args, "full_postdate"):
        _handle_forum_sync_full_postdate(args)
        return

    if (
        optional_int(args, "fid") is not None
        or optional_int(args, "start_page") is not None
        or optional_bool(args, "refresh")
    ):
        raise ValueError("--fid、--refresh和--start_page仅在--full_postdate模式下可用。")

    watch_config_path = optional_str(args, "watch_config") or DEFAULT_WATCH_CONFIG_PATH
    watch_configs = load_forum_watch_configs(watch_config_path)
    if not watch_configs:
        report_info("没有找到任何版面监控配置。")
        return

    configure_network_limits_from_args(args)
    client = NGAClient()
    thread_configs = NGAThreadConfigs()
    forum_store = ForumThreadStore()
    progress_display = InlineProgress()

    def update_db_scan_progress(progress: ForumDatabaseScanProgress) -> None:
        progress_display.update(
            f"正在筛查数据库 {progress.watch_name} fid={progress.fid}，"
            f"已扫描{progress.scanned_count}个，匹配{progress.matched_count}个"
        )

    def threads_for_watch(watch_config: ForumWatchConfig) -> list[ForumThread]:
        return forum_store.list_threads(
            watch_config["fid"],
            forumname=watch_config["watch_name"],
        )

    try:
        fetched_count, db_inserted_count, db_updated_count = (
            _fetch_default_forum_pages_to_db(
                client,
                forum_store,
                watch_configs,
                progress_display,
            )
        )
        scanned_count, matches = collect_matching_threads_from_thread_source(
            client=client,
            watch_configs=watch_configs,
            thread_source=threads_for_watch,
            progress_callback=update_db_scan_progress,
            existing_thread_list=thread_configs.ThreadList,
        )
    finally:
        progress_display.finish()

    outcomes = sync_matches_to_thread_list(
        thread_configs.ThreadList,
        matches,
        base_url=client.base_url,
    )
    status_counts = Counter(outcome.status for outcome in outcomes)
    if status_counts["added"] > 0 or status_counts["updated"] > 0:
        thread_configs.save_configs()

    report_info(
        f"远端抓取{fetched_count}个主题，"
        f"数据库新增{db_inserted_count}个，更新{db_updated_count}个。"
    )
    report_info(
        f"数据库筛查{scanned_count}个主题，匹配{len(matches)}个；"
        f"新增{status_counts['added']}个，更新{status_counts['updated']}个，"
        f"跳过{status_counts['skipped']}个，冲突{status_counts['conflict']}个。"
    )
    report_info(f"主题数据库：路径：{forum_store.db_path}")
    for outcome in outcomes:
        if outcome.status == "skipped":
            continue
        thread = outcome.match.thread
        report_info(
            f"[{outcome.status}] {outcome.match.thread_name} "
            f"(tid={thread['tid']}, aid={thread['authorid']}) - {outcome.message}"
        )

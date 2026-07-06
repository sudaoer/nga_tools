from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from nga_tools.backup.archive import backup_thread, backup_thread_sub
from nga_tools.backup.floor_map import generate_floor_map_from_backup
from nga_tools.console import (
    BackupConfigsProgressDisplay,
    get_reporter,
    report_info,
    report_progress,
    report_warning,
    use_warning_log,
    use_reporter,
)
from nga_tools.backup.pdf import generate_pdf
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, optional_int, required_int
from nga_tools.commands.warning_log import warning_log_path
from nga_tools.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)


def backup_all(args: CommandArgs) -> None:
    configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        backup_thread(thread_tid, thread_aid)


def backup_sub(args: CommandArgs) -> None:
    configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        backup_thread_sub(thread_tid, thread_aid)


def _thread_config_label(thread_config: ThreadConfig) -> str:
    tid = thread_config_tid(thread_config)
    aid = thread_config_aid(thread_config)
    return (
        f"{thread_config_name(thread_config)} "
        f"(tid: {tid}, aid: {aid})"
    )


def _backup_single_thread_config(thread_config: ThreadConfig) -> None:
    backup_thread_sub(
        thread_config_tid(thread_config),
        thread_config_aid(thread_config),
    )


def _backup_thread_config_with_progress(
    *,
    index: int,
    total: int,
    thread_config: ThreadConfig,
    progress: BackupConfigsProgressDisplay,
) -> None:
    thread_label = _thread_config_label(thread_config)
    task_reporter = progress.start_thread(
        index=index,
        total=total,
        label=thread_label,
    )
    log_path = warning_log_path(
        thread_config_tid(thread_config),
        thread_config_aid(thread_config),
    )
    with use_reporter(task_reporter), use_warning_log(log_path):
        report_progress("正在增量备份")
        try:
            _backup_single_thread_config(thread_config)
        except Exception as error:
            report_warning(f"备份失败：{error}")
            progress.finish_thread(task_reporter, status="失败")
            raise
        progress.finish_thread(task_reporter, status="完成")


def _backup_configs_sequential(
    thread_configs: list[ThreadConfig],
    progress: BackupConfigsProgressDisplay,
) -> list[tuple[ThreadConfig, Exception]]:
    failures: list[tuple[ThreadConfig, Exception]] = []
    total = len(thread_configs)
    for index, thread_config in enumerate(thread_configs, start=1):
        try:
            _backup_thread_config_with_progress(
                index=index,
                total=total,
                thread_config=thread_config,
                progress=progress,
            )
        except Exception as error:
            failures.append((thread_config, error))
            continue

    return failures


def _backup_configs_parallel(
    thread_configs: list[ThreadConfig],
    worker_count: int,
    progress: BackupConfigsProgressDisplay,
) -> list[tuple[ThreadConfig, Exception]]:
    failures: list[tuple[int, ThreadConfig, Exception]] = []
    total = len(thread_configs)
    with ThreadPoolExecutor(max_workers=min(worker_count, total)) as executor:
        future_context: dict[Future[None], tuple[int, ThreadConfig]] = {}
        for index, thread_config in enumerate(thread_configs, start=1):
            future = executor.submit(
                _backup_thread_config_with_progress,
                index=index,
                total=total,
                thread_config=thread_config,
                progress=progress,
            )
            future_context[future] = (index, thread_config)

        for future in as_completed(future_context):
            index, thread_config = future_context[future]
            try:
                future.result()
            except Exception as error:
                failures.append((index, thread_config, error))
                continue

    ordered_failures = [
        (thread_config, error)
        for _, thread_config, error in sorted(failures, key=lambda item: item[0])
    ]
    return ordered_failures


def _print_backup_configs_summary(
    total: int,
    failures: list[tuple[ThreadConfig, Exception]],
) -> None:
    success_count = total - len(failures)
    report_info(f"批量备份完成：成功{success_count}个，失败{len(failures)}个。")
    if failures:
        for thread_config, error in failures:
            report_info(f"失败：{_thread_config_label(thread_config)}：{error}")
        raise SystemExit(1)


def backup_configs(args: CommandArgs) -> None:
    app_config = configure_network_limits_from_args(args)

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        report_info("没有找到任何帖子配置。")
        return

    worker_arg = optional_int(args, "workers")
    worker_count = (
        app_config.backup_configs_workers if worker_arg is None else worker_arg
    )
    if worker_count <= 0:
        raise ValueError("workers必须大于0。")

    with BackupConfigsProgressDisplay(
        len(thread_configs),
        console=get_reporter().console,
    ) as progress:
        if worker_count == 1 or len(thread_configs) == 1:
            failures = _backup_configs_sequential(thread_configs, progress)
        else:
            failures = _backup_configs_parallel(
                thread_configs,
                worker_count,
                progress,
            )

    _print_backup_configs_summary(len(thread_configs), failures)


def backup_floors(args: CommandArgs) -> None:
    configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        generate_floor_map_from_backup(thread_tid, thread_aid)


def pdf_generate(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        generate_pdf(
            tid=thread_tid,
            aid=thread_aid,
            lou_per_pdf=required_int(args, "lou_per_pdf"),
            pdf_workers=optional_int(args, "pdf_workers"),
        )

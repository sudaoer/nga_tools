from __future__ import annotations

from pathlib import Path
from typing import Protocol

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.archive import backup_thread, backup_thread_sub
from nga_tools.backup.floor_map import generate_floor_map_from_backup
from nga_tools.config import get_config
from nga_tools.console import (
    report_info,
    report_warning,
    use_warning_log,
)
from nga_tools.backup.pdf import PdfRenderPool, generate_pdf
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.thread_batch import run_thread_config_batch
from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    required_int,
)
from nga_tools.core.paths import warning_log_path
from nga_tools.forum.thread_configs import (
    ThreadConfig,
    thread_config_aid,
    thread_config_tid,
)


class BackupFetchFunc(Protocol):
    def __call__(
        self,
        tid: int,
        aid: int | None,
        *,
        write_json: bool,
    ) -> None:
        raise NotImplementedError


def _batch_worker_count(args: CommandArgs, default_worker_count: int) -> int:
    worker_arg = optional_int(args, "workers")
    return default_worker_count if worker_arg is None else worker_arg


def _run_backup_fetch_batch(
    args: CommandArgs,
    *,
    backup_func: BackupFetchFunc,
    progress_text: str,
) -> None:
    app_config = configure_network_limits_from_args(args)
    write_json = optional_bool(args, "write_json")
    worker_count = _batch_worker_count(args, app_config.backup_configs_workers)

    def action(thread_config: ThreadConfig) -> None:
        tid = thread_config_tid(thread_config)
        aid = thread_config_aid(thread_config)
        backup_func(tid, aid, write_json=write_json)

    run_thread_config_batch(
        action=action,
        progress_text=progress_text,
        failure_text="备份失败",
        summary_name="备份",
        worker_count=worker_count,
    )


def backup_all(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        _run_backup_fetch_batch(
            args,
            backup_func=backup_thread,
            progress_text="正在完整备份",
        )
        return

    configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    write_json = optional_bool(args, "write_json")
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        backup_thread(thread_tid, thread_aid, write_json=write_json)


def backup_sub(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        _run_backup_fetch_batch(
            args,
            backup_func=backup_thread_sub,
            progress_text="正在增量备份",
        )
        return

    configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    write_json = optional_bool(args, "write_json")
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        backup_thread_sub(thread_tid, thread_aid, write_json=write_json)


def backup_configs(args: CommandArgs) -> None:
    _run_backup_fetch_batch(
        args,
        backup_func=backup_thread_sub,
        progress_text="正在增量备份",
    )


def backup_floors(args: CommandArgs) -> None:
    app_config = configure_network_limits_from_args(args)
    if optional_bool(args, "all_threads"):
        worker_count = _batch_worker_count(args, app_config.backup_configs_workers)

        def action(thread_config: ThreadConfig) -> None:
            generate_floor_map_from_backup(
                thread_config_tid(thread_config),
                thread_config_aid(thread_config),
            )

        run_thread_config_batch(
            action=action,
            progress_text="正在生成楼层映射",
            failure_text="楼层映射失败",
            summary_name="楼层映射",
            worker_count=worker_count,
        )
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        generate_floor_map_from_backup(thread_tid, thread_aid)


def _migrate_store_for_thread_folder(thread_folder: Path) -> None:
    result = ThreadArchiveStore(thread_folder).migrate_json_pages()
    report_info(
        f"迁移完成：{thread_folder}，"
        f"读取JSON页{result.page_files}个，"
        f"新增页快照{result.page_snapshots_inserted}个，"
        f"新增帖子版本{result.post_versions_inserted}个，"
        f"记录楼层观测{result.post_observations}条。"
    )


def _backup_store_candidate_folders(output_dir: Path) -> list[Path]:
    if not output_dir.exists():
        return []
    return sorted(
        folder
        for folder in output_dir.iterdir()
        if folder.is_dir() and (folder / "json").is_dir()
    )


def backup_migrate_store(args: CommandArgs) -> None:
    if optional_bool(args, "all"):
        failures: list[tuple[Path, Exception]] = []
        folders = _backup_store_candidate_folders(Path(get_config().output_dir))
        if not folders:
            report_info("没有找到可迁移的备份目录。")
            return

        for folder in folders:
            try:
                _migrate_store_for_thread_folder(folder)
            except Exception as error:
                failures.append((folder, error))
                report_warning(f"迁移失败：{folder}：{error}")

        report_info(
            f"批量迁移完成：成功{len(folders) - len(failures)}个，"
            f"失败{len(failures)}个。"
        )
        if failures:
            raise SystemExit(1)
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    log_path = warning_log_path(thread_tid, thread_aid)
    thread_folder = log_path.parent
    with use_warning_log(log_path):
        _migrate_store_for_thread_folder(thread_folder)


def pdf_generate(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        worker_count = _batch_worker_count(
            args,
            get_config().backup_configs_workers,
        )
        lou_per_pdf = required_int(args, "lou_per_pdf")
        pdf_workers = optional_int(args, "pdf_workers")

        with PdfRenderPool(pdf_workers) as pdf_renderer:
            def action(thread_config: ThreadConfig) -> None:
                generate_pdf(
                    tid=thread_config_tid(thread_config),
                    aid=thread_config_aid(thread_config),
                    lou_per_pdf=lou_per_pdf,
                    pdf_workers=pdf_workers,
                    pdf_renderer=pdf_renderer,
                )

            run_thread_config_batch(
                action=action,
                progress_text="正在生成PDF",
                failure_text="PDF生成失败",
                summary_name="PDF生成",
                worker_count=worker_count,
            )
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    with use_warning_log(warning_log_path(thread_tid, thread_aid)):
        generate_pdf(
            tid=thread_tid,
            aid=thread_aid,
            lou_per_pdf=required_int(args, "lou_per_pdf"),
            pdf_workers=optional_int(args, "pdf_workers"),
        )

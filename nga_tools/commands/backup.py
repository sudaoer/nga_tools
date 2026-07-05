from __future__ import annotations

from nga_tools.backup.archive import backup_thread, backup_thread_sub
from nga_tools.backup.floor_map import generate_floor_map_from_backup
from nga_tools.backup.pdf import generate_pdf
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, optional_int, required_int
from nga_tools.thread_configs import NGAThreadConfigs, ThreadConfig


def backup_all(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    backup_thread(thread_tid, thread_aid)


def backup_sub(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    backup_thread_sub(thread_tid, thread_aid)


def backup_configs(args: CommandArgs) -> None:
    del args

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        print("没有找到任何帖子配置。")
        return

    failures: list[tuple[ThreadConfig, Exception]] = []
    total = len(thread_configs)
    for index, thread_config in enumerate(thread_configs, start=1):
        thread_name = thread_config["thread_name"]
        tid = thread_config["tid"]
        aid = thread_config.get("aid")
        print(
            f"[{index}/{total}] 正在增量备份："
            f"{thread_name} (tid: {tid}, aid: {aid})"
        )
        try:
            backup_thread_sub(tid, aid)
        except Exception as error:
            failures.append((thread_config, error))
            print(
                f"[{index}/{total}] 备份失败："
                f"{thread_name} (tid: {tid}, aid: {aid})：{error}"
            )
            continue
        print(f"[{index}/{total}] 备份完成：{thread_name}")

    success_count = total - len(failures)
    print(f"批量备份完成：成功{success_count}个，失败{len(failures)}个。")
    if failures:
        for thread_config, error in failures:
            print(
                f"失败：{thread_config['thread_name']} "
                f"(tid: {thread_config['tid']}, aid: {thread_config.get('aid')})："
                f"{error}"
            )
        raise SystemExit(1)


def backup_floors(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    generate_floor_map_from_backup(thread_tid, thread_aid)


def pdf_generate(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    generate_pdf(
        tid=thread_tid,
        aid=thread_aid,
        lou_per_pdf=required_int(args, "lou_per_pdf"),
        pdf_workers=optional_int(args, "pdf_workers"),
    )

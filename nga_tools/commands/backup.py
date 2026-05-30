from __future__ import annotations

from nga_tools import utils
from nga_tools.backup.archive import backup_thread
from nga_tools.backup.floor_map import generate_floor_map_from_backup
from nga_tools.backup.pdf import generate_pdf
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, optional_int, required_int


def backup_all(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    backup_thread(thread_tid, thread_aid)


def backup_sub(args: CommandArgs) -> None:
    resolve_command_thread_target(args)
    utils.TODO("实现备份帖子本地没有部分的功能")


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

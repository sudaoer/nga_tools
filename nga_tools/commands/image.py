from __future__ import annotations

from nga_tools.backup.images import (
    verify_all_downloaded_images,
    verify_downloaded_images,
)
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, optional_int, optional_str


def image_verify(args: CommandArgs) -> None:
    name = optional_str(args, "name")
    tid = optional_int(args, "tid")
    aid = optional_int(args, "aid")
    if not name and tid is None:
        if aid is not None:
            raise ValueError("--aid必须与--tid或--name一起使用。")
        verify_all_downloaded_images()
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    verify_downloaded_images(thread_tid, thread_aid)

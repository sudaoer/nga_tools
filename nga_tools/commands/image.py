from __future__ import annotations

from nga_tools.backup.images import verify_downloaded_images
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs


def image_verify(args: CommandArgs) -> None:
    thread_tid, thread_aid = resolve_command_thread_target(args)
    verify_downloaded_images(thread_tid, thread_aid)

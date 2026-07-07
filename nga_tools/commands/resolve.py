from __future__ import annotations

from typing import Optional

from nga_tools.commands.types import CommandArgs, optional_int, optional_str
from nga_tools.forum.thread_configs import resolve_thread_target


def resolve_command_thread_target(args: CommandArgs) -> tuple[int, Optional[int]]:
    return resolve_thread_target(
        name=optional_str(args, "name"),
        tid=optional_int(args, "tid"),
        aid=optional_int(args, "aid"),
    )

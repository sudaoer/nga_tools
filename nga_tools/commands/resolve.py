from __future__ import annotations

from nga_tools.commands.types import CommandArgs, optional_int, optional_str
from nga_tools.thread_configs import resolve_thread_target


def resolve_command_thread_target(args: CommandArgs) -> tuple[int, int | None]:
    return resolve_thread_target(
        name=optional_str(args, "name"),
        tid=optional_int(args, "tid"),
        aid=optional_int(args, "aid"),
    )

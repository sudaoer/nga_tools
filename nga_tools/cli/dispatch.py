from __future__ import annotations

from typing import cast

from nga_tools.cli.parser import args_parse
from nga_tools.cli.schema import COMMANDS
from nga_tools.commands.types import CommandArgs
from nga_tools.console import use_command_warning_summary


def dispatch_command(args: CommandArgs) -> None:
    command = cast(str, args["command"])
    action = cast(str, args["action"])
    action_config = COMMANDS[command][action]
    handler = action_config["handler"]
    if action_config.get("child_warning_summary", False):
        handler(args)
        return
    with use_command_warning_summary():
        handler(args)


def main() -> None:
    args = args_parse()
    dispatch_command(args)

from __future__ import annotations

from typing import cast

from nga_tools.cli.parser import args_parse
from nga_tools.cli.schema import COMMANDS
from nga_tools.commands.types import CommandArgs


def dispatch_command(args: CommandArgs) -> None:
    command = cast(str, args["command"])
    action = cast(str, args["action"])
    action_config = COMMANDS[command][action]
    handler = action_config["handler"]
    handler(args)


def main() -> None:
    args = args_parse()
    dispatch_command(args)


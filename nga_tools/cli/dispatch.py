from __future__ import annotations

from pathlib import Path
from typing import cast

from nga_tools.cli.parser import args_parse
from nga_tools.cli.schema import COMMANDS
from nga_tools.commands.types import CommandArgs
from nga_tools.config import get_config
from nga_tools.console import use_command_warning_summary
from nga_tools.core.output_lock import use_output_root_lock


def dispatch_command(args: CommandArgs) -> None:
    command = cast(str, args["command"])
    action = cast(str, args["action"])
    action_config = COMMANDS[command][action]
    handler = action_config["handler"]

    def run_handler() -> None:
        if action_config.get("child_warning_summary", False):
            handler(args)
            return
        with use_command_warning_summary():
            handler(args)

    if action_config.get("output_root_lock", False):
        with use_output_root_lock(Path(get_config().output_dir)):
            run_handler()
        return
    run_handler()


def main() -> None:
    args = args_parse()
    dispatch_command(args)

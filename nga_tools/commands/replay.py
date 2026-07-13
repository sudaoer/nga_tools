from __future__ import annotations

from pathlib import Path

from nga_tools.commands.types import CommandArgs, optional_int, optional_str, required_str
from nga_tools.config import get_config
from nga_tools.replay.profile import load_replay_profile
from nga_tools.replay.runner import run_replay_backup
from nga_tools.replay.server import (
    DEFAULT_REPLAY_HOST,
    DEFAULT_REPLAY_PORT,
    serve_replay,
)


def replay_serve(args: CommandArgs) -> None:
    source_output = Path(required_str(args, "source_output"))
    profile_path = Path(required_str(args, "profile"))
    thread_config_arg = optional_str(args, "thread_config")
    thread_config_path = Path(
        get_config().thread_config_file
        if thread_config_arg is None
        else thread_config_arg
    )
    host = optional_str(args, "host") or DEFAULT_REPLAY_HOST
    port = optional_int(args, "port") or DEFAULT_REPLAY_PORT
    serve_replay(
        source_output=source_output,
        thread_config_path=thread_config_path,
        profile=load_replay_profile(profile_path),
        host=host,
        port=port,
    )


def replay_run(args: CommandArgs) -> None:
    run_replay_backup(args)

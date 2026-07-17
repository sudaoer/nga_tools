from __future__ import annotations

import argparse
import sys
from typing import Optional, cast

from nga_tools.cli.help import (
    format_action_help,
    format_command_help,
    format_global_help,
)
from nga_tools.cli.schema import ARG_DEFS, COMMANDS, HELP_FLAGS, all_actions
from nga_tools.commands.types import CommandArgs


def _arg_flag(arg_name: str) -> str:
    return ARG_DEFS[arg_name]["flags"][0]


def _format_arg_names(arg_names: list[str]) -> str:
    return ", ".join(_arg_flag(arg_name) for arg_name in arg_names)


def _provided_arg_names(argv: list[str]) -> set[str]:
    flag_to_name: dict[str, str] = {}
    for arg_name, arg_config in ARG_DEFS.items():
        for flag in arg_config["flags"]:
            flag_to_name[flag] = arg_name

    provided_args: set[str] = set()
    for token in argv:
        option_name = token.split("=", 1)[0]
        if option_name in flag_to_name:
            provided_args.add(flag_to_name[option_name])
    return provided_args


def _has_arg_value(value: Optional[object]) -> bool:
    if isinstance(value, bool):
        return value
    return value is not None and value != ""


def _print_help_and_exit(raw_args: list[str]) -> None:
    if not any(token in HELP_FLAGS for token in raw_args):
        return

    positional_args = [token for token in raw_args if token not in HELP_FLAGS]
    if not positional_args:
        print(format_global_help())
        raise SystemExit(0)

    command = positional_args[0]
    if command not in COMMANDS:
        print(format_global_help())
        raise SystemExit(2)

    if len(positional_args) == 1:
        print(format_command_help(command))
        raise SystemExit(0)

    action = positional_args[1]
    if action not in COMMANDS[command]:
        available_actions = ", ".join(COMMANDS[command])
        print(
            f"未知操作组合：{command} {action}。"
            f"{command}支持的操作：{available_actions}",
            file=sys.stderr,
        )
        print("", file=sys.stderr)
        print(format_command_help(command), file=sys.stderr)
        raise SystemExit(2)

    print(format_action_help(command, action))
    raise SystemExit(0)


def _validate_args(
    parser: argparse.ArgumentParser,
    args: CommandArgs,
    provided_args: set[str],
) -> None:
    command = cast(str, args["command"])
    action = cast(str, args["action"])
    action_configs = COMMANDS[command]
    if action not in action_configs:
        available_actions = ", ".join(sorted(action_configs))
        parser.error(
            f"未知操作组合：{command} {action}。"
            f"{command}支持的操作：{available_actions}"
        )

    action_config = action_configs[action]
    allowed_args = set(action_config["args"])
    unused_args = sorted(provided_args - allowed_args)
    if unused_args:
        parser.error(
            f"{command} {action} 不支持参数："
            + _format_arg_names(unused_args)
        )

    if command == "forum" and action == "sync" and not args.get("full_postdate"):
        postdate_only_args = sorted(
            provided_args & {"fid", "page_delay_seconds", "refresh", "start_page"}
        )
        if postdate_only_args:
            parser.error(
                "以下参数仅支持与--full-postdate一起使用："
                + _format_arg_names(postdate_only_args)
            )

    if (
        command == "forum"
        and action == "sync"
        and args.get("full_postdate")
        and "start_page" in provided_args
        and not args.get("refresh")
    ):
        parser.error("--start-page仅支持与--full-postdate --refresh一起使用。")

    if args.get("all_threads"):
        target_args = sorted(provided_args & {"aid", "name", "tid"})
        if target_args:
            parser.error(
                "--all-threads不能与以下单帖参数一起使用："
                + _format_arg_names(target_args)
            )

    if (
        command == "backup"
        and action in ("migrate-store", "migrate-layout", "migrate-content")
        and args.get("all")
    ):
        target_args = sorted(provided_args & {"aid", "name", "tid"})
        if target_args:
            parser.error(
                "--all不能与以下单帖参数一起使用："
                + _format_arg_names(target_args)
            )

    if (
        command == "backup"
        and action in ("migrate-layout", "migrate-content")
        and args.get("rollback")
    ):
        target_args = sorted(provided_args & {"aid", "all", "name", "tid"})
        if target_args:
            parser.error(
                "--rollback不能与以下迁移目标参数一起使用："
                + _format_arg_names(target_args)
            )

    if command == "backup" and action == "migrate-content" and args.get(
        "rollback"
    ) and args.get("dry_run"):
        parser.error("--rollback不能与--dry-run一起使用。")

    for arg_name, default_value in action_config.get("defaults", {}).items():
        if args.get(arg_name) is None:
            args[arg_name] = default_value

    missing_args = [
        arg_name
        for arg_name in action_config.get("required", [])
        if not _has_arg_value(args.get(arg_name))
    ]
    if missing_args:
        parser.error("缺少必需参数：" + _format_arg_names(missing_args))

    required_any = action_config.get("required_any", [])
    if required_any and not any(
        _has_arg_value(args.get(name)) for name in required_any
    ):
        parser.error(
            "必须提供以下参数之一：" + _format_arg_names(required_any)
        )

    positive_args = action_config.get("positive", [])
    for arg_name in positive_args:
        value = args.get(arg_name)
        if isinstance(value, int) and value <= 0:
            parser.error(f"{_arg_flag(arg_name)}必须大于0。")


def args_parse(argv: Optional[list[str]] = None) -> CommandArgs:
    parser = argparse.ArgumentParser(description="NGA帖子备份器", add_help=False)
    raw_args = sys.argv[1:] if argv is None else argv
    _print_help_and_exit(raw_args)

    parser.add_argument(
        "command",
        choices=sorted(COMMANDS),
        help="要执行的命令",
    )
    parser.add_argument(
        "action",
        choices=all_actions(),
        help="要执行的操作",
    )

    for arg_config in ARG_DEFS.values():
        if "action" in arg_config:
            parser.add_argument(
                *arg_config["flags"],
                action=arg_config["action"],
                help=arg_config["help"],
            )
            continue

        arg_type = arg_config.get("type")
        if arg_type is None:
            raise RuntimeError(f"参数定义缺少type：{arg_config['flags']}")

        parser.add_argument(
            *arg_config["flags"],
            type=arg_type,
            metavar=arg_config.get("metavar"),
            help=arg_config["help"],
        )

    args = cast(CommandArgs, vars(parser.parse_args(raw_args)))
    _validate_args(parser, args, _provided_arg_names(raw_args))
    return args

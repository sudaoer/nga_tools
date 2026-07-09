from __future__ import annotations

from nga_tools.cli.schema import ARG_DEFS, COMMANDS, PROGRAM_USAGE, ActionConfig


def _arg_flags(arg_name: str) -> str:
    arg_config = ARG_DEFS[arg_name]
    return ", ".join(arg_config["flags"])


def _arg_flag(arg_name: str) -> str:
    return ARG_DEFS[arg_name]["flags"][0]


def _format_arg_help(arg_name: str, action_config: ActionConfig) -> str:
    arg_config = ARG_DEFS[arg_name]
    flags = _arg_flags(arg_name)
    metavar = arg_config.get("metavar")
    if metavar:
        flags = f"{flags} {metavar}"
    help_text = arg_config["help"]

    default_values = action_config.get("defaults", {})
    if arg_name in default_values:
        help_text += f"（默认：{default_values[arg_name]}）"

    if arg_name in action_config.get("positive", []):
        help_text += "，必须大于0"

    return f"  {flags:<28} {help_text}"


def _required_help(action_config: ActionConfig) -> list[str]:
    lines: list[str] = []
    required_args = action_config.get("required", [])
    required_any_args = action_config.get("required_any", [])
    if required_args:
        required = ", ".join(_arg_flag(arg_name) for arg_name in required_args)
        lines.append(f"  必须提供：{required}")
    if required_any_args:
        required_any = " 或 ".join(
            _arg_flag(arg_name) for arg_name in required_any_args
        )
        lines.append(f"  必须提供其中之一：{required_any}")
    if not lines:
        lines.append("  无")
    return lines


def _format_examples(examples: list[str]) -> list[str]:
    if not examples:
        return []
    lines = ["", "示例："]
    lines.extend(f"  {example}" for example in examples)
    return lines


def _format_notes(notes: list[str]) -> list[str]:
    if not notes:
        return []
    lines = ["", "说明："]
    lines.extend(f"  {note}" for note in notes)
    return lines


def format_global_help() -> str:
    lines = [
        "NGA帖子备份器",
        "",
        f"用法：{PROGRAM_USAGE} <command> <action> [options]",
        "",
        "命令：",
    ]
    for command, action_configs in COMMANDS.items():
        lines.append(f"  {command}")
        for action, action_config in action_configs.items():
            lines.append(f"    {action:<8} {action_config['summary']}")

    lines.extend(
        [
            "",
            "查看详情：",
            f"  {PROGRAM_USAGE} <command> --help",
            f"  {PROGRAM_USAGE} <command> <action> --help",
            "",
            "常用示例：",
            f"  {PROGRAM_USAGE} forum list --fid 784",
            f"  {PROGRAM_USAGE} forum sync",
            f"  {PROGRAM_USAGE} backup all --name 帖子名",
            f"  {PROGRAM_USAGE} backup sub --all-threads",
            f"  {PROGRAM_USAGE} backup pdf --name 帖子名 --pdf-workers 2",
            f"  {PROGRAM_USAGE} image verify",
            f"  {PROGRAM_USAGE} stats words --name 帖子名",
            f"  {PROGRAM_USAGE} web serve",
        ]
    )
    return "\n".join(lines)


def format_command_help(command: str) -> str:
    action_configs = COMMANDS[command]
    lines = [
        f"{command} 命令",
        "",
        f"用法：{PROGRAM_USAGE} {command} <action> [options]",
        "",
        "可用操作：",
    ]
    for action, action_config in action_configs.items():
        lines.append(f"  {action:<8} {action_config['summary']}")

    lines.extend(
        [
            "",
            "查看操作详情：",
            f"  {PROGRAM_USAGE} {command} <action> --help",
        ]
    )
    return "\n".join(lines)


def format_action_help(command: str, action: str) -> str:
    action_config = COMMANDS[command][action]
    lines = [
        f"{command} {action}",
        "",
        action_config["summary"],
        "",
        f"用法：{action_config['usage']}",
        "",
        "必需参数：",
        *_required_help(action_config),
    ]

    option_args = action_config.get("args", [])
    if option_args:
        lines.extend(["", "参数："])
        lines.extend(
            _format_arg_help(arg_name, action_config) for arg_name in option_args
        )
    else:
        lines.extend(["", "参数：", "  无"])

    lines.extend(_format_examples(action_config.get("examples", [])))
    lines.extend(_format_notes(action_config.get("notes", [])))
    return "\n".join(lines)

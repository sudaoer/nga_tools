from __future__ import annotations

import argparse
import sys
from typing import Literal, NotRequired, Optional, TypedDict, cast

from nga_tools.commands.backup import (
    backup_all,
    backup_configs,
    backup_floors,
    backup_sub,
    pdf_generate,
)
from nga_tools.commands.forum import handle_forum_list, handle_forum_sync
from nga_tools.commands.image import image_migrate, image_prune_links, image_verify
from nga_tools.commands.stats import stats_words
from nga_tools.commands.thread import handle_thread_add, handle_thread_list
from nga_tools.commands.types import CommandArgs, CommandHandler

PROGRAM_USAGE = "python main.py"
HELP_FLAGS = {"-h", "--help"}


class ArgDef(TypedDict):
    flags: tuple[str, ...]
    help: str
    type: NotRequired[type[str] | type[int]]
    metavar: NotRequired[str]
    action: NotRequired[Literal["store_true"]]


class ActionConfig(TypedDict):
    handler: CommandHandler
    summary: str
    usage: str
    examples: list[str]
    args: list[str]
    notes: NotRequired[list[str]]
    required: NotRequired[list[str]]
    required_any: NotRequired[list[str]]
    defaults: NotRequired[dict[str, int]]
    positive: NotRequired[list[str]]


ARG_DEFS: dict[str, ArgDef] = {
    "name": {
        "flags": ("--name",),
        "type": str,
        "metavar": "NAME",
        "help": "帖子名称",
    },
    "tid": {
        "flags": ("--tid",),
        "type": int,
        "metavar": "TID",
        "help": "帖子tid",
    },
    "aid": {
        "flags": ("--aid",),
        "type": int,
        "metavar": "AID",
        "help": "作者aid（可选）",
    },
    "fid": {
        "flags": ("--fid",),
        "type": int,
        "metavar": "FID",
        "help": "版面fid",
    },
    "description": {
        "flags": ("--description",),
        "type": str,
        "metavar": "TEXT",
        "help": "帖子描述（可选）",
    },
    "lou_per_pdf": {
        "flags": ("--lou_per_pdf",),
        "type": int,
        "metavar": "N",
        "help": "每个PDF包含的楼层数（仅pdf命令有效）",
    },
    "pdf_workers": {
        "flags": ("--pdf_workers",),
        "type": int,
        "metavar": "N",
        "help": "生成PDF时并行运行weasyprint的worker数量（仅pdf命令有效）",
    },
    "workers": {
        "flags": ("--workers",),
        "type": int,
        "metavar": "N",
        "help": "批量备份帖子时的并行worker数量",
    },
    "api_concurrency": {
        "flags": ("--api_concurrency",),
        "type": int,
        "metavar": "N",
        "help": "NGA API请求的全局并发上限",
    },
    "image_concurrency": {
        "flags": ("--image_concurrency",),
        "type": int,
        "metavar": "N",
        "help": "图片下载的全局并发上限",
    },
    "min_body_chars": {
        "flags": ("--min_body_chars",),
        "type": int,
        "metavar": "N",
        "help": "正文楼层判定阈值（中文+中文标点数）",
    },
    "pages": {
        "flags": ("--pages",),
        "type": int,
        "metavar": "N",
        "help": "扫描版面页数",
    },
    "watch_config": {
        "flags": ("--watch_config",),
        "type": str,
        "metavar": "PATH",
        "help": "版面监控规则JSON路径",
    },
    "full_postdate": {
        "flags": ("--full_postdate",),
        "action": "store_true",
        "help": "按主题发布时间倒序全版面扫描并写入单独清单",
    },
    "scan_output": {
        "flags": ("--scan_output",),
        "type": str,
        "metavar": "PATH",
        "help": "发布时间全版面扫描输出JSONL路径",
    },
    "page_delay_seconds": {
        "flags": ("--page_delay_seconds",),
        "type": int,
        "metavar": "N",
        "help": "发布时间全版面扫描每页请求后的等待秒数",
    },
    "start_page": {
        "flags": ("--start_page",),
        "type": int,
        "metavar": "N",
        "help": "发布时间全版面扫描起始页",
    },
}


COMMANDS: dict[str, dict[str, ActionConfig]] = {
    "thread": {
        "add": {
            "handler": handle_thread_add,
            "summary": "添加帖子配置",
            "usage": (
                f"{PROGRAM_USAGE} thread add --name NAME --tid TID "
                "[--aid AID] [--description TEXT]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} thread add --name 帖子名 --tid 12345678",
                f"{PROGRAM_USAGE} thread add --name 帖子名 --tid 12345678 --aid 987654",
            ],
            "args": ["name", "tid", "aid", "description"],
            "required": ["name", "tid"],
        },
        "list": {
            "handler": handle_thread_list,
            "summary": "列出已保存的帖子配置",
            "usage": f"{PROGRAM_USAGE} thread list",
            "examples": [f"{PROGRAM_USAGE} thread list"],
            "args": [],
        },
    },
    "forum": {
        "list": {
            "handler": handle_forum_list,
            "summary": "列出版面主题",
            "usage": (
                f"{PROGRAM_USAGE} forum list --fid FID [--pages N] "
                "[--api_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} forum list --fid 784",
                f"{PROGRAM_USAGE} forum list --fid 784 --pages 2",
            ],
            "notes": [
                "此命令只列出版面主题，不修改thread_configs.json。",
                "需要secrets.json中有可访问NGA App API的登录Cookie。",
            ],
            "args": ["fid", "pages", "api_concurrency"],
            "required": ["fid"],
            "defaults": {"pages": 1},
            "positive": ["pages", "api_concurrency"],
        },
        "sync": {
            "handler": handle_forum_sync,
            "summary": "根据版面监控规则保存匹配主题配置",
            "usage": (
                f"{PROGRAM_USAGE} forum sync [--watch_config PATH] "
                "[--api_concurrency N]\n"
                f"       {PROGRAM_USAGE} forum sync --full_postdate "
                "[--fid FID] [--scan_output PATH] [--start_page N] "
                "[--page_delay_seconds N] [--api_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} forum sync",
                f"{PROGRAM_USAGE} forum sync --watch_config forum_watch_configs.json",
                f"{PROGRAM_USAGE} forum sync --full_postdate --fid 784",
                (
                    f"{PROGRAM_USAGE} forum sync --full_postdate --fid 784 "
                    "--page_delay_seconds 5"
                ),
                (
                    f"{PROGRAM_USAGE} forum sync --full_postdate --fid 784 "
                    "--start_page 544"
                ),
            ],
            "notes": [
                "默认读取forum_watch_configs.json。",
                "匹配主题会写入thread_configs.json，aid使用主题楼主authorid。",
                "此命令只保存配置，不自动下载帖子内容。",
                "--full_postdate模式只写主题清单，不修改thread_configs.json，也不请求单贴API。",
            ],
            "args": [
                "watch_config",
                "fid",
                "full_postdate",
                "scan_output",
                "start_page",
                "page_delay_seconds",
                "api_concurrency",
            ],
            "defaults": {"page_delay_seconds": 3},
            "positive": ["start_page", "page_delay_seconds", "api_concurrency"],
        },
    },
    "backup": {
        "all": {
            "handler": backup_all,
            "summary": "抓取帖子内容并下载图片",
            "usage": (
                f"{PROGRAM_USAGE} backup all (--name NAME | --tid TID) [--aid AID] "
                "[--api_concurrency N] [--image_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup all --name 帖子名",
                f"{PROGRAM_USAGE} backup all --tid 12345678 --aid 987654",
            ],
            "args": ["name", "tid", "aid", "api_concurrency", "image_concurrency"],
            "required_any": ["name", "tid"],
            "positive": ["api_concurrency", "image_concurrency"],
        },
        "sub": {
            "handler": backup_sub,
            "summary": "增量补充本地缺失内容和远端新增内容",
            "usage": (
                f"{PROGRAM_USAGE} backup sub (--name NAME | --tid TID) [--aid AID] "
                "[--api_concurrency N] [--image_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup sub --name 帖子名",
                f"{PROGRAM_USAGE} backup sub --tid 12345678 --aid 987654",
            ],
            "notes": [
                "此命令会补抓缺失JSON页，并刷新本地尾页到远端最后一页。",
                "随后会补齐缺失或新增的HTML、html_modified和图片文件。",
                "author-only备份会增量刷新floor_map.json。",
            ],
            "args": ["name", "tid", "aid", "api_concurrency", "image_concurrency"],
            "required_any": ["name", "tid"],
            "positive": ["api_concurrency", "image_concurrency"],
        },
        "configs": {
            "handler": backup_configs,
            "summary": "增量备份thread_configs.json中的所有帖子",
            "usage": (
                f"{PROGRAM_USAGE} backup configs [--workers N] "
                "[--api_concurrency N] [--image_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup configs",
                f"{PROGRAM_USAGE} backup configs --workers 4",
            ],
            "notes": [
                "此命令按thread_configs.json中的ThreadList批量执行增量备份。",
                "不会修改thread_configs.json，也不会生成PDF。",
                "单个帖子失败时会继续处理后续配置，最后以非零退出码报告失败。",
            ],
            "args": ["workers", "api_concurrency", "image_concurrency"],
            "positive": ["workers", "api_concurrency", "image_concurrency"],
        },
        "floors": {
            "handler": backup_floors,
            "summary": "根据已有备份生成只看作者楼层到原帖楼层的映射",
            "usage": (
                f"{PROGRAM_USAGE} backup floors (--name NAME | --tid TID) [--aid AID] "
                "[--api_concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup floors --name 帖子名",
                f"{PROGRAM_USAGE} backup floors --tid 12345678 --aid 987654",
            ],
            "notes": [
                "此命令会读取已有json备份并联网扫描原帖，增量刷新floor_map.json。",
                "author-only备份生成PDF前必须先有floor_map.json。",
                "缺失楼无法唯一确定原楼层时，会在floor_map.json中记录候选原楼层。",
            ],
            "args": ["name", "tid", "aid", "api_concurrency"],
            "required_any": ["name", "tid"],
            "positive": ["api_concurrency"],
        },
        "pdf": {
            "handler": pdf_generate,
            "summary": "根据已备份的HTML和图片生成PDF",
            "usage": (
                f"{PROGRAM_USAGE} backup pdf (--name NAME | --tid TID) [--aid AID] "
                "[--lou_per_pdf N] [--pdf_workers N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup pdf --name 帖子名",
                f"{PROGRAM_USAGE} backup pdf --name 帖子名 --lou_per_pdf 100 --pdf_workers 2",
            ],
            "notes": [
                "author-only备份会读取floor_map.json，同时显示只看作者楼层和原帖楼层。",
                f"如缺少floor_map.json，先运行 {PROGRAM_USAGE} backup floors --name 帖子名。",
                "Overlay：创建 <output_dir>/<tid>_<aid>/overlay/post_<楼层>.html "
                "可在生成PDF时覆盖对应楼层。",
                "Overlay只影响PDF生成，不会改写json、html或html_modified备份内容。",
            ],
            "args": ["name", "tid", "aid", "lou_per_pdf", "pdf_workers"],
            "required_any": ["name", "tid"],
            "defaults": {"lou_per_pdf": 200},
            "positive": ["lou_per_pdf", "pdf_workers"],
        },
    },
    "image": {
        "verify": {
            "handler": image_verify,
            "summary": "校验已下载图片，删除损坏文件",
            "usage": f"{PROGRAM_USAGE} image verify [(--name NAME | --tid TID) [--aid AID]]",
            "examples": [
                f"{PROGRAM_USAGE} image verify",
                f"{PROGRAM_USAGE} image verify --name 帖子名",
            ],
            "notes": [
                "不提供参数时会校验output_dir/images_unique中的全局图片。",
                "提供帖子参数时会校验该帖html_modified引用到的图片文件。",
            ],
            "args": ["name", "tid", "aid"],
        },
        "migrate": {
            "handler": image_migrate,
            "summary": "迁移旧图片软链接为SQLite映射",
            "usage": f"{PROGRAM_USAGE} image migrate",
            "examples": [f"{PROGRAM_USAGE} image migrate"],
            "notes": [
                "会从output_dir/images旧软链接生成output_dir/image_index.sqlite3。",
                "会把html_modified中的旧images引用改写为images_unique路径。",
                "不会删除旧软链接目录；确认后可运行image prune-links清理。",
            ],
            "args": [],
        },
        "prune-links": {
            "handler": image_prune_links,
            "summary": "删除已迁移的旧图片软链接目录",
            "usage": f"{PROGRAM_USAGE} image prune-links",
            "examples": [f"{PROGRAM_USAGE} image prune-links"],
            "notes": [
                "仅当html_modified不再引用output_dir/images时才会删除。",
                "如果旧目录内存在非软链接文件，会拒绝删除。",
            ],
            "args": [],
        },
    },
    "stats": {
        "words": {
            "handler": stats_words,
            "summary": "统计已有备份的正文中文字数",
            "usage": (
                f"{PROGRAM_USAGE} stats words (--name NAME | --tid TID) [--aid AID] "
                "[--min_body_chars N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} stats words --name 帖子名",
                f"{PROGRAM_USAGE} stats words --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} stats words --name 帖子名 --min_body_chars 80",
            ],
            "notes": [
                "此命令只读取本地json备份，不联网、不生成统计文件。",
                "会清洗图片、链接、HTML/BBCode、表情、用户引用、回复引用和骰子标记。",
                "默认只纳入清洗后中文+中文标点数达到120的正文楼层。",
            ],
            "args": ["name", "tid", "aid", "min_body_chars"],
            "required_any": ["name", "tid"],
            "defaults": {"min_body_chars": 120},
            "positive": ["min_body_chars"],
        },
    },
}


def _all_actions() -> list[str]:
    actions = {
        action for action_configs in COMMANDS.values() for action in action_configs
    }
    return sorted(actions)


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
    return value is not None and value != ""


def _arg_flags(arg_name: str) -> str:
    arg_config = ARG_DEFS[arg_name]
    return ", ".join(arg_config["flags"])


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
        required = ", ".join(f"--{arg_name}" for arg_name in required_args)
        lines.append(f"  必须提供：{required}")
    if required_any_args:
        required_any = " 或 ".join(f"--{arg_name}" for arg_name in required_any_args)
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
            f"  {PROGRAM_USAGE} thread list",
            f"  {PROGRAM_USAGE} forum list --fid 784",
            f"  {PROGRAM_USAGE} forum sync",
            f"  {PROGRAM_USAGE} backup all --name 帖子名",
            f"  {PROGRAM_USAGE} backup configs",
            f"  {PROGRAM_USAGE} backup pdf --name 帖子名 --pdf_workers 2",
            f"  {PROGRAM_USAGE} image migrate",
            f"  {PROGRAM_USAGE} stats words --name 帖子名",
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
            + ", ".join(f"--{arg_name}" for arg_name in unused_args)
        )

    if command == "forum" and action == "sync" and not args.get("full_postdate"):
        postdate_only_args = sorted(
            provided_args & {"fid", "page_delay_seconds", "scan_output", "start_page"}
        )
        if postdate_only_args:
            parser.error(
                "以下参数仅支持与--full_postdate一起使用："
                + ", ".join(f"--{name}" for name in postdate_only_args)
            )

    for arg_name, default_value in action_config.get("defaults", {}).items():
        if args.get(arg_name) is None:
            args[arg_name] = default_value

    missing_args = [
        arg_name
        for arg_name in action_config.get("required", [])
        if not _has_arg_value(args.get(arg_name))
    ]
    if missing_args:
        parser.error("缺少必需参数：" + ", ".join(f"--{name}" for name in missing_args))

    required_any = action_config.get("required_any", [])
    if required_any and not any(
        _has_arg_value(args.get(name)) for name in required_any
    ):
        parser.error(
            "必须提供以下参数之一：" + ", ".join(f"--{name}" for name in required_any)
        )

    positive_args = action_config.get("positive", [])
    for arg_name in positive_args:
        value = args.get(arg_name)
        if isinstance(value, int) and value <= 0:
            parser.error(f"--{arg_name}必须大于0。")


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
        choices=_all_actions(),
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


def dispatch_command(args: CommandArgs) -> None:
    command = cast(str, args["command"])
    action = cast(str, args["action"])
    action_config = COMMANDS[command][action]
    handler = action_config["handler"]
    handler(args)


def main() -> None:
    args = args_parse()
    dispatch_command(args)

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from nga_tools.commands.backup import (
    backup_all,
    backup_configs,
    backup_floors,
    backup_migrate_store,
    backup_sub,
    pdf_generate,
)
from nga_tools.commands.forum import handle_forum_list, handle_forum_sync
from nga_tools.commands.image import image_migrate, image_prune_links, image_verify
from nga_tools.commands.stats import stats_words
from nga_tools.commands.types import CommandHandler
from nga_tools.commands.web import web_serve
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR

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
    defaults: NotRequired[dict[str, object]]
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
        "flags": ("--lou-per-pdf", "--lou_per_pdf"),
        "type": int,
        "metavar": "N",
        "help": "每个PDF包含的楼层数（仅pdf命令有效）",
    },
    "pdf_workers": {
        "flags": ("--pdf-workers", "--pdf_workers"),
        "type": int,
        "metavar": "N",
        "help": "整个pdf命令中并行运行weasyprint的全局worker数量",
    },
    "workers": {
        "flags": ("--workers",),
        "type": int,
        "metavar": "N",
        "help": "批量处理帖子时的并行worker数量",
    },
    "api_concurrency": {
        "flags": ("--api-concurrency", "--api_concurrency"),
        "type": int,
        "metavar": "N",
        "help": "NGA API请求的全局并发上限",
    },
    "image_concurrency": {
        "flags": ("--image-concurrency", "--image_concurrency"),
        "type": int,
        "metavar": "N",
        "help": "图片下载的全局并发上限",
    },
    "min_body_chars": {
        "flags": ("--min-body-chars", "--min_body_chars"),
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
        "flags": ("--watch-config", "--watch_config"),
        "type": str,
        "metavar": "PATH",
        "help": "版面监控规则JSON路径",
    },
    "full_postdate": {
        "flags": ("--full-postdate", "--full_postdate"),
        "action": "store_true",
        "help": "按主题发布时间倒序扫描版面主题并写入数据库",
    },
    "refresh": {
        "flags": ("--refresh",),
        "action": "store_true",
        "help": "刷新已有数据库数据，不因已有tid提前停止",
    },
    "page_delay_seconds": {
        "flags": ("--page-delay-seconds", "--page_delay_seconds"),
        "type": int,
        "metavar": "N",
        "help": "发布时间全版面扫描每页请求后的等待秒数",
    },
    "start_page": {
        "flags": ("--start-page", "--start_page"),
        "type": int,
        "metavar": "N",
        "help": "发布时间全版面扫描起始页",
    },
    "all": {
        "flags": ("--all",),
        "action": "store_true",
        "help": "处理所有已有备份目录",
    },
    "all_threads": {
        "flags": ("--all-threads", "--all_threads"),
        "action": "store_true",
        "help": "处理thread_configs.json中的所有帖子配置",
    },
    "write_json": {
        "flags": ("--write-json", "--write_json"),
        "action": "store_true",
        "help": "备份时额外输出json/page_*.json最近响应缓存",
    },
    "host": {
        "flags": ("--host",),
        "type": str,
        "metavar": "HOST",
        "help": "Web服务监听地址",
    },
    "port": {
        "flags": ("--port",),
        "type": int,
        "metavar": "PORT",
        "help": "Web服务监听端口",
    },
    "static_dir": {
        "flags": ("--static-dir", "--static_dir"),
        "type": str,
        "metavar": "PATH",
        "help": "前端dist目录",
    },
}


COMMANDS: dict[str, dict[str, ActionConfig]] = {
    "forum": {
        "list": {
            "handler": handle_forum_list,
            "summary": "列出版面主题",
            "usage": (
                f"{PROGRAM_USAGE} forum list --fid FID [--pages N] "
                "[--api-concurrency N]"
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
                f"{PROGRAM_USAGE} forum sync [--watch-config PATH] "
                "[--api-concurrency N]\n"
                f"       {PROGRAM_USAGE} forum sync --full-postdate "
                "[--fid FID] [--refresh [--start-page N]] "
                "[--page-delay-seconds N] [--api-concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} forum sync",
                f"{PROGRAM_USAGE} forum sync --watch-config forum_watch_configs.json",
                f"{PROGRAM_USAGE} forum sync --full-postdate --fid 784",
                (
                    f"{PROGRAM_USAGE} forum sync --full-postdate --fid 784 "
                    "--page-delay-seconds 5"
                ),
                (
                    f"{PROGRAM_USAGE} forum sync --full-postdate --refresh --fid 784 "
                    "--start-page 544"
                ),
            ],
            "notes": [
                "默认读取forum_watch_configs.json。",
                "匹配主题会写入或刷新thread_configs.json，aid使用主题楼主authorid。",
                "同步会保存普通主题link，方便在编辑器里直接跳转原帖。",
                "此命令只保存配置，不自动下载帖子内容。",
                "pages只限制远端默认排序抓取页数，筛查从主题数据库读取。",
                "抓到的版面主题列表会写入output_dir/forum_threads.sqlite3。",
                "--full-postdate默认遇到数据库已有tid后停止；--refresh会刷新到远端末页。",
            ],
            "args": [
                "watch_config",
                "fid",
                "full_postdate",
                "refresh",
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
                f"{PROGRAM_USAGE} backup all "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--api-concurrency N] [--image-concurrency N] "
                "[--write-json]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup all --name 帖子名",
                f"{PROGRAM_USAGE} backup all --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} backup all --all-threads",
            ],
            "args": [
                "name",
                "tid",
                "aid",
                "all_threads",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "write_json",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": ["workers", "api_concurrency", "image_concurrency"],
        },
        "sub": {
            "handler": backup_sub,
            "summary": "增量补充本地缺失内容和远端新增内容",
            "usage": (
                f"{PROGRAM_USAGE} backup sub "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--api-concurrency N] [--image-concurrency N] "
                "[--write-json]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup sub --name 帖子名",
                f"{PROGRAM_USAGE} backup sub --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} backup sub --all-threads",
            ],
            "notes": [
                "此命令会补抓缺失页，并刷新本地尾页到远端最后一页。",
                "默认只写archive.sqlite3；加--write-json才输出json/page_*.json。",
                "随后会补齐缺失或新增的html_modified和图片文件。",
                "author-only备份会增量刷新floor_map.json。",
                "--all-threads会按thread_configs.json中的ThreadList批量执行增量备份。",
            ],
            "args": [
                "name",
                "tid",
                "aid",
                "all_threads",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "write_json",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": ["workers", "api_concurrency", "image_concurrency"],
        },
        "configs": {
            "handler": backup_configs,
            "summary": "兼容入口：增量备份thread_configs.json中的所有帖子",
            "usage": (
                f"{PROGRAM_USAGE} backup configs [--workers N] "
                "[--api-concurrency N] [--image-concurrency N] [--write-json]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup configs",
                f"{PROGRAM_USAGE} backup configs --workers 4",
            ],
            "notes": [
                "此命令是兼容入口，推荐使用 backup sub --all-threads。",
                "此命令按thread_configs.json中的ThreadList批量执行增量备份。",
                "默认只写archive.sqlite3；加--write-json才输出json/page_*.json。",
                "不会修改thread_configs.json，也不会生成PDF。",
                "单个帖子失败时会继续处理后续配置，最后以非零退出码报告失败。",
            ],
            "args": ["workers", "api_concurrency", "image_concurrency", "write_json"],
            "positive": ["workers", "api_concurrency", "image_concurrency"],
        },
        "floors": {
            "handler": backup_floors,
            "summary": "根据已有备份生成只看作者楼层到原帖楼层的映射",
            "usage": (
                f"{PROGRAM_USAGE} backup floors "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--api-concurrency N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup floors --name 帖子名",
                f"{PROGRAM_USAGE} backup floors --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} backup floors --all-threads",
            ],
            "notes": [
                "此命令会读取archive.sqlite3并联网扫描原帖，增量刷新floor_map.json。",
                "author-only备份生成PDF前必须先有floor_map.json。",
                "缺失楼无法唯一确定原楼层时，会在floor_map.json中记录候选原楼层。",
            ],
            "args": ["name", "tid", "aid", "all_threads", "workers", "api_concurrency"],
            "required_any": ["name", "tid", "all_threads"],
            "positive": ["workers", "api_concurrency"],
        },
        "migrate-store": {
            "handler": backup_migrate_store,
            "summary": "把旧分页JSON迁移到每帖SQLite存储",
            "usage": (
                f"{PROGRAM_USAGE} backup migrate-store "
                "((--name NAME | --tid TID [--aid AID]) | --all)"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup migrate-store --name 帖子名",
                f"{PROGRAM_USAGE} backup migrate-store --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} backup migrate-store --all",
            ],
            "notes": [
                "每个帖子目录会生成或更新自己的archive.sqlite3。",
                "迁移只读取旧json/page_*.json，不删除或改写旧JSON。",
                "--all会扫描output_dir下已有备份目录。",
            ],
            "args": ["name", "tid", "aid", "all"],
            "required_any": ["name", "tid", "all"],
        },
        "pdf": {
            "handler": pdf_generate,
            "summary": "根据已备份的HTML和图片生成PDF",
            "usage": (
                f"{PROGRAM_USAGE} backup pdf "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--lou-per-pdf N] [--pdf-workers N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup pdf --name 帖子名",
                f"{PROGRAM_USAGE} backup pdf --name 帖子名 --lou-per-pdf 100 --pdf-workers 2",
                f"{PROGRAM_USAGE} backup pdf --all-threads",
            ],
            "notes": [
                "author-only备份会读取floor_map.json，同时显示只看作者楼层和原帖楼层。",
                f"如缺少floor_map.json，先运行 {PROGRAM_USAGE} backup floors --name 帖子名。",
                "--all-threads会按thread_configs.json中的ThreadList批量生成PDF。",
                "--workers控制并行处理的帖子数，--pdf-workers控制全命令共享的"
                "WeasyPrint并发数。",
                "网页管理页保存的BBCode overlay会写入post_overlays.json，"
                "并刷新对应html_modified，普通查看和PDF都会使用。",
                "旧HTML overlay：<output_dir>/<tid>_<aid>/overlay/post_<楼层>.html "
                "仅作为legacy PDF覆盖方式保留；同楼层存在BBCode overlay时会被忽略。",
            ],
            "args": [
                "name",
                "tid",
                "aid",
                "all_threads",
                "workers",
                "lou_per_pdf",
                "pdf_workers",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "defaults": {"lou_per_pdf": 200},
            "positive": ["workers", "lou_per_pdf", "pdf_workers"],
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
            "summary": "迁移旧图片目录为SQLite映射",
            "usage": f"{PROGRAM_USAGE} image migrate",
            "examples": [f"{PROGRAM_USAGE} image migrate"],
            "notes": [
                "会从output_dir/images旧图片条目生成output_dir/image_index.sqlite3。",
                "会把html_modified中的旧images引用改写为images_unique路径。",
                "不会删除旧图片目录；确认后可运行image prune-links清理。",
            ],
            "args": [],
        },
        "prune-links": {
            "handler": image_prune_links,
            "summary": "删除已迁移的旧图片目录",
            "usage": f"{PROGRAM_USAGE} image prune-links",
            "examples": [f"{PROGRAM_USAGE} image prune-links"],
            "notes": [
                "仅当html_modified不再引用output_dir/images时才会删除。",
                "如果旧目录内存在无法识别的文件，会拒绝删除。",
            ],
            "args": [],
        },
    },
    "stats": {
        "words": {
            "handler": stats_words,
            "summary": "统计已有备份的正文中文字数",
            "usage": (
                f"{PROGRAM_USAGE} stats words "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--min-body-chars N]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} stats words --name 帖子名",
                f"{PROGRAM_USAGE} stats words --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} stats words --name 帖子名 --min-body-chars 80",
                f"{PROGRAM_USAGE} stats words --all-threads",
            ],
            "notes": [
                "此命令读取本地archive.sqlite3，不联网；会把字数字段写回归档，"
                "但不生成独立统计文件。",
                "会清洗图片、链接、HTML/BBCode、表情、用户引用、回复引用和骰子标记。",
                "默认只纳入清洗后中文+中文标点数达到120的正文楼层。",
            ],
            "args": ["name", "tid", "aid", "all_threads", "workers", "min_body_chars"],
            "required_any": ["name", "tid", "all_threads"],
            "defaults": {"min_body_chars": 120},
            "positive": ["workers", "min_body_chars"],
        },
    },
    "web": {
        "serve": {
            "handler": web_serve,
            "summary": "启动本地只读Web查看器",
            "usage": (
                f"{PROGRAM_USAGE} web serve "
                "[--host HOST] [--port PORT] [--static-dir PATH]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} web serve",
                f"{PROGRAM_USAGE} web serve --port {DEFAULT_WEB_PORT}",
                f"{PROGRAM_USAGE} web serve --host 0.0.0.0",
            ],
            "notes": [
                "此命令只读取本地备份，不联网、不修改备份内容。",
                "默认只监听本机地址，避免把本地备份暴露到局域网。",
                "线程列表会显示已存盘的正文字数，并支持按正文字数排序。",
                "只支持当前archive.sqlite3和html_modified备份；旧JSON请先迁移。",
            ],
            "args": ["host", "port", "static_dir"],
            "defaults": {
                "host": DEFAULT_WEB_HOST,
                "port": DEFAULT_WEB_PORT,
                "static_dir": DEFAULT_WEB_STATIC_DIR,
            },
            "positive": ["port"],
        },
    },
}


def all_actions() -> list[str]:
    actions = {
        action for action_configs in COMMANDS.values() for action in action_configs
    }
    return sorted(actions)

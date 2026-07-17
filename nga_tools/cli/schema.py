from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from nga_tools.commands.backup import (
    backup_all,
    backup_sub,
    pdf_generate,
)
from nga_tools.commands.ankebak import backup_auto
from nga_tools.commands.forum import handle_forum_list, handle_forum_sync
from nga_tools.commands.image import image_add, image_verify
from nga_tools.commands.replay import replay_run, replay_serve, replay_test
from nga_tools.commands.types import CommandHandler
from nga_tools.commands.web import web_serve
from nga_tools.replay.server import DEFAULT_REPLAY_HOST, DEFAULT_REPLAY_PORT
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
    child_warning_summary: NotRequired[bool]
    output_root_lock: NotRequired[bool]


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
    "url": {
        "flags": ("--url",),
        "type": str,
        "metavar": "URL",
        "help": "完整NGA图片URL",
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
    "audio_concurrency": {
        "flags": ("--audio-concurrency", "--audio_concurrency"),
        "type": int,
        "metavar": "N",
        "help": "帖子音频下载的全局并发上限",
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
    "all_threads": {
        "flags": ("--all-threads", "--all_threads"),
        "action": "store_true",
        "help": "处理thread_configs.json中的所有帖子配置",
    },
    "write_json": {
        "flags": ("--write-json", "--write_json"),
        "action": "store_true",
        "help": "备份时额外输出debug_json/page_*.json方便查看原始响应",
    },
    "force_processing": {
        "flags": ("--force-processing", "--force_processing"),
        "action": "store_true",
        "help": "忽略线程级处理状态并重新执行楼层、图片和音频派生处理",
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
        "help": "本地服务监听端口",
    },
    "static_dir": {
        "flags": ("--static-dir", "--static_dir"),
        "type": str,
        "metavar": "PATH",
        "help": "前端dist目录",
    },
    "source_output": {
        "flags": ("--source-output",),
        "type": str,
        "metavar": "PATH",
        "help": "只读重放语料所在的output目录",
    },
    "target_output": {
        "flags": ("--target-output",),
        "type": str,
        "metavar": "PATH",
        "help": "重放备份写入的独立output目录",
    },
    "server_url": {
        "flags": ("--server-url",),
        "type": str,
        "metavar": "URL",
        "help": "已启动的本地重放服务origin",
    },
    "initial_state": {
        "flags": ("--initial-state",),
        "type": str,
        "metavar": "STATE",
        "help": "目标初始状态：empty、warm或existing",
    },
    "thread_config": {
        "flags": ("--thread-config",),
        "type": str,
        "metavar": "PATH",
        "help": "重放语料对应的帖子配置JSON路径",
    },
    "profile": {
        "flags": ("--profile",),
        "type": str,
        "metavar": "PATH",
        "help": "重放延迟、带宽与最大并发配置JSON路径",
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
                "默认排序按上次扫描的最晚普通主题lastpost水位增量抓取：到达水位后处理完整页再停。",
                "置顶/公告与版面镜像（is_forum）不参与水位判定，避免误停；按tid去重并多扫一页重叠。",
                "首次扫描（数据库无该版面数据）时按pages上限全量抓取。",
                "pages只限制远端默认排序抓取页数上限，筛查从主题数据库读取。",
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
            "output_root_lock": True,
        },
    },
    "backup": {
        "auto": {
            "handler": backup_auto,
            "summary": "同步版面并智能选择增量或概率完整备份",
            "usage": (
                f"{PROGRAM_USAGE} backup auto [--watch-config PATH] "
                "[--workers N] [--api-concurrency N] "
                "[--image-concurrency N] [--audio-concurrency N] [--write-json]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup auto",
                f"{PROGRAM_USAGE} backup auto --workers 4",
            ],
            "notes": [
                "先执行默认forum sync，再仅备份本轮变化或有本地待处理工作的帖子。",
                "完整备份按距上次成功时间递增的稳定概率选择，"
                "达到config.json中的ankebak_full_backup_interval_hours后必定执行。",
                "首次启用时，缺少ankebak状态的帖子会立即执行完整备份。",
            ],
            "args": [
                "watch_config",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
                "write_json",
            ],
            "positive": [
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
            "output_root_lock": True,
        },
        "all": {
            "handler": backup_all,
            "summary": "抓取帖子内容并下载图片与音频",
            "usage": (
                f"{PROGRAM_USAGE} backup all "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--api-concurrency N] [--image-concurrency N] "
                "[--audio-concurrency N] "
                "[--write-json] [--force-processing]"
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
                "audio_concurrency",
                "write_json",
                "force_processing",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": [
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
            "output_root_lock": True,
        },
        "sub": {
            "handler": backup_sub,
            "summary": "增量补充本地缺失内容和远端新增内容",
            "usage": (
                f"{PROGRAM_USAGE} backup sub "
                "((--name NAME | --tid TID) [--aid AID] | --all-threads) "
                "[--workers N] [--api-concurrency N] [--image-concurrency N] "
                "[--audio-concurrency N] "
                "[--write-json] [--force-processing]"
            ),
            "examples": [
                f"{PROGRAM_USAGE} backup sub --name 帖子名",
                f"{PROGRAM_USAGE} backup sub --tid 12345678 --aid 987654",
                f"{PROGRAM_USAGE} backup sub --all-threads",
            ],
            "notes": [
                "此命令会补抓缺失页，并刷新本地尾页到远端最后一页。",
                "默认只写archive.sqlite3；加--write-json才输出debug_json/page_*.json。",
                "随后会从archive解析正文并补齐缺失图片与历史版本音频，不写入逐楼HTML。",
                "author-only备份会在archive.sqlite3中增量刷新楼层映射。",
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
                "audio_concurrency",
                "write_json",
                "force_processing",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": [
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
            "output_root_lock": True,
        },
        "pdf": {
            "handler": pdf_generate,
            "summary": "根据archive原始正文和已下载图片生成PDF",
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
                "author-only备份会读取archive.sqlite3中的楼层映射，同时显示只看作者楼层和原帖楼层。",
                f"如缺少楼层映射，运行 {PROGRAM_USAGE} backup sub --name 帖子名 "
                "--force-processing刷新备份。",
                "--all-threads会按thread_configs.json中的ThreadList批量生成PDF。",
                "--workers控制并行处理的帖子数，--pdf-workers控制全命令共享的"
                "WeasyPrint并发数。",
                "网页管理页保存的BBCode overlay会写入每帖archive.sqlite3的"
                "post_overlays表，普通查看和PDF都会按需渲染同一份overlay。",
                "overlay允许空内容；[img]仅接受已下载且本地文件有效的完整NGA图片URL。",
                "旧overlay/post_<楼层>.html不再读取。",
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
            "output_root_lock": True,
        },
    },
    "image": {
        "add": {
            "handler": image_add,
            "summary": "下载一张NGA图片并写入全局图片索引",
            "usage": f"{PROGRAM_USAGE} image add --url URL",
            "examples": [
                (
                    f"{PROGRAM_USAGE} image add --url "
                    "https://img.nga.178.com/attachments/mon_202506/06/example.png"
                ),
            ],
            "notes": [
                "只接受完整的NGA图片URL，不接受相对路径或外站图片。",
                "已有有效映射时直接成功；否则下载到images_unique并更新image_index.sqlite3。",
                "可先运行此命令，再把同一URL写入Web管理页的BBCode overlay。",
            ],
            "args": ["url"],
            "required": ["url"],
            "output_root_lock": True,
        },
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
                "提供帖子参数时会从archive正文解析引用并校验对应图片文件。",
            ],
            "args": ["name", "tid", "aid"],
            "output_root_lock": True,
        },
    },
    "web": {
        "serve": {
            "handler": web_serve,
            "summary": "启动本地Web查看器和管理页",
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
                "普通阅读和数据库浏览只读取本地备份，管理页可写入正文版本选择和overlay。",
                "此命令不访问NGA；管理写入会与其他output写任务互斥。",
                "默认只监听本机地址，避免把本地备份暴露到局域网。",
                "线程列表会显示已存盘的正文字数，并支持按正文字数排序。",
                "只支持当前archive.sqlite3格式；只有旧JSON的目录不会显示。",
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
    "replay": {
        "serve": {
            "handler": replay_serve,
            "summary": "从现有归档和图片启动只读NGA重放服务",
            "usage": (
                f"{PROGRAM_USAGE} replay serve --source-output PATH "
                "--profile PATH [--thread-config PATH] "
                "[--host HOST] [--port PORT]"
            ),
            "examples": [
                (
                    f"{PROGRAM_USAGE} replay serve --source-output output "
                    "--profile replay_profile.json"
                ),
                (
                    f"{PROGRAM_USAGE} replay serve --source-output output "
                    "--thread-config thread_configs.json "
                    "--profile replay_profile.json --port 8765"
                ),
            ],
            "notes": [
                "启动时从最新归一化内容合成API分页并预载入内存。",
                "重放不读取归档中的NGA原始分页响应。",
                "源SQLite以immutable只读方式打开，要求WAL已完成检查点。",
                "图片文件仅流式读取，不修改源数据库或图片内容。",
                "缺少原帖归档时会根据楼层映射合成原帖分页。",
                "默认只监听本机地址；此命令不会访问NGA或其他公网地址。",
            ],
            "args": [
                "source_output",
                "thread_config",
                "profile",
                "host",
                "port",
            ],
            "required": ["source_output", "profile"],
            "defaults": {
                "host": DEFAULT_REPLAY_HOST,
                "port": DEFAULT_REPLAY_PORT,
            },
            "positive": ["port"],
        },
        "run": {
            "handler": replay_run,
            "summary": "针对本地重放服务运行共享backup all基准路径",
            "usage": (
                f"{PROGRAM_USAGE} replay run --server-url URL "
                "--source-output PATH --target-output PATH "
                "--initial-state (empty|warm|existing) "
                "((--name NAME | --tid TID [--aid AID]) | --all-threads) "
                "[--thread-config PATH] [--workers N] "
                "[--api-concurrency N] [--image-concurrency N] "
                "[--audio-concurrency N]"
            ),
            "examples": [
                (
                    f"{PROGRAM_USAGE} replay run "
                    "--server-url http://127.0.0.1:8765 "
                    "--source-output output --target-output replay-output/run-a "
                    "--initial-state warm --all-threads --workers 8 "
                    "--api-concurrency 4 --image-concurrency 50"
                ),
                (
                    f"{PROGRAM_USAGE} replay run "
                    "--server-url http://127.0.0.1:8765 "
                    "--source-output output --target-output replay-output/sample "
                    "--initial-state empty --name 帖子名"
                ),
            ],
            "notes": [
                "empty与warm拒绝非空目标；existing要求目标已经存在。",
                "warm通过SQLite Online Backup复制数据库，并复制或reflink图片。",
                "API和图片实际请求只允许访问server-url，不读取环境代理或NGA Cookie。",
                "备份计时不含目标准备耗时，结果原子写入target-output下的replay_run JSON。",
            ],
            "args": [
                "server_url",
                "source_output",
                "target_output",
                "initial_state",
                "thread_config",
                "name",
                "tid",
                "aid",
                "all_threads",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
            "required": [
                "server_url",
                "source_output",
                "target_output",
                "initial_state",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": [
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
        },
        "test": {
            "handler": replay_test,
            "child_warning_summary": True,
            "summary": "自动启动重放服务和模拟运行进程进行测试",
            "usage": (
                f"{PROGRAM_USAGE} replay test --source-output PATH "
                "--profile PATH --target-output PATH "
                "--initial-state (empty|warm|existing) "
                "((--name NAME | --tid TID [--aid AID]) | --all-threads) "
                "[--thread-config PATH] [--port PORT] [--workers N] "
                "[--api-concurrency N] [--image-concurrency N] "
                "[--audio-concurrency N]"
            ),
            "examples": [
                (
                    f"{PROGRAM_USAGE} replay test "
                    "--source-output output --profile replay_profile.json "
                    "--target-output replay-output/run-a "
                    "--initial-state warm --all-threads --workers 8 "
                    "--api-concurrency 4 --image-concurrency 50"
                ),
                (
                    f"{PROGRAM_USAGE} replay test "
                    "--source-output output --profile replay_profile.json "
                    "--target-output replay-output/sample "
                    "--initial-state empty --name 帖子名 --port 8765"
                ),
            ],
            "notes": [
                "默认仅在127.0.0.1上自动选择空闲端口，也可通过--port覆盖。",
                "语料加载和服务就绪检查合计最多等待300秒。",
                "服务就绪后才启动模拟运行；两个子进程直接继承当前终端输出。",
                "模拟运行结束、失败或被中断时会自动停止重放服务并回收子进程。",
                "目标准备、离线网络保护、验收和报告格式与replay run相同。",
            ],
            "args": [
                "source_output",
                "profile",
                "target_output",
                "initial_state",
                "thread_config",
                "port",
                "name",
                "tid",
                "aid",
                "all_threads",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
            "required": [
                "source_output",
                "profile",
                "target_output",
                "initial_state",
            ],
            "required_any": ["name", "tid", "all_threads"],
            "positive": [
                "port",
                "workers",
                "api_concurrency",
                "image_concurrency",
                "audio_concurrency",
            ],
        },
    },
}


def all_actions() -> list[str]:
    actions = {
        action for action_configs in COMMANDS.values() for action in action_configs
    }
    return sorted(actions)

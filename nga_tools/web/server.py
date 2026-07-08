from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, cast

from aiohttp import web as aiohttp_web

from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.data import (
    ThreadNotFoundError,
    ThreadUnavailableError,
    load_thread_metadata,
    read_posts,
    read_thread_summary,
    safe_output_file,
    scan_thread_summaries,
)

_MAX_POST_LIMIT = 200


@dataclass(frozen=True)
class ViewerContext:
    output_dir: Path
    static_dir: Path


def _context(request: aiohttp_web.Request) -> ViewerContext:
    return cast(ViewerContext, request.app["viewer_context"])


def _json_error(message: str, status: int) -> aiohttp_web.Response:
    return aiohttp_web.json_response({"error": message}, status=status)


def _query_int(
    request: aiohttp_web.Request,
    name: str,
    default: Optional[int] = None,
) -> Optional[int]:
    value = request.query.get(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"{name}必须是整数。") from error


def _positive_query_int(
    request: aiohttp_web.Request,
    name: str,
    default: int,
) -> int:
    value = _query_int(request, name, default)
    if value is None or value <= 0:
        raise ValueError(f"{name}必须大于0。")
    return value


def _non_negative_query_int(
    request: aiohttp_web.Request,
    name: str,
    default: int,
) -> int:
    value = _query_int(request, name, default)
    if value is None or value < 0:
        raise ValueError(f"{name}不能为负数。")
    return value


def _match_int(request: aiohttp_web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except ValueError as error:
        raise ValueError(f"{name}必须是整数。") from error


async def health(request: aiohttp_web.Request) -> aiohttp_web.Response:
    context = _context(request)
    return aiohttp_web.json_response(
        {
            "ok": True,
            "outputDir": str(context.output_dir),
        }
    )


async def list_threads(request: aiohttp_web.Request) -> aiohttp_web.Response:
    context = _context(request)
    summaries = scan_thread_summaries(context.output_dir, load_thread_metadata())
    return aiohttp_web.json_response({"items": summaries})


async def thread_detail(request: aiohttp_web.Request) -> aiohttp_web.Response:
    context = _context(request)
    try:
        summary = read_thread_summary(
            context.output_dir,
            load_thread_metadata(),
            _match_int(request, "tid"),
            request.match_info["aid_key"],
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except ThreadNotFoundError as error:
        return _json_error(str(error), 404)
    return aiohttp_web.json_response(summary)


async def thread_posts(request: aiohttp_web.Request) -> aiohttp_web.Response:
    context = _context(request)
    try:
        limit = min(
            _positive_query_int(request, "limit", 50),
            _MAX_POST_LIMIT,
        )
        posts = read_posts(
            context.output_dir,
            _match_int(request, "tid"),
            request.match_info["aid_key"],
            offset=_non_negative_query_int(request, "offset", 0),
            limit=limit,
            query=request.query.get("q", ""),
            lou_from=_query_int(request, "lou_from"),
            lou_to=_query_int(request, "lou_to"),
        )
    except ValueError as error:
        return _json_error(str(error), 400)
    except ThreadNotFoundError as error:
        return _json_error(str(error), 404)
    except ThreadUnavailableError as error:
        return _json_error(str(error), 409)
    return aiohttp_web.json_response(posts)


async def output_file(request: aiohttp_web.Request) -> aiohttp_web.StreamResponse:
    context = _context(request)
    path = safe_output_file(context.output_dir, request.match_info["relative_path"])
    if path is None:
        return _json_error("文件不存在或不允许访问。", 404)
    return aiohttp_web.FileResponse(path)


async def static_app(request: aiohttp_web.Request) -> aiohttp_web.StreamResponse:
    context = _context(request)
    static_root = context.static_dir.resolve()
    index_path = static_root / "index.html"
    if not index_path.is_file():
        return _json_error(
            f"缺少前端构建产物：{index_path}。请先运行 pixi run web-build。",
            503,
        )

    tail = request.match_info.get("tail", "")
    if tail:
        candidate = (static_root / tail).resolve()
        if candidate.is_relative_to(static_root) and candidate.is_file():
            return aiohttp_web.FileResponse(candidate)
    return aiohttp_web.FileResponse(index_path)


def create_app(
    *,
    output_dir: Optional[Path] = None,
    static_dir: Optional[Path] = None,
) -> aiohttp_web.Application:
    resolved_output_dir = (
        Path(get_config().output_dir) if output_dir is None else output_dir
    )
    context = ViewerContext(
        output_dir=resolved_output_dir,
        static_dir=Path(DEFAULT_WEB_STATIC_DIR) if static_dir is None else static_dir,
    )
    app = aiohttp_web.Application()
    app["viewer_context"] = context
    app.router.add_get("/api/health", health)
    app.router.add_get("/api/threads", list_threads)
    app.router.add_get("/api/threads/{tid}/{aid_key}", thread_detail)
    app.router.add_get("/api/threads/{tid}/{aid_key}/posts", thread_posts)
    app.router.add_get("/api/files/{relative_path:.*}", output_file)
    app.router.add_get("/{tail:.*}", static_app)
    return app


def serve_app(
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    static_dir: Path = Path(DEFAULT_WEB_STATIC_DIR),
) -> None:
    report_info(f"只读Web查看服务：http://{host}:{port}/")
    aiohttp_web.run_app(
        create_app(static_dir=static_dir),
        host=host,
        port=port,
        print=None,
    )

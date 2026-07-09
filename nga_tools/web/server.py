from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Optional, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.backup.floor_models import ORIGINAL_POSTS_PER_PAGE
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.data import (
    PostsResult,
    PostVersionGroupsResult,
    PostVersionPreview,
    PostVersionSelectionResult,
    PostVersionThreadSummariesResult,
    ThreadNotFoundError,
    ThreadSummary,
    ThreadUnavailableError,
    clear_post_version_selection,
    load_thread_metadata,
    read_post_version_groups,
    read_post_version_preview,
    read_post_version_thread_summaries,
    read_posts,
    read_thread_summary,
    safe_output_file,
    scan_thread_summaries,
    select_post_version,
)
from nga_tools.web.database import (
    DatabaseNotFoundError,
    DatabaseSchema,
    DatabaseSummary,
    DatabaseUnavailableError,
    RowNotFoundError,
    TableNotFoundError,
    TableRowDetail,
    TableRows,
    list_database_summaries,
    read_database_schema,
    read_table_row_detail,
    read_table_rows,
)

_MAX_POST_LIMIT = 200
_MAX_DATABASE_ROW_LIMIT = 200


@dataclass(frozen=True)
class ViewerContext:
    output_dir: Path
    static_dir: Path


def _context(request: Request) -> ViewerContext:
    return cast(ViewerContext, request.app.state.viewer_context)


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _http_exception_handler(
    _request: Request,
    exception: Exception,
) -> JSONResponse:
    if isinstance(exception, HTTPException):
        return _error_response(exception.detail, exception.status_code)
    return _error_response("请求失败。", 500)


async def _validation_exception_handler(
    _request: Request,
    _exception: Exception,
) -> JSONResponse:
    return _error_response("请求参数无效。", 422)


async def health(request: Request) -> dict[str, object]:
    context = _context(request)
    return {
        "ok": True,
        "outputDir": str(context.output_dir),
    }


async def list_threads(request: Request) -> dict[str, list[ThreadSummary]]:
    context = _context(request)
    summaries = scan_thread_summaries(context.output_dir, load_thread_metadata())
    return {"items": summaries}


async def list_post_version_threads(
    request: Request,
    multi_version_only: bool = False,
) -> PostVersionThreadSummariesResult:
    context = _context(request)
    return read_post_version_thread_summaries(
        context.output_dir,
        load_thread_metadata(),
        multi_version_only=multi_version_only,
    )


async def thread_detail(
    request: Request,
    tid: int,
    aid_key: str,
) -> ThreadSummary:
    context = _context(request)
    try:
        return read_thread_summary(
            context.output_dir,
            load_thread_metadata(),
            tid,
            aid_key,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


async def thread_posts(
    request: Request,
    tid: int,
    aid_key: str,
    page: Annotated[int, Query(ge=1)] = 1,
    offset: Annotated[Optional[int], Query(ge=0)] = None,
    limit: Annotated[Optional[int], Query(gt=0, le=_MAX_POST_LIMIT)] = None,
    q: str = "",
    lou_from: Optional[int] = None,
    lou_to: Optional[int] = None,
) -> PostsResult:
    context = _context(request)
    resolved_page = page
    if offset is not None and page == 1:
        resolved_page = offset // ORIGINAL_POSTS_PER_PAGE + 1
    del limit
    try:
        return read_posts(
            context.output_dir,
            tid,
            aid_key,
            page=resolved_page,
            query=q,
            lou_from=lou_from,
            lou_to=lou_to,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def post_version_groups(
    request: Request,
    tid: int,
    aid_key: str,
) -> PostVersionGroupsResult:
    context = _context(request)
    try:
        return read_post_version_groups(context.output_dir, tid, aid_key)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def post_version_preview(
    request: Request,
    tid: int,
    aid_key: str,
    version_id: int,
) -> PostVersionPreview:
    context = _context(request)
    try:
        return read_post_version_preview(context.output_dir, tid, aid_key, version_id)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def put_post_version_selection(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostVersionSelectionResult:
    raw_version_id = payload.get("versionId")
    if type(raw_version_id) is not int:
        raise HTTPException(status_code=400, detail="versionId必须是整数。")
    context = _context(request)
    try:
        return select_post_version(
            context.output_dir,
            tid,
            aid_key,
            lou,
            raw_version_id,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def delete_post_version_selection(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostVersionSelectionResult:
    context = _context(request)
    try:
        return clear_post_version_selection(context.output_dir, tid, aid_key, lou)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def list_databases(request: Request) -> dict[str, list[DatabaseSummary]]:
    context = _context(request)
    return {"items": list_database_summaries(context.output_dir)}


async def database_schema(request: Request, db_id: str) -> DatabaseSchema:
    context = _context(request)
    try:
        return read_database_schema(context.output_dir, db_id)
    except DatabaseNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def database_table_rows(
    request: Request,
    db_id: str,
    table_name: str,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_DATABASE_ROW_LIMIT)] = 50,
    q: str = "",
    sort_by: Optional[str] = None,
    sort_direction: Literal["asc", "desc"] = "asc",
) -> TableRows:
    context = _context(request)
    try:
        return read_table_rows(
            context.output_dir,
            db_id,
            table_name,
            offset=offset,
            limit=limit,
            query=q,
            sort_by=sort_by,
            sort_direction=sort_direction,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (DatabaseNotFoundError, TableNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def database_row_detail(
    request: Request,
    db_id: str,
    table_name: str,
    rowid: int,
) -> TableRowDetail:
    context = _context(request)
    try:
        return read_table_row_detail(context.output_dir, db_id, table_name, rowid)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except (DatabaseNotFoundError, TableNotFoundError, RowNotFoundError) as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except DatabaseUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def output_file(
    request: Request,
    relative_path: str,
) -> FileResponse:
    context = _context(request)
    path = safe_output_file(context.output_dir, relative_path)
    if path is None:
        raise HTTPException(status_code=404, detail="文件不存在或不允许访问。")
    return FileResponse(path)


async def static_app(
    request: Request,
    tail: str = "",
) -> FileResponse:
    context = _context(request)
    static_root = context.static_dir.resolve()
    index_path = static_root / "index.html"
    if not index_path.is_file():
        raise HTTPException(
            status_code=503,
            detail=f"缺少前端构建产物：{index_path}。请先运行 pixi run web-build。",
        )

    if tail:
        candidate = (static_root / tail).resolve()
        if candidate.is_relative_to(static_root) and candidate.is_file():
            return FileResponse(candidate)
    return FileResponse(index_path)


def create_app(
    *,
    output_dir: Optional[Path] = None,
    static_dir: Optional[Path] = None,
) -> FastAPI:
    resolved_output_dir = (
        Path(get_config().output_dir) if output_dir is None else output_dir
    )
    context = ViewerContext(
        output_dir=resolved_output_dir,
        static_dir=Path(DEFAULT_WEB_STATIC_DIR) if static_dir is None else static_dir,
    )
    app = FastAPI()
    app.state.viewer_context = context
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_api_route("/api/health", health, methods=["GET"])
    app.add_api_route("/api/threads", list_threads, methods=["GET"])
    app.add_api_route("/api/threads/{tid}/{aid_key}", thread_detail, methods=["GET"])
    app.add_api_route(
        "/api/threads/{tid}/{aid_key}/posts",
        thread_posts,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/post-version-threads",
        list_post_version_threads,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/post-versions",
        post_version_groups,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/post-versions/{version_id}/preview",
        post_version_preview,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/post-version-selections/{lou}",
        put_post_version_selection,
        methods=["PUT"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/post-version-selections/{lou}",
        delete_post_version_selection,
        methods=["DELETE"],
    )
    app.add_api_route("/api/databases", list_databases, methods=["GET"])
    app.add_api_route("/api/databases/{db_id}/schema", database_schema, methods=["GET"])
    app.add_api_route(
        "/api/databases/{db_id}/tables/{table_name}/rows",
        database_table_rows,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/databases/{db_id}/tables/{table_name}/rows/{rowid}",
        database_row_detail,
        methods=["GET"],
    )
    app.add_api_route("/api/files/{relative_path:path}", output_file, methods=["GET"])
    app.add_api_route("/{tail:path}", static_app, methods=["GET"])
    return app


def serve_app(
    *,
    host: str = DEFAULT_WEB_HOST,
    port: int = DEFAULT_WEB_PORT,
    static_dir: Path = Path(DEFAULT_WEB_STATIC_DIR),
) -> None:
    report_info(f"Web查看服务：http://{host}:{port}/")
    report_info("管理界面会写入本地正文版本选择并刷新html_modified。")
    uvicorn.run(
        create_app(static_dir=static_dir),
        host=host,
        port=port,
        log_level="warning",
    )

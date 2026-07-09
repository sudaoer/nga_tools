from __future__ import annotations

import threading
from _thread import LockType
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, Optional, cast

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.backup.floor_models import ORIGINAL_POSTS_PER_PAGE
from nga_tools.forum.thread_configs import ThreadConfig
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.data import (
    PostOverlayDetail,
    PostOverlayPreview,
    PostsResult,
    PostVersionGroupsResult,
    PostVersionPreview,
    PostVersionSelectionResult,
    PostVersionThreadSummariesResult,
    ThreadNotFoundError,
    ThreadSummary,
    ThreadSummaryDetail,
    ThreadUnavailableError,
    clear_thread_post_overlay,
    clear_post_version_selection,
    load_thread_metadata,
    parse_thread_dir_name,
    preview_post_overlay,
    read_post_overlay,
    read_post_version_groups,
    read_post_version_preview,
    read_post_version_thread_summaries,
    read_posts,
    read_thread_summary,
    safe_output_file,
    save_thread_post_overlay,
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
ThreadListFingerprint = tuple[str, ...]


def _new_lock() -> LockType:
    return threading.Lock()


def _new_lock_map() -> dict[Path, LockType]:
    return {}


def _file_fingerprint(path: Path) -> str:
    try:
        stat_result = path.stat()
    except OSError:
        return "-"
    return f"{stat_result.st_mtime_ns}:{stat_result.st_size}"


def _thread_list_fingerprint(output_dir: Path) -> ThreadListFingerprint:
    entries = [
        f"config:{_file_fingerprint(Path(get_config().thread_config_file))}",
        f"output:{_file_fingerprint(output_dir)}",
    ]
    if not output_dir.is_dir():
        return tuple(entries)

    for thread_folder in sorted(output_dir.iterdir(), key=lambda path: path.name):
        if not thread_folder.is_dir():
            continue
        if parse_thread_dir_name(thread_folder.name) is None:
            continue
        html_modified_dir = thread_folder / "html_modified"
        entries.append(
            "\0".join(
                [
                    thread_folder.name,
                    _file_fingerprint(thread_folder),
                    _file_fingerprint(thread_folder / "archive.sqlite3"),
                    _file_fingerprint(html_modified_dir),
                    _file_fingerprint(
                        html_modified_dir / "html_modified_manifest.json"
                    ),
                    _file_fingerprint(thread_folder / "floor_map.json"),
                    _file_fingerprint(thread_folder / "warnings.log"),
                    _file_fingerprint(thread_folder / "json"),
                ]
            )
        )
    return tuple(entries)


def _copy_thread_summaries(items: list[ThreadSummary]) -> list[ThreadSummary]:
    return [cast(ThreadSummary, dict(item)) for item in items]


@dataclass
class ThreadSummaryCache:
    _guard: LockType = field(default_factory=_new_lock)
    _fingerprint: Optional[ThreadListFingerprint] = None
    _items: Optional[list[ThreadSummary]] = None

    def read(
        self,
        output_dir: Path,
        metadata_by_key: dict[tuple[int, str], ThreadConfig],
        *,
        refresh: bool,
    ) -> list[ThreadSummary]:
        fingerprint = _thread_list_fingerprint(output_dir)
        with self._guard:
            if (
                not refresh
                and self._fingerprint == fingerprint
                and self._items is not None
            ):
                return _copy_thread_summaries(self._items)

            items = scan_thread_summaries(
                output_dir,
                metadata_by_key,
                detail="full",
            )
            self._fingerprint = _thread_list_fingerprint(output_dir)
            self._items = _copy_thread_summaries(items)
            return _copy_thread_summaries(items)


@dataclass(frozen=True)
class PostVersionSelectionLocks:
    _guard: LockType = field(default_factory=_new_lock)
    _locks: dict[Path, LockType] = field(default_factory=_new_lock_map)

    def for_thread(self, thread_folder: Path) -> LockType:
        lock_key = thread_folder.resolve()
        with self._guard:
            lock = self._locks.get(lock_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[lock_key] = lock
            return lock


@dataclass(frozen=True)
class ViewerContext:
    output_dir: Path
    static_dir: Path
    post_version_selection_locks: PostVersionSelectionLocks
    thread_summary_cache: ThreadSummaryCache


def _context(request: Request) -> ViewerContext:
    return cast(ViewerContext, request.app.state.viewer_context)


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _list_threads_sync(
    output_dir: Path,
    cache: ThreadSummaryCache,
    detail: ThreadSummaryDetail,
    refresh: bool,
) -> dict[str, list[ThreadSummary]]:
    metadata_by_key = load_thread_metadata()
    if detail == "light":
        return {
            "items": scan_thread_summaries(
                output_dir,
                metadata_by_key,
                detail="light",
            )
        }
    return {"items": cache.read(output_dir, metadata_by_key, refresh=refresh)}


def _list_post_version_threads_sync(
    output_dir: Path,
    *,
    multi_version_only: bool,
) -> PostVersionThreadSummariesResult:
    return read_post_version_thread_summaries(
        output_dir,
        load_thread_metadata(),
        multi_version_only=multi_version_only,
    )


def _read_thread_summary_sync(
    output_dir: Path,
    tid: int,
    aid_key: str,
) -> ThreadSummary:
    return read_thread_summary(
        output_dir,
        load_thread_metadata(),
        tid,
        aid_key,
    )


def _thread_folder_for_lock(context: ViewerContext, tid: int, aid_key: str) -> Path:
    return context.output_dir / f"{tid}_{aid_key}"


def _select_post_version_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
    version_id: int,
) -> PostVersionSelectionResult:
    with lock:
        return select_post_version(output_dir, tid, aid_key, lou, version_id)


def _clear_post_version_selection_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostVersionSelectionResult:
    with lock:
        return clear_post_version_selection(output_dir, tid, aid_key, lou)


def _save_post_overlay_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
    bbcode: str,
) -> PostOverlayDetail:
    with lock:
        return save_thread_post_overlay(output_dir, tid, aid_key, lou, bbcode)


def _clear_post_overlay_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    with lock:
        return clear_thread_post_overlay(output_dir, tid, aid_key, lou)


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


async def list_threads(
    request: Request,
    detail: ThreadSummaryDetail = "full",
    refresh: bool = False,
) -> dict[str, list[ThreadSummary]]:
    context = _context(request)
    return await run_in_threadpool(
        _list_threads_sync,
        context.output_dir,
        context.thread_summary_cache,
        detail,
        refresh,
    )


async def list_post_version_threads(
    request: Request,
    multi_version_only: bool = False,
) -> PostVersionThreadSummariesResult:
    context = _context(request)
    return await run_in_threadpool(
        _list_post_version_threads_sync,
        context.output_dir,
        multi_version_only=multi_version_only,
    )


async def thread_detail(
    request: Request,
    tid: int,
    aid_key: str,
) -> ThreadSummary:
    context = _context(request)
    try:
        return await run_in_threadpool(
            _read_thread_summary_sync,
            context.output_dir,
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
        return await run_in_threadpool(
            read_posts,
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
        return await run_in_threadpool(
            read_post_version_groups,
            context.output_dir,
            tid,
            aid_key,
        )
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
        return await run_in_threadpool(
            read_post_version_preview,
            context.output_dir,
            tid,
            aid_key,
            version_id,
        )
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
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await run_in_threadpool(
            _select_post_version_locked,
            lock,
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
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await run_in_threadpool(
            _clear_post_version_selection_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def post_overlay_detail(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    context = _context(request)
    try:
        return await run_in_threadpool(
            read_post_overlay,
            context.output_dir,
            tid,
            aid_key,
            lou,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def post_overlay_preview(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostOverlayPreview:
    raw_bbcode = payload.get("bbcode")
    if not isinstance(raw_bbcode, str):
        raise HTTPException(status_code=400, detail="bbcode必须是字符串。")
    context = _context(request)
    try:
        return await run_in_threadpool(
            preview_post_overlay,
            context.output_dir,
            tid,
            aid_key,
            lou,
            raw_bbcode,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def put_post_overlay(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostOverlayDetail:
    raw_bbcode = payload.get("bbcode")
    if not isinstance(raw_bbcode, str):
        raise HTTPException(status_code=400, detail="bbcode必须是字符串。")
    context = _context(request)
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await run_in_threadpool(
            _save_post_overlay_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
            raw_bbcode,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def delete_post_overlay(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    context = _context(request)
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await run_in_threadpool(
            _clear_post_overlay_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ThreadNotFoundError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ThreadUnavailableError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


async def list_databases(request: Request) -> dict[str, list[DatabaseSummary]]:
    context = _context(request)
    summaries = await run_in_threadpool(list_database_summaries, context.output_dir)
    return {"items": summaries}


async def database_schema(request: Request, db_id: str) -> DatabaseSchema:
    context = _context(request)
    try:
        return await run_in_threadpool(read_database_schema, context.output_dir, db_id)
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
        return await run_in_threadpool(
            read_table_rows,
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
        return await run_in_threadpool(
            read_table_row_detail,
            context.output_dir,
            db_id,
            table_name,
            rowid,
        )
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
    path = await run_in_threadpool(safe_output_file, context.output_dir, relative_path)
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
        post_version_selection_locks=PostVersionSelectionLocks(),
        thread_summary_cache=ThreadSummaryCache(),
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
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/overlays/{lou}",
        post_overlay_detail,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/overlays/{lou}/preview",
        post_overlay_preview,
        methods=["POST"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/overlays/{lou}",
        put_post_overlay,
        methods=["PUT"],
    )
    app.add_api_route(
        "/api/admin/threads/{tid}/{aid_key}/overlays/{lou}",
        delete_post_overlay,
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

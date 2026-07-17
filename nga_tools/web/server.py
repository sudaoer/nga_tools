from __future__ import annotations

from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError

from nga_tools.config import get_config
from nga_tools.console import report_info
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.routes import (
    database_row_detail,
    database_schema,
    database_table_rows,
    delete_post_overlay,
    delete_post_version_selection,
    health,
    http_exception_handler,
    image_usage_detail_route,
    image_usage_replies_route,
    list_databases,
    list_image_problems,
    list_image_usage,
    list_post_version_threads,
    list_threads,
    output_file,
    post_overlay_detail,
    post_overlay_preview,
    post_version_groups,
    post_version_preview,
    put_post_overlay,
    put_post_version_selection,
    static_app,
    thread_detail,
    thread_posts,
    validation_exception_handler,
)
from nga_tools.web.server_state import (
    DatabaseSchemaCache,
    DatabaseSummaryCache,
    ImageUsageCache,
    PostVersionSelectionLocks,
    PostVersionThreadCache,
    ThreadSummaryCache,
    ViewerContext,
)


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
        post_version_thread_cache=PostVersionThreadCache(),
        database_summary_cache=DatabaseSummaryCache(),
        database_schema_cache=DatabaseSchemaCache(),
        image_usage_cache=ImageUsageCache(),
    )
    app = FastAPI()
    app.state.viewer_context = context
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
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
    app.add_api_route("/api/admin/image-usage", list_image_usage, methods=["GET"])
    app.add_api_route(
        "/api/admin/image-usage/detail",
        image_usage_detail_route,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/image-usage/replies",
        image_usage_replies_route,
        methods=["GET"],
    )
    app.add_api_route(
        "/api/admin/image-problems",
        list_image_problems,
        methods=["GET"],
    )
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
    report_info("管理界面会写入本地正文版本选择和BBCode overlay。")
    uvicorn.run(
        create_app(static_dir=static_dir),
        host=host,
        port=port,
        log_level="warning",
    )

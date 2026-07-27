from __future__ import annotations

from _thread import LockType
from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Annotated, Literal, Optional, cast
from urllib.parse import urlencode

from fastapi import HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse

from nga_tools.backup.floor_models import ORIGINAL_POSTS_PER_PAGE
from nga_tools.core.output_lock import ThreadOutputLockError, use_output_root_lock
from nga_tools.web.database import (
    DatabaseSchema,
    DatabaseSummary,
    TableRowDetail,
    TableRows,
    read_table_row_detail,
    read_table_rows,
)
from nga_tools.web.errors import WebBadRequest, WebConflict, WebNotFound
from nga_tools.web.image_problem_markup import annotate_image_problem_html
from nga_tools.web.image_usage import (
    ImageProblemFilter,
    ImageProblemIssueItem,
    ImageProblemPostReference,
    ImageProblemPostItem,
    ImageProblemsResult,
    ImageUsageDetailResult,
    ImageUsageRepliesResult,
    ImageUsageReplyItem,
    ImageUsageResult,
    ImageUsageSnapshot,
    ImageUsageSort,
    copy_image_problem_kind_counts,
    image_problem_kind_counts,
    image_problem_references,
    image_reply_references,
    image_usage_detail,
    image_usage_result,
)
from nga_tools.web.cluster_data import (
    ClusterDetailResult,
    ClustersResult,
    ClusterStatsResult,
    read_cluster_detail,
    read_clusters,
    read_cluster_stats,
)
from nga_tools.web.output_files import safe_output_file
from nga_tools.web.post_data import (
    PostOverlayDetail,
    PostOverlayPreview,
    PostsResult,
    PostVersionGroupsResult,
    PostVersionPreview,
    PostVersionSelectionResult,
    clear_post_version_selection,
    clear_thread_post_overlay,
    preview_post_overlay,
    read_post_overlay,
    read_post_version_groups,
    read_post_version_preview,
    read_posts,
    save_thread_post_overlay,
    select_post_version,
)
from nga_tools.web.server_state import (
    PostVersionThreadCache,
    ThreadSummaryCache,
    ViewerContext,
)
from nga_tools.web.thread_data import (
    PostVersionThreadSummariesResult,
    ThreadNotFoundError,
    ThreadSummary,
    ThreadSummaryDetail,
    load_thread_metadata,
    read_thread_summary,
    scan_thread_summaries,
)

_MAX_POST_LIMIT = 200
_MAX_DATABASE_ROW_LIMIT = 200
_MAX_IMAGE_USAGE_LIMIT = 200
_MAX_CLUSTER_LIMIT = 200


def _context(request: Request) -> ViewerContext:
    return cast(ViewerContext, request.app.state.viewer_context)


def _error_response(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


async def _run_with_bad_request[**Params, ResultT](
    operation: Callable[Params, ResultT],
    *args: Params.args,
    **kwargs: Params.kwargs,
) -> ResultT:
    try:
        return await run_in_threadpool(operation, *args, **kwargs)
    except ValueError as error:
        raise WebBadRequest(str(error)) from error


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
    cache: PostVersionThreadCache,
    detail: ThreadSummaryDetail,
    refresh: bool,
    *,
    multi_version_only: bool,
) -> PostVersionThreadSummariesResult:
    items = cache.read(
        output_dir,
        load_thread_metadata(),
        detail=detail,
        refresh=refresh,
    )
    if multi_version_only:
        items = [
            item for item in items if item["multiVersionFloorCount"] > 0
        ]
    return {"items": items}


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
    with use_output_root_lock(output_dir):
        with lock:
            return select_post_version(output_dir, tid, aid_key, lou, version_id)


def _clear_post_version_selection_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostVersionSelectionResult:
    with use_output_root_lock(output_dir):
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
    with use_output_root_lock(output_dir):
        with lock:
            return save_thread_post_overlay(output_dir, tid, aid_key, lou, bbcode)


def _clear_post_overlay_locked(
    lock: LockType,
    output_dir: Path,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    with use_output_root_lock(output_dir):
        with lock:
            return clear_thread_post_overlay(output_dir, tid, aid_key, lou)


async def http_exception_handler(
    _request: Request,
    exception: Exception,
) -> JSONResponse:
    if isinstance(exception, HTTPException):
        return _error_response(exception.detail, exception.status_code)
    return _error_response("请求失败。", 500)


async def validation_exception_handler(
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
    detail: ThreadSummaryDetail = "full",
    refresh: bool = False,
) -> PostVersionThreadSummariesResult:
    context = _context(request)
    return await run_in_threadpool(
        _list_post_version_threads_sync,
        context.output_dir,
        context.post_version_thread_cache,
        detail,
        refresh,
        multi_version_only=multi_version_only,
    )


async def thread_detail(
    request: Request,
    tid: int,
    aid_key: str,
) -> ThreadSummary:
    context = _context(request)
    return await _run_with_bad_request(
        _read_thread_summary_sync,
        context.output_dir,
        tid,
        aid_key,
    )


async def thread_posts(
    request: Request,
    tid: int,
    aid_key: str,
    page: Annotated[int, Query(ge=1)] = 1,
    offset: Annotated[Optional[int], Query(ge=0)] = None,
    _limit: Annotated[
        Optional[int],
        Query(alias="limit", gt=0, le=_MAX_POST_LIMIT),
    ] = None,
    q: str = "",
    lou_from: Optional[int] = None,
    lou_to: Optional[int] = None,
) -> PostsResult:
    context = _context(request)
    resolved_page = page
    if offset is not None and page == 1:
        resolved_page = offset // ORIGINAL_POSTS_PER_PAGE + 1
    return await _run_with_bad_request(
        read_posts,
        context.output_dir,
        tid,
        aid_key,
        page=resolved_page,
        query=q,
        lou_from=lou_from,
        lou_to=lou_to,
    )


async def post_version_groups(
    request: Request,
    tid: int,
    aid_key: str,
) -> PostVersionGroupsResult:
    context = _context(request)
    return await _run_with_bad_request(
        read_post_version_groups,
        context.output_dir,
        tid,
        aid_key,
    )


async def post_version_preview(
    request: Request,
    tid: int,
    aid_key: str,
    version_id: int,
) -> PostVersionPreview:
    context = _context(request)
    return await _run_with_bad_request(
        read_post_version_preview,
        context.output_dir,
        tid,
        aid_key,
        version_id,
    )


async def put_post_version_selection(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostVersionSelectionResult:
    raw_version_id = payload.get("versionId")
    if type(raw_version_id) is not int:
        raise WebBadRequest("versionId必须是整数。")
    context = _context(request)
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await _run_with_bad_request(
            _select_post_version_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
            raw_version_id,
        )
    except ThreadOutputLockError as error:
        raise WebConflict(str(error)) from error


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
        return await _run_with_bad_request(
            _clear_post_version_selection_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
        )
    except ThreadOutputLockError as error:
        raise WebConflict(str(error)) from error


async def post_overlay_detail(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
) -> PostOverlayDetail:
    context = _context(request)
    return await _run_with_bad_request(
        read_post_overlay,
        context.output_dir,
        tid,
        aid_key,
        lou,
    )


async def post_overlay_preview(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostOverlayPreview:
    raw_bbcode = payload.get("bbcode")
    if not isinstance(raw_bbcode, str):
        raise WebBadRequest("bbcode必须是字符串。")
    context = _context(request)
    return await _run_with_bad_request(
        preview_post_overlay,
        context.output_dir,
        tid,
        aid_key,
        lou,
        raw_bbcode,
    )


async def put_post_overlay(
    request: Request,
    tid: int,
    aid_key: str,
    lou: int,
    payload: dict[str, object],
) -> PostOverlayDetail:
    raw_bbcode = payload.get("bbcode")
    if not isinstance(raw_bbcode, str):
        raise WebBadRequest("bbcode必须是字符串。")
    context = _context(request)
    lock = context.post_version_selection_locks.for_thread(
        _thread_folder_for_lock(context, tid, aid_key)
    )
    try:
        return await _run_with_bad_request(
            _save_post_overlay_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
            raw_bbcode,
        )
    except ThreadOutputLockError as error:
        raise WebConflict(str(error)) from error


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
        return await _run_with_bad_request(
            _clear_post_overlay_locked,
            lock,
            context.output_dir,
            tid,
            aid_key,
            lou,
        )
    except ThreadOutputLockError as error:
        raise WebConflict(str(error)) from error


async def list_databases(
    request: Request,
    refresh: bool = False,
) -> dict[str, list[DatabaseSummary]]:
    context = _context(request)
    summaries = await run_in_threadpool(
        context.database_summary_cache.read,
        context.output_dir,
        refresh=refresh,
    )
    return {"items": summaries}


async def list_image_usage(
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_IMAGE_USAGE_LIMIT)] = 100,
    sort: ImageUsageSort = "usage",
    refresh: bool = False,
) -> ImageUsageResult:
    context = _context(request)
    snapshot = await run_in_threadpool(
        context.image_usage_cache.read,
        context.output_dir,
        refresh=refresh,
    )
    return image_usage_result(snapshot, offset=offset, limit=limit, sort=sort)


def _image_problem_post_item(
    output_dir: Path,
    snapshot: ImageUsageSnapshot,
    reference: ImageProblemPostReference,
    page_cache: dict[tuple[int, str, int], PostsResult],
) -> Optional[ImageProblemPostItem]:
    page = reference.lou // ORIGINAL_POSTS_PER_PAGE + 1
    cache_key = (reference.tid, reference.aid_key, page)
    posts = page_cache.get(cache_key)
    if posts is None:
        posts = read_posts(
            output_dir,
            reference.tid,
            reference.aid_key,
            page=page,
        )
        page_cache[cache_key] = posts
    post = next(
        (
            item
            for item in posts["items"]
            if item["lou"] == reference.lou and item["pid"] == reference.pid
        ),
        None,
    )
    if post is None:
        return None

    issues: list[ImageProblemIssueItem] = [
        {
            "kind": issue.kind,
            "url": issue.url,
            "occurrenceCount": issue.occurrence_count,
            "imageIndexes": list(issue.image_indexes),
            "sourceIndexes": list(issue.source_indexes),
            "relativePath": issue.relative_path,
        }
        for issue in reference.issues
    ]
    edit_query = urlencode(
        {
            "tid": reference.tid,
            "aid": reference.aid_key,
            "page": page,
            "lou_from": reference.lou,
            "lou_to": reference.lou,
            "overlay_lou": reference.lou,
        }
    )
    return {
        "tid": reference.tid,
        "aidKey": reference.aid_key,
        "dirName": reference.dir_name,
        "title": snapshot.thread_titles.get(
            reference.tid,
            f"tid {reference.tid}",
        ),
        "pid": reference.pid,
        "lou": reference.lou,
        "floorLabel": post["floorLabel"],
        "authorName": post["authorName"],
        "postdate": post["postdate"],
        "issueCount": sum(issue["occurrenceCount"] for issue in issues),
        "issues": issues,
        "html": annotate_image_problem_html(post["html"], reference.issues),
        "editUrl": f"/threads?{edit_query}",
    }


def _read_image_problems_sync(
    output_dir: Path,
    snapshot: ImageUsageSnapshot,
    offset: int,
    limit: int,
    kind: ImageProblemFilter,
    query: str,
) -> ImageProblemsResult:
    normalized_query = query.strip()
    matching_references = image_problem_references(
        snapshot,
        "all",
        normalized_query,
    )
    references = (
        matching_references
        if kind == "all"
        else tuple(
            reference
            for reference in matching_references
            if any(issue.kind == kind for issue in reference.issues)
        )
    )
    selected_references = references[offset : offset + limit]
    page_cache: dict[tuple[int, str, int], PostsResult] = {}
    items: list[ImageProblemPostItem] = []
    for reference in selected_references:
        item = _image_problem_post_item(
            output_dir,
            snapshot,
            reference,
            page_cache,
        )
        if item is not None:
            items.append(item)

    kind_counts = (
        copy_image_problem_kind_counts(snapshot.problem_kind_counts)
        if not normalized_query
        else image_problem_kind_counts(matching_references)
    )
    return {
        "items": items,
        "total": len(references),
        "offset": offset,
        "limit": limit,
        "kind": kind,
        "query": normalized_query,
        "computedAt": snapshot.computed_at,
        "archiveCount": snapshot.archive_count,
        "scannedPostCount": snapshot.post_count,
        "problemPostCount": len(matching_references),
        "problemThreadCount": len(
            {reference.tid for reference in matching_references}
        ),
        "problemOccurrenceCount": (
            kind_counts["invalid_url"]["occurrenceCount"]
            + kind_counts["unmapped"]["occurrenceCount"]
            + kind_counts["missing_file"]["occurrenceCount"]
        ),
        "kindCounts": kind_counts,
        "skippedArchives": deepcopy(snapshot.skipped_archives),
    }


async def list_image_problems(
    request: Request,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_POST_LIMIT)] = 20,
    kind: ImageProblemFilter = "all",
    q: str = "",
    refresh: bool = False,
) -> ImageProblemsResult:
    context = _context(request)
    try:
        snapshot = await run_in_threadpool(
            context.image_usage_cache.read,
            context.output_dir,
            refresh=refresh,
        )
        return await run_in_threadpool(
            _read_image_problems_sync,
            context.output_dir,
            snapshot,
            offset,
            limit,
            kind,
            q,
        )
    except ThreadNotFoundError as error:
        raise WebConflict(str(error)) from error


async def image_usage_detail_route(
    request: Request,
    relative_path: Annotated[str, Query(min_length=1)],
) -> ImageUsageDetailResult:
    context = _context(request)
    snapshot = await run_in_threadpool(
        context.image_usage_cache.read,
        context.output_dir,
        refresh=False,
    )
    return image_usage_detail(snapshot, relative_path)


def _read_image_usage_replies_sync(
    output_dir: Path,
    snapshot: ImageUsageSnapshot,
    relative_path: str,
    tid: int,
    offset: int,
    limit: int,
) -> ImageUsageRepliesResult:
    references = image_reply_references(snapshot, relative_path, tid)
    selected_references = references[offset : offset + limit]
    page_cache: dict[tuple[int, str, int], PostsResult] = {}
    items: list[ImageUsageReplyItem] = []
    for reference in selected_references:
        page = reference.lou // ORIGINAL_POSTS_PER_PAGE + 1
        cache_key = (reference.tid, reference.aid_key, page)
        posts = page_cache.get(cache_key)
        if posts is None:
            posts = read_posts(
                output_dir,
                reference.tid,
                reference.aid_key,
                page=page,
            )
            page_cache[cache_key] = posts
        post = next(
            (
                item
                for item in posts["items"]
                if item["lou"] == reference.lou and item["pid"] == reference.pid
            ),
            None,
        )
        if post is None:
            continue
        reader_query = urlencode(
            {
                "tid": reference.tid,
                "aid": reference.aid_key,
                "page": page,
            }
        )
        items.append(
            {
                "tid": reference.tid,
                "aidKey": reference.aid_key,
                "dirName": reference.dir_name,
                "pid": reference.pid,
                "lou": reference.lou,
                "floorLabel": post["floorLabel"],
                "authorName": post["authorName"],
                "postdate": post["postdate"],
                "occurrenceCount": reference.occurrence_count,
                "html": post["html"],
                "readerUrl": f"/threads?{reader_query}",
            }
        )
    return {
        "items": items,
        "total": len(references),
        "offset": offset,
        "limit": limit,
    }


async def image_usage_replies_route(
    request: Request,
    relative_path: Annotated[str, Query(min_length=1)],
    tid: Annotated[int, Query(gt=0)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_POST_LIMIT)] = 20,
) -> ImageUsageRepliesResult:
    context = _context(request)
    try:
        snapshot = await run_in_threadpool(
            context.image_usage_cache.read,
            context.output_dir,
            refresh=False,
        )
        return await run_in_threadpool(
            _read_image_usage_replies_sync,
            context.output_dir,
            snapshot,
            relative_path,
            tid,
            offset,
            limit,
        )
    except ThreadNotFoundError as error:
        raise WebConflict(str(error)) from error


async def database_schema(
    request: Request,
    db_id: str,
    refresh: bool = False,
) -> DatabaseSchema:
    context = _context(request)
    return await run_in_threadpool(
        context.database_schema_cache.read,
        context.output_dir,
        db_id,
        refresh=refresh,
    )


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
    return await _run_with_bad_request(
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


async def database_row_detail(
    request: Request,
    db_id: str,
    table_name: str,
    rowid: int,
) -> TableRowDetail:
    context = _context(request)
    return await _run_with_bad_request(
        read_table_row_detail,
        context.output_dir,
        db_id,
        table_name,
        rowid,
    )


async def list_clusters(
    request: Request,
    run_id: Annotated[Optional[int], Query()] = None,
    min_size: Annotated[int, Query(ge=1)] = 1,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(gt=0, le=_MAX_CLUSTER_LIMIT)] = 100,
) -> ClustersResult:
    context = _context(request)
    return await run_in_threadpool(
        read_clusters,
        context.output_dir,
        run_id,
        min_size,
        offset,
        limit,
    )


async def cluster_detail(
    request: Request,
    cluster_id: int,
    run_id: Annotated[Optional[int], Query()] = None,
) -> ClusterDetailResult:
    context = _context(request)
    return await run_in_threadpool(
        read_cluster_detail,
        context.output_dir,
        run_id,
        cluster_id,
    )


async def cluster_stats(
    request: Request,
    run_id: Annotated[Optional[int], Query()] = None,
) -> ClusterStatsResult:
    context = _context(request)
    return await run_in_threadpool(
        read_cluster_stats,
        context.output_dir,
        run_id,
    )


async def output_file(
    request: Request,
    relative_path: str,
) -> FileResponse:
    context = _context(request)
    path = await run_in_threadpool(safe_output_file, context.output_dir, relative_path)
    if path is None:
        raise WebNotFound("文件不存在或不允许访问。")
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

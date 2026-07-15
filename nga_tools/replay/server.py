from __future__ import annotations

import asyncio
import json
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from time import perf_counter
from typing import BinaryIO, Literal, TypeAlias
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from nga_tools.console import report_info, report_progress
from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.replay.corpus import ImageReplayEntry, ReplayCorpus, load_replay_corpus
from nga_tools.replay.metrics import ReplayMetrics, ReplayOperation, TrafficKind
from nga_tools.replay.profile import ReplayProfile
from nga_tools.replay.rate_limit import TrafficShaper

DEFAULT_REPLAY_HOST = "127.0.0.1"
DEFAULT_REPLAY_PORT = 8765

ByteSource: TypeAlias = AsyncIterator[bytes]
ResponseKind: TypeAlias = Literal["api", "image"]


def _json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


async def _bytes_source(payload: bytes, chunk_bytes: int) -> ByteSource:
    for start in range(0, len(payload), chunk_bytes):
        yield payload[start : start + chunk_bytes]


async def _file_source(path: Path, chunk_bytes: int) -> ByteSource:
    input_file: BinaryIO = await asyncio.to_thread(path.open, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(input_file.read, chunk_bytes)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(input_file.close)


class ReplayService:
    def __init__(self, corpus: ReplayCorpus, profile: ReplayProfile) -> None:
        self.corpus = corpus
        self.profile = profile
        self.metrics = ReplayMetrics()
        self.api_shaper = TrafficShaper(profile.api)
        self.image_shaper = TrafficShaper(profile.image)

    def _shaper(self, kind: TrafficKind) -> TrafficShaper:
        return self.api_shaper if kind == "api" else self.image_shaper

    @property
    def busy_count(self) -> int:
        return self.api_shaper.busy_count + self.image_shaper.busy_count

    async def shaped_stream(
        self,
        kind: TrafficKind,
        source: ByteSource,
        *,
        status: int,
        floor_map_original: bool = False,
        operation: ReplayOperation | None = None,
    ) -> ByteSource:
        shaper = self._shaper(kind)
        response_bytes = 0
        latency_wait_seconds = 0.0
        bandwidth_wait_seconds = 0.0
        service_started = perf_counter()
        async with shaper.slot():
            self.metrics.begin(
                kind,
                floor_map_original=floor_map_original,
                operation=operation,
            )
            try:
                latency_wait_seconds = await shaper.wait_latency()
                async for chunk in source:
                    bandwidth_wait_seconds += await shaper.wait_for_bytes(len(chunk))
                    response_bytes += len(chunk)
                    yield chunk
            finally:
                self.metrics.finish(
                    kind,
                    status=status,
                    response_bytes=response_bytes,
                    latency_wait_seconds=latency_wait_seconds,
                    bandwidth_wait_seconds=bandwidth_wait_seconds,
                    service_seconds=perf_counter() - service_started,
                )

    def manifest_payload(self) -> dict[str, object]:
        return {
            **self.corpus.manifest.as_dict(),
            "profile_id": self.profile.profile_id,
            "profile": self.profile.as_dict(),
        }

    def reset(self) -> str:
        if self.busy_count or self.metrics.active_count:
            raise RuntimeError("仍有重放请求在途，不能重置。")
        self.api_shaper.reset()
        self.image_shaper.reset()
        return self.metrics.reset()


def _streaming_response(
    service: ReplayService,
    kind: ResponseKind,
    source: ByteSource,
    *,
    status: int,
    content_type: str,
    content_length: int,
    floor_map_original: bool = False,
    operation: ReplayOperation | None = None,
    headers: dict[str, str] | None = None,
) -> StreamingResponse:
    response_headers = {"Content-Length": str(content_length)}
    if headers is not None:
        response_headers.update(headers)
    return StreamingResponse(
        service.shaped_stream(
            kind,
            source,
            status=status,
            floor_map_original=floor_map_original,
            operation=operation,
        ),
        status_code=status,
        media_type=content_type,
        headers=response_headers,
    )


def _api_response(
    service: ReplayService,
    payload: bytes,
    *,
    floor_map_original: bool,
    operation: ReplayOperation | None = None,
) -> StreamingResponse:
    return _streaming_response(
        service,
        "api",
        _bytes_source(payload, service.profile.chunk_bytes),
        status=200,
        content_type="application/json",
        content_length=len(payload),
        floor_map_original=floor_map_original,
        operation=operation,
    )


def _api_error_response(service: ReplayService, message: str) -> StreamingResponse:
    return _api_response(
        service,
        _json_bytes({"code": -1, "msg": message, "result": []}),
        floor_map_original=False,
    )


def _pid_response(
    service: ReplayService,
    payload: bytes,
    *,
    status: int,
    location: str | None = None,
) -> StreamingResponse:
    headers = None if location is None else {"Location": location}
    return _streaming_response(
        service,
        "api",
        _bytes_source(payload, service.profile.chunk_bytes),
        status=status,
        content_type="text/html",
        content_length=len(payload),
        operation="pid_redirect",
        headers=headers,
    )


def _image_response(
    service: ReplayService,
    entry: ImageReplayEntry,
) -> StreamingResponse:
    content_type = mimetypes.guess_type(entry.path.name)[0]
    return _streaming_response(
        service,
        "image",
        _file_source(entry.path, service.profile.chunk_bytes),
        status=200,
        content_type=(
            "application/octet-stream" if content_type is None else content_type
        ),
        content_length=entry.size,
    )


def _missing_image_response(service: ReplayService) -> StreamingResponse:
    payload = b"image not found\n"
    return _streaming_response(
        service,
        "image",
        _bytes_source(payload, service.profile.chunk_bytes),
        status=404,
        content_type="text/plain",
        content_length=len(payload),
    )


def _single_form_value(
    form: dict[str, list[str]],
    key: str,
) -> str | None:
    values = form.get(key)
    if values is None or len(values) != 1:
        return None
    return values[0]


def _positive_form_int(
    form: dict[str, list[str]],
    key: str,
) -> int | None:
    value = _single_form_value(form, key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def create_replay_app(corpus: ReplayCorpus, profile: ReplayProfile) -> FastAPI:
    service = ReplayService(corpus, profile)
    app = FastAPI(title="NGA offline replay server")
    app.state.replay_service = service

    async def health() -> dict[str, object]:
        return {
            "status": "ok",
            "corpus_id": corpus.manifest.corpus_id,
            "profile_id": profile.profile_id,
            "busy_requests": service.busy_count,
        }

    async def manifest() -> dict[str, object]:
        return service.manifest_payload()

    async def metrics() -> dict[str, object]:
        return service.metrics.snapshot()

    async def reset() -> JSONResponse:
        try:
            reset_at = service.reset()
        except RuntimeError as error:
            return JSONResponse(status_code=409, content={"detail": str(error)})
        return JSONResponse(content={"status": "ok", "reset_at": reset_at})

    async def nga_post_list(request: Request) -> StreamingResponse:
        if (
            request.query_params.get("__lib") != "post"
            or request.query_params.get("__act") != "list"
        ):
            return _api_error_response(service, "replay只支持post/list接口")

        try:
            body = (await request.body()).decode("utf-8")
        except UnicodeDecodeError:
            return _api_error_response(service, "请求表单不是UTF-8")
        form = parse_qs(body, keep_blank_values=True, strict_parsing=False)
        tid = _positive_form_int(form, "tid")
        page_number = _positive_form_int(form, "page")
        raw_aid = _single_form_value(form, "authorid")
        if tid is None or page_number is None:
            return _api_error_response(service, "tid或page无效")
        aid: int | None = None
        if raw_aid is not None:
            try:
                aid = int(raw_aid)
            except ValueError:
                return _api_error_response(service, "authorid无效")
            if aid <= 0:
                return _api_error_response(service, "authorid无效")

        replay_page = corpus.page(tid, aid, page_number)
        if replay_page is None:
            aid_text = "all" if aid is None else str(aid)
            return _api_error_response(
                service,
                f"replay语料缺少 tid={tid}, aid={aid_text}, page={page_number}",
            )
        return _api_response(
            service,
            replay_page.payload,
            floor_map_original=replay_page.floor_map_original,
            operation=(
                "original_post_list" if aid is None else "author_post_list"
            ),
        )

    async def nga_pid_redirect(request: Request) -> StreamingResponse:
        raw_pid = request.query_params.get("pid")
        if request.query_params.get("opt") != "128" or raw_pid is None:
            return _pid_response(
                service,
                b"<html><body>invalid pid request</body></html>",
                status=200,
            )
        try:
            pid = int(raw_pid)
        except ValueError:
            pid = 0
        target = corpus.pid_target(pid) if pid > 0 else None
        if target is None:
            return _pid_response(
                service,
                b"<html><body>post not found</body></html>",
                status=200,
            )
        location = (
            f"/read.php?tid={target.tid}&page={target.page_number}"
            f"#pid{pid}Anchor"
        )
        return _pid_response(service, b"", status=302, location=location)

    async def replay_image(request: Request) -> StreamingResponse:
        url = request.query_params.get("url")
        if url is None:
            return _missing_image_response(service)
        normalized_url = url.replace(",", "")
        if not NGA_img_link_verify(normalized_url):
            return _missing_image_response(service)
        entry = corpus.image(normalized_url)
        if entry is None:
            return _missing_image_response(service)
        return _image_response(service, entry)

    app.add_api_route("/__replay__/health", health, methods=["GET"])
    app.add_api_route("/__replay__/manifest", manifest, methods=["GET"])
    app.add_api_route("/__replay__/metrics", metrics, methods=["GET"])
    app.add_api_route("/__replay__/reset", reset, methods=["POST"])
    app.add_api_route("/app_api.php", nga_post_list, methods=["POST"])
    app.add_api_route("/read.php", nga_pid_redirect, methods=["GET"])
    app.add_api_route("/__replay__/image", replay_image, methods=["GET"])
    return app


def load_replay_app(
    *,
    source_output: Path,
    thread_config_path: Path,
    profile: ReplayProfile,
) -> FastAPI:
    """Load a frozen replay corpus and build its ASGI application."""
    report_info(f"正在加载重放语料：{source_output}")

    def update_progress(completed: int, total: int, message: str) -> None:
        report_progress(message, completed=completed, total=total)

    corpus = load_replay_corpus(
        source_output,
        thread_config_path,
        on_progress=update_progress,
    )
    manifest = corpus.manifest
    report_info(
        f"重放语料加载完成：帖子{manifest.thread_count}个，"
        f"内容分页{manifest.archive_content_page_count}页，"
        f"楼层映射原帖{manifest.floor_map_original_thread_count}个/"
        f"{manifest.floor_map_original_page_count}页，"
        f"可用图片映射{manifest.available_image_mapping_count}条。"
    )
    report_info(f"Corpus ID：{manifest.corpus_id}")
    return create_replay_app(corpus, profile)


def serve_replay(
    *,
    source_output: Path,
    thread_config_path: Path,
    profile: ReplayProfile,
    host: str = DEFAULT_REPLAY_HOST,
    port: int = DEFAULT_REPLAY_PORT,
) -> None:
    app = load_replay_app(
        source_output=source_output,
        thread_config_path=thread_config_path,
        profile=profile,
    )
    report_info(f"重放服务：http://{host}:{port}/")
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="warning",
    )

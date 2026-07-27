from __future__ import annotations

from typing import ClassVar

from fastapi import Request
from fastapi.responses import JSONResponse


class WebError(Exception):
    status_code: ClassVar[int] = 500


class WebBadRequest(WebError):
    status_code = 400


class WebNotFound(WebError):
    status_code = 404


class WebConflict(WebError):
    status_code = 409


async def web_error_handler(
    _request: Request,
    exception: Exception,
) -> JSONResponse:
    if isinstance(exception, WebError):
        return JSONResponse(
            {"error": str(exception)},
            status_code=exception.status_code,
        )
    return JSONResponse({"error": "请求失败。"}, status_code=500)

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncGenerator, Generator
from contextlib import asynccontextmanager, contextmanager

from nga_tools.config import get_config

_STATE_LOCK = threading.Lock()
_api_concurrency: int | None = None
_image_concurrency: int | None = None
_api_semaphore: threading.BoundedSemaphore | None = None
_image_semaphore: threading.BoundedSemaphore | None = None


def _validate_positive(name: str, value: int) -> None:
    if value <= 0:
        raise ValueError(f"{name}必须大于0。")


def configure_network_limits(
    *,
    api_concurrency: int,
    image_concurrency: int,
) -> None:
    _validate_positive("api_concurrency", api_concurrency)
    _validate_positive("image_concurrency", image_concurrency)

    from nga_tools.ngaclient.api_runtime import active_api_runtime_capacity

    active_capacity = active_api_runtime_capacity()
    if active_capacity is not None and active_capacity != api_concurrency:
        raise RuntimeError(
            "NGA API运行时活动期间不能修改API并发数："
            f"active={active_capacity}, requested={api_concurrency}"
        )

    global _api_concurrency, _image_concurrency, _api_semaphore, _image_semaphore
    with _STATE_LOCK:
        _api_concurrency = api_concurrency
        _image_concurrency = image_concurrency
        _api_semaphore = threading.BoundedSemaphore(api_concurrency)
        _image_semaphore = threading.BoundedSemaphore(image_concurrency)


def _ensure_configured() -> None:
    global _api_concurrency, _image_concurrency, _api_semaphore, _image_semaphore
    with _STATE_LOCK:
        if _api_semaphore is not None and _image_semaphore is not None:
            return
        app_config = get_config()
        _api_concurrency = app_config.api_concurrency
        _image_concurrency = app_config.image_concurrency
        _api_semaphore = threading.BoundedSemaphore(app_config.api_concurrency)
        _image_semaphore = threading.BoundedSemaphore(app_config.image_concurrency)


def get_api_concurrency() -> int:
    _ensure_configured()
    if _api_concurrency is None:
        raise RuntimeError("API并发限制未初始化。")
    return _api_concurrency


def get_image_concurrency() -> int:
    _ensure_configured()
    if _image_concurrency is None:
        raise RuntimeError("图片下载并发限制未初始化。")
    return _image_concurrency


def _api_request_semaphore() -> threading.BoundedSemaphore:
    _ensure_configured()
    if _api_semaphore is None:
        raise RuntimeError("API并发限制未初始化。")
    return _api_semaphore


def _image_download_semaphore() -> threading.BoundedSemaphore:
    _ensure_configured()
    if _image_semaphore is None:
        raise RuntimeError("图片下载并发限制未初始化。")
    return _image_semaphore


@contextmanager
def api_request_slot() -> Generator[None]:
    semaphore = _api_request_semaphore()
    semaphore.acquire()
    try:
        yield
    finally:
        semaphore.release()


@asynccontextmanager
async def image_download_slot() -> AsyncGenerator[None]:
    semaphore = _image_download_semaphore()
    await asyncio.to_thread(semaphore.acquire)
    try:
        yield
    finally:
        semaphore.release()

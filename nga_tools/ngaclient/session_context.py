from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar

import requests

_CURRENT_SHARED_API_SESSION: ContextVar[requests.Session | None] = ContextVar(
    "nga_tools_shared_api_session",
    default=None,
)


def current_shared_api_session() -> requests.Session | None:
    return _CURRENT_SHARED_API_SESSION.get()


@contextmanager
def use_shared_api_session(session: requests.Session) -> Generator[None]:
    token = _CURRENT_SHARED_API_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_SHARED_API_SESSION.reset(token)

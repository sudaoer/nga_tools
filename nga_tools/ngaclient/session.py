from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Lock, local

import requests

from nga_tools.config import get_config


def create_api_session() -> requests.Session:
    app_config = get_config()
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": app_config.user_agent,
            "Cookie": (
                f"ngaPassportUid={app_config.nga_passport_uid}; "
                f"ngaPassportCid={app_config.nga_passport_cid};"
            ),
        }
    )
    return session


_CURRENT_API_SESSION: ContextVar[requests.Session | None] = ContextVar(
    "nga_tools_current_api_session",
    default=None,
)


def current_api_session() -> requests.Session | None:
    return _CURRENT_API_SESSION.get()


@contextmanager
def use_api_session(session: requests.Session) -> Generator[None]:
    token = _CURRENT_API_SESSION.set(session)
    try:
        yield
    finally:
        _CURRENT_API_SESSION.reset(token)


class _ThreadSessionState(local):
    session: requests.Session | None

    def __init__(self) -> None:
        self.session = None


class ThreadLocalAPISessionPool:
    """Create one reusable NGA API session for each participating thread."""

    def __init__(self) -> None:
        self._thread_state = _ThreadSessionState()
        self._sessions: list[requests.Session] = []
        self._lock = Lock()
        self._closed = False

    def session(self) -> requests.Session:
        existing_session = self._thread_state.session
        if existing_session is not None:
            return existing_session

        session = create_api_session()
        with self._lock:
            if self._closed:
                session.close()
                raise RuntimeError("NGA API session池已经关闭。")
            self._sessions.append(session)
        self._thread_state.session = session
        return session

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = tuple(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    def __enter__(self) -> ThreadLocalAPISessionPool:
        return self

    def __exit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        self.close()

from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from requests import PreparedRequest, Response
from requests import _types as requests_types  # pyright: ignore[reportPrivateUsage]
from requests.adapters import HTTPAdapter

from nga_tools.core.nga_images import NGA_img_link_verify


class ReplayOfflineError(RuntimeError):
    pass


def _normalized_server_url(server_url: str) -> tuple[str, str]:
    parsed = urlsplit(server_url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError("server-url必须是无路径、查询或凭据的HTTP(S) origin。")
    host = parsed.hostname.lower()
    default_port = 80 if parsed.scheme == "http" else 443
    port = default_port if parsed.port is None else parsed.port
    origin = f"{parsed.scheme.lower()}://{host}:{port}"
    display_host = f"[{host}]" if ":" in host else host
    base_url = f"{parsed.scheme.lower()}://{display_host}"
    if port != default_port:
        base_url += f":{port}"
    return base_url, origin


def normalized_server_url(server_url: str) -> str:
    return _normalized_server_url(server_url)[0]


@dataclass(frozen=True, slots=True)
class ReplayNetworkPolicy:
    server_url: str
    server_origin: str

    @classmethod
    def create(cls, server_url: str) -> ReplayNetworkPolicy:
        normalized, origin = _normalized_server_url(server_url)
        return cls(normalized, origin)

    def assert_allowed(self, request_url: str) -> None:
        try:
            _, request_origin = _normalized_server_url(
                _origin_url_for_request(request_url)
            )
        except ValueError as error:
            raise ReplayOfflineError(f"重放请求URL无效：{request_url}") from error
        if request_origin != self.server_origin:
            raise ReplayOfflineError(
                "重放离线保护拒绝非服务端请求："
                f"{request_url}（允许origin：{self.server_url}）"
            )

    def image_request_url(self, logical_url: str) -> str:
        if not NGA_img_link_verify(logical_url):
            raise ReplayOfflineError(f"重放图片逻辑URL不是NGA图片：{logical_url}")
        return f"{self.server_url}/__replay__/image?{urlencode({'url': logical_url})}"


def _origin_url_for_request(request_url: str) -> str:
    parsed = urlsplit(request_url)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("request URL is not absolute")
    host = parsed.hostname
    if host is None:
        raise ValueError("request URL has no host")
    display_host = f"[{host}]" if ":" in host else host
    port = "" if parsed.port is None else f":{parsed.port}"
    return f"{parsed.scheme}://{display_host}{port}"


_POLICY_LOCK = threading.RLock()
_active_policy: ReplayNetworkPolicy | None = None


def current_replay_network_policy() -> ReplayNetworkPolicy | None:
    with _POLICY_LOCK:
        return _active_policy


@contextmanager
def use_replay_network_policy(server_url: str) -> Generator[ReplayNetworkPolicy]:
    global _active_policy
    policy = ReplayNetworkPolicy.create(server_url)
    with _POLICY_LOCK:
        if _active_policy is not None:
            raise RuntimeError("重放离线网络保护已经启用。")
        _active_policy = policy
    try:
        yield policy
    finally:
        with _POLICY_LOCK:
            _active_policy = None


def assert_replay_request_allowed(request_url: str) -> None:
    policy = current_replay_network_policy()
    if policy is not None:
        policy.assert_allowed(request_url)


def image_request_url(logical_url: str) -> str:
    policy = current_replay_network_policy()
    return logical_url if policy is None else policy.image_request_url(logical_url)


class ReplayGuardHTTPAdapter(HTTPAdapter):
    def send(
        self,
        request: PreparedRequest,
        stream: bool = False,
        timeout: requests_types.TimeoutType = None,
        verify: requests_types.VerifyType = True,
        cert: requests_types.CertType = None,
        proxies: dict[str, str] | None = None,
    ) -> Response:
        if request.url is None:
            raise ReplayOfflineError("重放请求缺少URL。")
        assert_replay_request_allowed(request.url)
        return super().send(
            request,
            stream=stream,
            timeout=timeout,
            verify=verify,
            cert=cert,
            proxies=proxies,
        )

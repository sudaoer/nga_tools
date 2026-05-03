# 管理api key的工具，有add、list、delete子命令
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import ipaddress

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_FILE_PATH = _PROJECT_ROOT / "config.json"
DEFAULT_SELF_HOSTED_MAX_CONCURRENT_GENERATIONS = 4
DEFAULT_EXTERNAL_INITIAL_MAX_CONCURRENT = 16


"""
示例：
{
    "providers": [
        {
            "provider": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-************",
            "model_names": ["deepseek-chat", "deepseek-reasoner"]
        }
    ]
}
"""


def load_config() -> dict[str, Any]:
    if not _CONFIG_FILE_PATH.exists():
        with open(_CONFIG_FILE_PATH, "w") as f:
            json.dump({"providers": []}, f)
    with open(_CONFIG_FILE_PATH, "r") as f:
        config = json.load(f)
    return config


def save_config(config: dict[str, Any]) -> None:
    with open(_CONFIG_FILE_PATH, "w") as f:
        json.dump(config, f, indent=4)


def get_key(endpoint: str) -> str:
    # http开头的接口大概率是本地部署的，默认不需要key
    if endpoint.startswith("http://"):
        return ""
    provider = get_provider_for_baseurl(endpoint)
    if provider is not None:
        key = provider.get("api_key", "")
        if key:
            return key
    raise ValueError(f"No key found for endpoint: {endpoint}")


def get_model_baseurl(model_name: str) -> str:
    provider = get_provider_for_model(model_name)
    return provider["base_url"]


def get_provider_for_model(model_name: str) -> dict[str, Any]:
    config = load_config()
    providers = config.get("providers", [])
    for provider in providers:
        if model_name in provider["model_names"]:
            return provider
    raise ValueError(f"No provider found for model: {model_name}")


def get_provider_for_baseurl(base_url: str) -> dict[str, Any] | None:
    config = load_config()
    providers = config.get("providers", [])
    normalized = normalize_base_url(base_url)
    for provider in providers:
        provider_base_url = provider.get("base_url")
        if (
            isinstance(provider_base_url, str)
            and normalize_base_url(provider_base_url) == normalized
        ):
            return provider
    return None


def normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def is_self_hosted_endpoint(base_url: str) -> bool:
    parsed = urlparse(base_url)
    hostname = parsed.hostname
    if hostname is None:
        return False
    if hostname in {"localhost", "host.docker.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(hostname)
        return ip.is_loopback or ip.is_private
    except ValueError:
        pass
    if hostname.endswith(".local"):
        return True
    return parsed.scheme == "http"


def get_max_concurrent_generations(
    base_url: str,
    model_name: str | None = None,
) -> int | None:
    provider: dict[str, Any] | None = None
    if model_name is not None:
        try:
            provider = get_provider_for_model(model_name)
        except ValueError:
            provider = None
    if provider is None:
        provider = get_provider_for_baseurl(base_url)

    if provider is not None:
        configured_limit = provider.get("max_concurrent_generations")
        if isinstance(configured_limit, int):
            if configured_limit <= 0:
                return None
            return configured_limit

    if is_self_hosted_endpoint(base_url):
        return DEFAULT_SELF_HOSTED_MAX_CONCURRENT_GENERATIONS
    return DEFAULT_EXTERNAL_INITIAL_MAX_CONCURRENT


def get_baseurl_key(model_name: str) -> tuple[str, str]:
    provider = get_provider_for_model(model_name)
    base_url = provider["base_url"]
    # http 开头大概率是本地部署，不需要 key
    if base_url.startswith("http://"):
        return base_url, ""
    key = provider.get("api_key", "")
    if not key:
        raise ValueError(f"No api_key configured for provider: {provider.get('provider', base_url)}")
    return base_url, key


# ── 通用配置 getter ──────────────────────────────────────────────


def get_proxy_port() -> int | None:
    """返回代理端口号，未配置时返回 None。"""
    config = load_config()
    port = config.get("proxy", {}).get("port")
    if isinstance(port, int) and port > 0:
        return port
    return None


def get_no_proxy() -> str:
    config = load_config()
    return config.get("proxy", {}).get("no_proxy", "127.0.0.1,localhost")


def get_sandbox_docker_image_prefix() -> str:
    config = load_config()
    return config.get("sandbox", {}).get("docker_image_prefix", "sandbox")


def get_sandbox_mem_limit() -> str:
    config = load_config()
    return config.get("sandbox", {}).get("mem_limit", "2g")


def get_sandbox_max_pool_size() -> int:
    config = load_config()
    return config.get("sandbox", {}).get("max_pool_size", 128)

DEFAULT_WORKER_NUM = 8
def get_default_workers() -> int:
    config = load_config()
    workers = config.get("default_workers")
    if isinstance(workers, int) and workers > 0:
        return workers
    return DEFAULT_WORKER_NUM

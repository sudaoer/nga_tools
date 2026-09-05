from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


NGA_ATTACHMENT_HOSTS: frozenset[str] = frozenset(
    {
        "img.nga.178.com",
        "img.nga.cn",
    }
)

NGA_LEGACY_ATTACHMENT_HOSTS: frozenset[str] = frozenset(
    {
        "img.nga.178.com",
    }
)


def is_nga_attachment_host(netloc: str) -> bool:
    return netloc.lower() in NGA_ATTACHMENT_HOSTS


def is_nga_legacy_attachment_host(netloc: str) -> bool:
    return netloc.lower() in NGA_LEGACY_ATTACHMENT_HOSTS


def attachment_url_alias(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not is_nga_attachment_host(
        parsed.netloc
    ):
        return None
    other_host = next(
        (
            host
            for host in NGA_ATTACHMENT_HOSTS
            if host != parsed.netloc.lower()
        ),
        None,
    )
    if other_host is None:
        return None
    return urlunsplit(("https", other_host, parsed.path, parsed.query, ""))


def attachment_url_identity(url: str) -> str | None:
    parsed = urlsplit(url)
    if parsed.scheme.lower() != "https" or not is_nga_attachment_host(
        parsed.netloc
    ):
        return None
    if parsed.query:
        return f"{parsed.path}?{parsed.query}"
    return parsed.path

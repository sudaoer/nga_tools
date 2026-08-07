from __future__ import annotations

import datetime
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from nga_tools.core.nga_attachment import is_nga_attachment_host

_NGA_AUDIO_PATH_RE = re.compile(
    r"^/attachments/(mon_(\d{4})(\d{2}))/(\d{2})/"
    r"([A-Za-z0-9][A-Za-z0-9_-]*\.mp3)$",
    re.IGNORECASE,
)


def normalize_nga_audio_url(value: str) -> str | None:
    raw_url = unescape(value).strip()
    parsed = urlsplit(raw_url)
    if (
        parsed.scheme.lower() != "https"
        or not is_nga_attachment_host(parsed.netloc)
        or parsed.fragment
    ):
        return None

    path_match = _NGA_AUDIO_PATH_RE.fullmatch(parsed.path)
    if path_match is None:
        return None
    year = int(path_match.group(2))
    month = int(path_match.group(3))
    day = int(path_match.group(4))
    try:
        datetime.date(year, month, day)
    except ValueError:
        return None

    return urlunsplit(
        (
            "https",
            parsed.netloc.lower(),
            parsed.path,
            parsed.query,
            "",
        )
    )


class _AudioSourceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def _handle_audio(self, attrs: list[tuple[str, str | None]]) -> None:
        for name, value in attrs:
            if name.lower() != "src" or value is None:
                continue
            normalized_url = normalize_nga_audio_url(value)
            if normalized_url is not None:
                self.urls.append(normalized_url)
            return

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "audio":
            self._handle_audio(attrs)

    def handle_startendtag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() == "audio":
            self._handle_audio(attrs)


def extract_nga_audio_urls(content: str) -> tuple[str, ...]:
    if "<audio" not in content.lower():
        return ()
    parser = _AudioSourceParser()
    parser.feed(content)
    parser.close()
    return tuple(parser.urls)

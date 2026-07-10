from __future__ import annotations

import datetime
import re
from urllib.parse import urlsplit

_NGA_IMAGE_FILENAME_RE = re.compile(
    r"^[A-Za-z0-9-][A-Za-z0-9_-]*"
    r"\.(?:jpg|jpeg|png|gif|webp|avif|heic|heif|jxl)"
    r"(?:\.(?:thumb|thumb_s|thumb_ss|medium)\.jpg)?$",
    re.IGNORECASE,
)
_NGA_IMAGE_PATH_RE = re.compile(
    r"^/attachments/(mon_(\d{4})(\d{2}))/(\d{2})/([^/]+)$"
)


def NGA_img_link_verify(url: str) -> bool:
    parsed_url = urlsplit(url)
    if parsed_url.scheme != "https" or parsed_url.netloc != "img.nga.178.com":
        return False
    if parsed_url.fragment:
        return False

    path_match = _NGA_IMAGE_PATH_RE.fullmatch(parsed_url.path)
    if path_match is None:
        return False

    year = int(path_match.group(2))
    month = int(path_match.group(3))
    day = int(path_match.group(4))
    filename = path_match.group(5)
    try:
        datetime.date(year, month, day)
    except ValueError:
        return False

    return bool(_NGA_IMAGE_FILENAME_RE.fullmatch(filename))

from __future__ import annotations

from typing import Final

from compression import zstd


ZSTD_CONTENT_COMPRESSION_LEVEL: Final[int] = 3


class ContentCodecError(ValueError):
    """Raised when a stored post body cannot be decoded."""


def encode_content(content: str) -> bytes:
    return zstd.compress(
        content.encode("utf-8"),
        level=ZSTD_CONTENT_COMPRESSION_LEVEL,
    )


def decode_content(value: object, *, source: str = "帖子正文") -> str:
    if isinstance(value, str):
        # Archives created before the content migration store UTF-8 text.
        return value
    if not isinstance(value, bytes):
        raise ContentCodecError(f"{source}存储类型无效：{type(value).__name__}")
    try:
        raw_content = zstd.decompress(value)
    except zstd.ZstdError as error:
        raise ContentCodecError(f"{source}不是有效zstd正文：{error}") from error
    try:
        return raw_content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContentCodecError(f"{source}不是有效UTF-8正文：{error}") from error

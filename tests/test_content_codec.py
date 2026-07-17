from __future__ import annotations

import pytest

from nga_tools.backup.content_codec import (
    ContentCodecError,
    decode_content,
    encode_content,
)


@pytest.mark.parametrize(
    "content",
    ["", "普通中文正文", "[quote]<b>HTML</b>[/quote]\x00尾部"],
)
def test_content_codec_round_trip(content: str) -> None:
    encoded = encode_content(content)

    assert isinstance(encoded, bytes)
    assert decode_content(encoded) == content


def test_content_codec_rejects_text() -> None:
    with pytest.raises(ContentCodecError, match="存储类型无效"):
        decode_content("legacy content")


def test_content_codec_rejects_invalid_zstd() -> None:
    with pytest.raises(ContentCodecError, match="不是有效zstd正文"):
        decode_content(b"not zstd")

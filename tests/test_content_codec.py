from __future__ import annotations

import pytest

from nga_tools.backup.content_codec import decode_content, encode_content


@pytest.mark.parametrize(
    "content",
    ["", "普通中文正文", "[quote]<b>HTML</b>[/quote]\x00尾部"],
)
def test_content_codec_round_trip(content: str) -> None:
    encoded = encode_content(content)

    assert isinstance(encoded, bytes)
    assert decode_content(encoded) == content

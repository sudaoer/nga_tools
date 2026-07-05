from __future__ import annotations

import sys
import unicodedata
from typing import TextIO


def _display_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        if unicodedata.east_asian_width(char) in {"F", "W"}:
            width += 2
        else:
            width += 1
    return width


class InlineProgress:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream = stream if stream is not None else sys.stdout
        self._last_width = 0
        self._active = False

    def update(self, message: str) -> None:
        message_width = _display_width(message)
        padding_width = max(0, self._last_width - message_width)
        self._stream.write("\r" + message + " " * padding_width)
        if padding_width:
            self._stream.write("\r" + message)
        self._stream.flush()
        self._last_width = message_width
        self._active = True

    def finish(self) -> None:
        if not self._active:
            return
        self._stream.write("\n")
        self._stream.flush()
        self._last_width = 0
        self._active = False

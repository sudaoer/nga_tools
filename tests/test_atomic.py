from __future__ import annotations

from pathlib import Path

import pytest

from nga_tools.core.atomic import open_text_atomically, write_text_atomically


class AtomicFileWriteTest:
    def test_write_text_atomically_replaces_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")

        write_text_atomically(path, "new")

        assert path.read_text(encoding="utf-8") == "new"

    def test_open_text_atomically_keeps_existing_file_on_error(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "threads.jsonl"
        path.write_text("old\n", encoding="utf-8")

        with pytest.raises(RuntimeError, match="stop"):
            with open_text_atomically(path) as output_file:
                output_file.write("partial\n")
                raise RuntimeError("stop")

        assert path.read_text(encoding="utf-8") == "old\n"
        assert list(tmp_path.glob(".threads.jsonl.*.tmp")) == []

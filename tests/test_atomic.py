from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from nga_tools.core.atomic import (
    replace_file_atomically,
    write_text_atomically,
)


class AtomicFileWriteTest:
    def test_write_text_atomically_replaces_existing_file(self, tmp_path: Path) -> None:
        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")

        write_text_atomically(path, "new")

        assert path.read_text(encoding="utf-8") == "new"

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable")
    def test_write_text_atomically_preserves_existing_file_mode(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "state.json"
        path.write_text("old", encoding="utf-8")
        path.chmod(0o640)

        write_text_atomically(path, "new")

        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    @pytest.mark.skipif(os.name == "nt", reason="POSIX umask is not portable")
    def test_write_text_atomically_uses_process_umask_for_new_file(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "state.json"
        old_umask = os.umask(0o027)
        try:
            write_text_atomically(path, "new")
        finally:
            os.umask(old_umask)

        assert stat.S_IMODE(path.stat().st_mode) == 0o640

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not portable")
    def test_replace_file_atomically_preserves_target_mode_after_copy2(
        self,
        tmp_path: Path,
    ) -> None:
        source_path = tmp_path / "source.txt"
        target_path = tmp_path / "target.txt"
        source_path.write_text("source", encoding="utf-8")
        target_path.write_text("target", encoding="utf-8")
        source_path.chmod(0o600)
        target_path.chmod(0o644)

        replace_file_atomically(source_path, target_path, move_source=False)

        assert target_path.read_text(encoding="utf-8") == "source"
        assert source_path.exists()
        assert stat.S_IMODE(target_path.stat().st_mode) == 0o644

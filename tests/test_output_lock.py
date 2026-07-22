from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from nga_tools.cli.dispatch import dispatch_command
from nga_tools.cli.schema import COMMANDS
from nga_tools.core.output_lock import (
    ThreadOutputLockError,
    output_root_lock_path,
    use_output_root_lock,
    use_thread_output_lock,
)


def test_thread_output_lock_fails_fast_for_same_thread() -> None:
    with use_thread_output_lock(123, 456):
        with pytest.raises(ThreadOutputLockError, match="输出目录正在被另一个任务使用"):
            with use_thread_output_lock(123, 456):
                pass


def test_thread_output_lock_allows_different_threads() -> None:
    with use_thread_output_lock(123, 456):
        with use_thread_output_lock(123, None):
            pass


def test_output_root_lock_fails_fast_for_same_root(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    assert output_root_lock_path(output_root) == (
        output_root / ".nga_tools-output.lock"
    )
    with use_output_root_lock(output_root):
        with pytest.raises(ThreadOutputLockError):
            with use_output_root_lock(output_root):
                pass


def test_mutating_command_dispatch_holds_output_root_lock(tmp_path: Path) -> None:
    output_root = tmp_path / "output"

    def handler(_args: dict[str, object]) -> None:
        with pytest.raises(ThreadOutputLockError):
            with use_output_root_lock(output_root):
                pass

    action = COMMANDS["backup"]["all"]
    with (
        patch.dict(action, {"handler": handler}),
        patch(
            "nga_tools.cli.dispatch.get_config",
            return_value=SimpleNamespace(output_dir=str(output_root)),
        ),
    ):
        dispatch_command({"command": "backup", "action": "all"})

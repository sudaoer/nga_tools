from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nga_tools.timing import record_timing, time_section, use_timing_log


class TimingLogTest:
    def test_timing_log_records_sections_and_overwrites_file(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "thread" / "timing.log"
            log_path.parent.mkdir()
            log_path.write_text("旧日志\n", encoding="utf-8")

            with use_timing_log(
                log_path,
                task_name="backup sub",
                target="tid=101, aid=all",
            ):
                record_timing("固定阶段", 1.23456)
                with time_section("即时阶段"):
                    pass

            log_text = log_path.read_text(encoding="utf-8")

        assert "旧日志" not in log_text
        assert "任务：backup sub\n" in log_text
        assert "目标：tid=101, aid=all\n" in log_text
        assert "阶段：固定阶段，耗时：1.235s\n" in log_text
        assert "阶段：即时阶段，耗时：" in log_text
        assert "总耗时：" in log_text
        assert "状态：完成" in log_text

    def test_disabled_timing_log_does_not_create_file(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "thread" / "timing.log"

            with use_timing_log(
                log_path,
                task_name="backup sub",
                enabled=False,
            ):
                record_timing("不会写入", 1)

            assert not log_path.exists()

    def test_timing_log_records_failure_status(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"

            with pytest.raises(RuntimeError):
                with use_timing_log(log_path, task_name="backup sub"):
                    record_timing("失败前", 0.5)
                    raise RuntimeError("boom")

            log_text = log_path.read_text(encoding="utf-8")

        assert "阶段：失败前，耗时：0.500s\n" in log_text
        assert "状态：失败" in log_text

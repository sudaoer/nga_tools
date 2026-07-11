from __future__ import annotations

import re
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from nga_tools.timing import (
    record_timing,
    record_timing_metric,
    time_section,
    use_timing_log,
)


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
                record_timing_metric("缓存命中数", 42)
                with time_section("即时阶段"):
                    in_progress_text = log_path.read_text(encoding="utf-8")

            log_text = log_path.read_text(encoding="utf-8")

        assert "旧日志" not in log_text
        assert "任务：backup sub\n" in log_text
        assert "目标：tid=101, aid=all\n" in log_text
        assert "阶段：固定阶段，耗时：1.235s\n" in log_text
        assert "指标：缓存命中数，值：42\n" in log_text
        assert "阶段：即时阶段，开始时间：" in in_progress_text
        assert "阶段：即时阶段，结束时间：" not in in_progress_text
        assert "阶段：即时阶段，结束时间：" in log_text
        assert "耗时：" in log_text
        assert "状态：完成" in log_text
        assert re.search(
            r"阶段：即时阶段，开始时间："
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}[+-]\d{2}:\d{2}",
            log_text,
        )
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
                record_timing_metric("不会写入的指标", 1)

            assert not log_path.exists()

    def test_timing_log_records_failure_status(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"

            with pytest.raises(RuntimeError):
                with use_timing_log(log_path, task_name="backup sub"):
                    record_timing("失败前", 0.5)
                    with time_section("失败阶段"):
                        raise RuntimeError("boom")

            log_text = log_path.read_text(encoding="utf-8")

        assert "阶段：失败前，耗时：0.500s\n" in log_text
        assert "阶段：失败阶段，开始时间：" in log_text
        assert "阶段：失败阶段，结束时间：" in log_text
        assert "状态：失败" in log_text
        assert re.search(r"总耗时：.+，状态：失败\n", log_text)

    def test_nested_sections_record_start_and_end_in_execution_order(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"

            with use_timing_log(log_path, task_name="backup sub"):
                with time_section("父阶段"):
                    with time_section("子阶段"):
                        pass

            stage_lines = [
                line
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("阶段：")
            ]

        assert stage_lines[0].startswith("阶段：父阶段，开始时间：")
        assert stage_lines[1].startswith("阶段：子阶段，开始时间：")
        assert stage_lines[2].startswith("阶段：子阶段，结束时间：")
        assert stage_lines[3].startswith("阶段：父阶段，结束时间：")

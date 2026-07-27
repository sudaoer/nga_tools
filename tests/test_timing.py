from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from nga_tools.forum.timing import (
    ForumSyncTimingCollector,
    ForumSyncTimingSnapshot,
)
from nga_tools.timing import (
    TimingSectionRecord,
    TimingSnapshot,
    _new_log_path,
    record_timing,
    record_timing_label,
    record_timing_metric,
    time_section,
    use_timing_log,
    write_batch_timing_summary,
)


class TimingLogTest:
    def test_forum_sync_timing_collector_accumulates_phases_and_counts(
        self,
    ) -> None:
        ticks = iter((0.0, 1.0, 3.0, 4.0, 7.0, 10.0))
        collector = ForumSyncTimingCollector(clock=lambda: next(ticks))

        with collector.measure("setup"):
            pass
        with collector.measure("setup"):
            pass
        collector.record_forum_page_request_attempt()
        collector.record_successful_forum_page(35)
        collector.record_rate_limit_retry()
        collector.record_scanned_threads(100)
        collector.record_author_page_request()
        collector.record_config_saved()

        snapshot = collector.snapshot()

        assert snapshot.total_seconds == 10.0
        assert snapshot.setup_seconds == 5.0
        assert snapshot.successful_page_count == 1
        assert snapshot.forum_page_request_attempt_count == 1
        assert snapshot.rate_limit_retry_count == 1
        assert snapshot.fetched_thread_count == 35
        assert snapshot.scanned_thread_count == 100
        assert snapshot.author_page_request_count == 1
        assert snapshot.config_saved is True

    def test_timing_log_records_sections_without_overwriting_legacy_file(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "thread" / "timing.log"
            log_path.parent.mkdir()
            log_path.write_text("旧日志\n", encoding="utf-8")

            with use_timing_log(
                log_path,
                task_name="backup sub",
                target="tid=101, aid=all",
            ) as timing_log:
                assert timing_log is not None
                record_timing("固定阶段", 1.23456)
                record_timing_metric("缓存命中数", 42)
                with time_section("即时阶段"):
                    in_progress_text = timing_log.path.read_text(encoding="utf-8")

            log_text = timing_log.path.read_text(encoding="utf-8")
            legacy_text = log_path.read_text(encoding="utf-8")

        assert legacy_text == "旧日志\n"
        assert re.fullmatch(r"timing-\d{8}-\d{6}(?:-\d+)?\.log", timing_log.path.name)
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

            timing_paths = list(log_path.parent.glob("timing-*.log"))
            assert len(timing_paths) == 1
            log_text = timing_paths[0].read_text(encoding="utf-8")

        assert "阶段：失败前，耗时：0.500s\n" in log_text
        assert "阶段：失败阶段，开始时间：" in log_text
        assert "阶段：失败阶段，结束时间：" in log_text
        assert "状态：失败" in log_text
        assert re.search(r"总耗时：.+，状态：失败\n", log_text)

    def test_nested_sections_record_start_and_end_in_execution_order(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"

            with use_timing_log(log_path, task_name="backup sub") as timing_log:
                assert timing_log is not None
                with time_section("父阶段"):
                    with time_section("子阶段"):
                        pass

            stage_lines = [
                line
                for line in timing_log.path.read_text(encoding="utf-8").splitlines()
                if line.lstrip().startswith("阶段：")
            ]

        assert stage_lines[0].startswith("阶段：父阶段，开始时间：")
        assert stage_lines[1].startswith("  阶段：子阶段，开始时间：")
        assert stage_lines[2].startswith("  阶段：子阶段，结束时间：")
        assert stage_lines[3].startswith("阶段：父阶段，结束时间：")

    def test_metrics_and_labels_indent_to_current_section_depth(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"

            with use_timing_log(log_path, task_name="backup sub") as timing_log:
                assert timing_log is not None
                record_timing_metric("顶层指标", 1)
                with time_section("父阶段"):
                    record_timing_metric("嵌套指标", 2)
                    record_timing_label("嵌套标签", "hit")
                    with time_section("子阶段"):
                        record_timing_metric("深层指标", 3)

            log_text = timing_log.path.read_text(encoding="utf-8")

        assert "指标：顶层指标，值：1\n" in log_text
        assert "  指标：嵌套指标，值：2\n" in log_text
        assert "  标签：嵌套标签，值：hit\n" in log_text
        assert "    指标：深层指标，值：3\n" in log_text

    def test_timing_log_keeps_all_files_within_retention_days(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"
            log_path.write_text("旧日志\n", encoding="utf-8")

            created_paths: list[Path] = []
            for _ in range(8):
                with use_timing_log(
                    log_path,
                    task_name="backup sub",
                    retention_days=7,
                ) as timing_log:
                    assert timing_log is not None
                    created_paths.append(timing_log.path)

            retained_paths = sorted(log_path.parent.glob("timing-*.log"))
            assert len(retained_paths) == 8
            assert log_path.exists()
            assert all(path.exists() for path in created_paths)

    def test_timing_log_prunes_by_calendar_date(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"
            today = datetime.now().astimezone()
            recent_path = log_path.parent / (
                f"timing-{(today - timedelta(days=6)).strftime('%Y%m%d')}-120000.log"
            )
            stale_path = log_path.parent / (
                f"timing-{(today - timedelta(days=7)).strftime('%Y%m%d')}-120000.log"
            )
            legacy_path = log_path.parent / "timing-20200101T120000000000.log"
            unrelated_path = log_path.parent / "timing-not-a-date.log"
            for candidate in (recent_path, stale_path, legacy_path, unrelated_path):
                candidate.write_text("历史日志\n", encoding="utf-8")
            log_path.write_text("旧固定名日志\n", encoding="utf-8")

            with use_timing_log(
                log_path,
                task_name="backup sub",
                retention_days=7,
            ):
                pass

            assert recent_path.exists()
            assert not stale_path.exists()
            assert not legacy_path.exists()
            assert unrelated_path.exists()
            assert log_path.exists()

    def test_new_log_path_uses_numeric_suffix_for_same_second_collision(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            base_path = Path(temp_dir_name) / "timing.log"
            started_at = datetime(2026, 7, 26, 12, 34, 56)

            first_path = _new_log_path(base_path, started_at)
            first_path.touch()
            second_path = _new_log_path(base_path, started_at)

        assert first_path.name == "timing-20260726-123456.log"
        assert second_path.name == "timing-20260726-123456-2.log"

    def test_timing_log_writes_commit_id_when_available(self) -> None:
        commit_id = "a" * 40
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"
            with (
                patch("nga_tools.timing.git_commit_id", return_value=commit_id),
                use_timing_log(log_path, task_name="backup sub") as timing_log,
            ):
                assert timing_log is not None

            log_text = timing_log.path.read_text(encoding="utf-8")

        assert f"Commit ID：{commit_id}\n" in log_text

    def test_timing_log_omits_commit_id_when_unavailable(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            log_path = Path(temp_dir_name) / "timing.log"
            with (
                patch("nga_tools.timing.git_commit_id", return_value=None),
                use_timing_log(log_path, task_name="backup sub") as timing_log,
            ):
                assert timing_log is not None

            log_text = timing_log.path.read_text(encoding="utf-8")

        assert "Commit ID：" not in log_text

    def test_batch_timing_summary_is_versioned_and_keeps_recent_files(self) -> None:
        commit_id = "b" * 40
        with TemporaryDirectory() as temp_dir_name:
            batch_path = Path(temp_dir_name) / "batch_timing.log"
            batch_path.write_text("旧汇总\n", encoding="utf-8")
            started_at = datetime.now().astimezone()

            with patch(
                "nga_tools.timing.git_commit_id",
                return_value=commit_id,
            ):
                for index in range(6):
                    write_batch_timing_summary(
                        batch_path,
                        task_name="backup sub --all-threads",
                        started_at=started_at + timedelta(seconds=index),
                        wall_seconds=1.0,
                        total_threads=0,
                        snapshots=(),
                        thread_failure_categories=Counter(),
                    )

            retained_paths = sorted(
                batch_path.parent.glob("batch_timing-*.log")
            )
            retained_texts = [
                path.read_text(encoding="utf-8") for path in retained_paths
            ]

            assert len(retained_paths) == 6
            assert batch_path.exists()
            assert all(
                re.fullmatch(
                    r"batch_timing-\d{8}-\d{6}(?:-\d+)?\.log",
                    path.name,
                )
                for path in retained_paths
            )
            assert all(
                f"Commit ID：{commit_id}\n" in text
                for text in retained_texts
            )

    def test_batch_timing_summary_reports_abnormal_thread_status(self) -> None:
        started_at = datetime.now().astimezone()

        with TemporaryDirectory() as temp_dir_name:
            output_path = write_batch_timing_summary(
                Path(temp_dir_name) / "batch_timing.log",
                task_name="backup sub",
                started_at=started_at,
                wall_seconds=1.0,
                total_threads=3,
                snapshots=(),
                thread_failure_categories=Counter(),
                expected_thread_failure_categories=Counter({"状态异常": 2}),
            )
            summary = output_path.read_text(encoding="utf-8")

        assert "帖子：总数3，成功1，状态异常2，失败0" in summary
        assert "状态：完成（含状态异常）" in summary
        assert "状态异常：2" in summary
        assert "- 状态异常: 2" in summary
        assert "隐藏跳过" not in summary

    def test_batch_timing_summary_reports_tail_and_slowest_targets(self) -> None:
        started_at = datetime.now().astimezone()
        snapshots = tuple(
            TimingSnapshot(
                task_name="backup auto",
                target=f"target-{index}",
                started_at=started_at,
                elapsed_seconds=elapsed,
                status="完成",
                sections=(
                    TimingSectionRecord("共享阶段", elapsed / 2, "完成"),
                    TimingSectionRecord("共享阶段", elapsed / 2, "完成"),
                ),
                metrics=(),
                labels=(("图片引用处理模式", "delta"),),
            )
            for index, elapsed in enumerate((1.0, 2.0, 3.0, 4.0, 100.0), start=1)
        )

        with TemporaryDirectory() as temp_dir_name:
            batch_path = Path(temp_dir_name) / "batch_timing.log"
            output_path = write_batch_timing_summary(
                batch_path,
                task_name="backup auto",
                started_at=started_at,
                wall_seconds=100.0,
                total_threads=5,
                snapshots=snapshots,
                thread_failure_categories=Counter(),
                peak_unstarted_configs=3,
                unstarted_config_seconds=6.5,
                max_config_start_wait_seconds=4.5,
            )
            summary = output_path.read_text(encoding="utf-8")

        assert (
            "样本5，线程秒总和=110.000s，P50=3.000s，"
            "P95=100.000s，P99=100.000s，max=100.000s"
            in summary
        )
        assert "- 峰值未启动主题数：3" in summary
        assert "- 累计启动等待：6.500s" in summary
        assert "- 最大启动等待：4.500s" in summary
        assert "图片引用处理模式：\ndelta=5\n" in summary
        assert summary.index("- target-5: 100.000s") < summary.index(
            "- target-4: 4.000s"
        )

    def test_batch_timing_summary_expands_forum_sync_breakdown(self) -> None:
        started_at = datetime.now().astimezone()
        forum_timing = ForumSyncTimingSnapshot(
            total_seconds=15.0,
            setup_seconds=1.0,
            fetch_seconds=5.0,
            forum_page_request_seconds=2.0,
            rate_limit_wait_seconds=1.0,
            watermark_read_seconds=0.5,
            database_upsert_seconds=0.5,
            screening_seconds=6.0,
            database_read_seconds=1.0,
            author_page_request_seconds=4.0,
            config_merge_seconds=1.0,
            config_save_seconds=1.0,
            reporting_seconds=1.0,
            successful_page_count=3,
            forum_page_request_attempt_count=4,
            rate_limit_retry_count=1,
            fetched_thread_count=105,
            scanned_thread_count=25826,
            author_page_request_count=40,
            config_saved=True,
        )

        with TemporaryDirectory() as temp_dir_name:
            output_path = write_batch_timing_summary(
                Path(temp_dir_name) / "batch_timing.log",
                task_name="backup auto",
                started_at=started_at,
                wall_seconds=30.0,
                total_threads=0,
                snapshots=(),
                thread_failure_categories=Counter(),
                forum_sync_seconds=20.0,
                forum_sync_timing=forum_timing,
            )
            summary = output_path.read_text(encoding="utf-8")

        assert "- 论坛同步：20.000s\n" in summary
        assert "  - 准备：1.000s\n" in summary
        assert "  - 版面抓取与入库：5.000s（成功3页，抓取105个主题）\n" in summary
        assert "    - 版面页请求：2.000s（尝试4次，限流重试1次）\n" in summary
        assert "    - 限流等待：1.000s\n" in summary
        assert "    - 水位读取：0.500s\n" in summary
        assert "    - SQLite写入：0.500s\n" in summary
        assert "    - 其余页处理与进度：1.000s\n" in summary
        assert "    - SQLite读取：1.000s（筛查25826条记录）\n" in summary
        assert "    - 只看作者请求：4.000s（请求40次）\n" in summary
        assert "    - 本地规则匹配与进度：1.000s\n" in summary
        assert "  - 配置保存：1.000s（已写入）\n" in summary
        assert (
            "  - 结果输出/其他：6.000s"
            "（结果输出 1.000s，未归类 5.000s）\n"
            in summary
        )

    def test_batch_timing_summary_keeps_legacy_forum_sync_line_without_snapshot(
        self,
    ) -> None:
        with TemporaryDirectory() as temp_dir_name:
            output_path = write_batch_timing_summary(
                Path(temp_dir_name) / "batch_timing.log",
                task_name="backup auto",
                started_at=datetime.now().astimezone(),
                wall_seconds=10.0,
                total_threads=0,
                snapshots=(),
                thread_failure_categories=Counter(),
                forum_sync_seconds=3.0,
            )
            summary = output_path.read_text(encoding="utf-8")

        assert "命令阶段耗时：\n- 论坛同步：3.000s\n" in summary
        assert "版面抓取与入库" not in summary

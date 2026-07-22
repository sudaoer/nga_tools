from __future__ import annotations

import math
import re
import subprocess
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from time import perf_counter
from typing import TextIO

from nga_tools.core.atomic import write_text_atomically
from nga_tools.forum.timing import ForumSyncTimingSnapshot


TIMING_LOG_RETENTION_COUNT = 5
_COMMIT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
_PROJECT_ROOT = Path(__file__).resolve().parents[1]


@cache
def git_commit_id() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_PROJECT_ROOT,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit_id = result.stdout.strip().lower()
    if result.returncode != 0 or _COMMIT_ID_RE.fullmatch(commit_id) is None:
        return None
    return commit_id


def _timestamped_log_path(path: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y%m%dT%H%M%S%f")
    base_name = f"{path.stem}-{timestamp}"
    candidate = path.with_name(f"{base_name}{path.suffix}")
    collision_index = 2
    while candidate.exists():
        candidate = path.with_name(
            f"{base_name}-{collision_index}{path.suffix}"
        )
        collision_index += 1
    return candidate


def _timing_log_candidates(path: Path) -> list[Path]:
    candidates = [
        candidate
        for candidate in path.parent.glob(f"{path.stem}-*{path.suffix}")
        if candidate.is_file()
    ]
    if path.is_file():
        candidates.append(path)
    return candidates


def _prune_timing_logs(path: Path) -> None:
    candidates: list[tuple[int, str, Path]] = []
    for candidate in _timing_log_candidates(path):
        try:
            modified_ns = candidate.stat().st_mtime_ns
        except OSError:
            continue
        candidates.append((modified_ns, candidate.name, candidate))
    candidates.sort(reverse=True)
    for _, _, stale_path in candidates[TIMING_LOG_RETENTION_COUNT:]:
        try:
            stale_path.unlink()
        except OSError:
            continue


@dataclass(frozen=True)
class TimingSectionRecord:
    name: str
    elapsed_seconds: float
    status: str


@dataclass(frozen=True)
class TimingSnapshot:
    task_name: str
    target: str | None
    started_at: datetime
    elapsed_seconds: float
    status: str
    sections: tuple[TimingSectionRecord, ...]
    metrics: tuple[tuple[str, int], ...]
    labels: tuple[tuple[str, str], ...]


class BatchTimingCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshots: list[TimingSnapshot] = []

    def add(self, snapshot: TimingSnapshot) -> None:
        with self._lock:
            self._snapshots.append(snapshot)

    def snapshots(self) -> tuple[TimingSnapshot, ...]:
        with self._lock:
            return tuple(self._snapshots)


class TimingLog:
    def __init__(
        self,
        log_file: TextIO,
        *,
        task_name: str,
        target: str | None,
        started_at: datetime,
        path: Path,
        commit_id: str | None,
    ) -> None:
        self._log_file = log_file
        self._task_name = task_name
        self._target = target
        self._started_at = started_at
        self._path = path
        self._start = perf_counter()
        self._finished = False
        self._snapshot: TimingSnapshot | None = None
        self._sections: list[TimingSectionRecord] = []
        self._metrics: list[tuple[str, int]] = []
        self._labels: list[tuple[str, str]] = []
        self._section_depth = 0
        self._write_header(task_name, target, commit_id)

    @property
    def path(self) -> Path:
        return self._path

    def _indent(self) -> str:
        return "  " * self._section_depth

    def _write_header(
        self,
        task_name: str,
        target: str | None,
        commit_id: str | None,
    ) -> None:
        started_at = _format_timestamp(self._started_at)
        self._log_file.write(f"开始时间：{started_at}\n")
        self._log_file.write(f"任务：{task_name}\n")
        if target is not None:
            self._log_file.write(f"目标：{target}\n")
        if commit_id is not None:
            self._log_file.write(f"Commit ID：{commit_id}\n")
        self._log_file.flush()

    def start_section(self, section_name: str, started_at: datetime) -> None:
        self._log_file.write(
            f"{self._indent()}阶段：{section_name}，"
            f"开始时间：{_format_timestamp(started_at)}\n"
        )
        self._log_file.flush()
        self._section_depth += 1

    def finish_section(
        self,
        section_name: str,
        ended_at: datetime,
        elapsed_seconds: float,
        *,
        status: str,
    ) -> None:
        self._section_depth -= 1
        self._sections.append(
            TimingSectionRecord(section_name, elapsed_seconds, status)
        )
        self._log_file.write(
            f"{self._indent()}阶段：{section_name}，结束时间：{_format_timestamp(ended_at)}，"
            f"耗时：{_format_duration(elapsed_seconds)}，状态：{status}\n"
        )
        self._log_file.flush()

    def record(self, section_name: str, elapsed_seconds: float) -> None:
        self._sections.append(
            TimingSectionRecord(section_name, elapsed_seconds, "完成")
        )
        self._log_file.write(
            f"{self._indent()}阶段：{section_name}，耗时：{_format_duration(elapsed_seconds)}\n"
        )
        self._log_file.flush()

    def record_metric(self, metric_name: str, value: int) -> None:
        self._metrics.append((metric_name, value))
        self._log_file.write(f"{self._indent()}指标：{metric_name}，值：{value}\n")
        self._log_file.flush()

    def record_label(self, label_name: str, value: str) -> None:
        self._labels.append((label_name, value))
        self._log_file.write(f"{self._indent()}标签：{label_name}，值：{value}\n")
        self._log_file.flush()

    def finish(self, status: str) -> TimingSnapshot:
        if self._finished:
            if self._snapshot is None:
                raise RuntimeError("计时日志已结束但缺少快照。")
            return self._snapshot
        self._finished = True
        elapsed_seconds = perf_counter() - self._start
        self._log_file.write(
            f"总耗时：{_format_duration(elapsed_seconds)}，状态：{status}\n"
        )
        self._log_file.flush()
        self._snapshot = TimingSnapshot(
            task_name=self._task_name,
            target=self._target,
            started_at=self._started_at,
            elapsed_seconds=elapsed_seconds,
            status=status,
            sections=tuple(self._sections),
            metrics=tuple(self._metrics),
            labels=tuple(self._labels),
        )
        return self._snapshot


_CURRENT_TIMING_LOG: ContextVar[TimingLog | None] = ContextVar(
    "nga_tools_timing_log",
    default=None,
)


def _format_duration(elapsed_seconds: float) -> str:
    return f"{elapsed_seconds:.3f}s"


def _format_timestamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds")


@contextmanager
def use_timing_log(
    path: str | Path,
    *,
    task_name: str,
    target: str | None = None,
    enabled: bool = True,
    on_finish: Callable[[TimingSnapshot], None] | None = None,
) -> Generator[TimingLog | None]:
    if not enabled:
        yield None
        return

    base_log_path = Path(path)
    base_log_path.parent.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now().astimezone()
    log_path = _timestamped_log_path(base_log_path, started_at)
    try:
        with log_path.open("x", encoding="utf-8") as log_file:
            timing_log = TimingLog(
                log_file,
                task_name=task_name,
                target=target,
                started_at=started_at,
                path=log_path,
                commit_id=git_commit_id(),
            )
            token = _CURRENT_TIMING_LOG.set(timing_log)
            try:
                yield timing_log
            except BaseException:
                snapshot = timing_log.finish("失败")
                if on_finish is not None:
                    on_finish(snapshot)
                raise
            else:
                snapshot = timing_log.finish("完成")
                if on_finish is not None:
                    on_finish(snapshot)
            finally:
                _CURRENT_TIMING_LOG.reset(token)
    finally:
        _prune_timing_logs(base_log_path)


@contextmanager
def time_section(section_name: str) -> Generator[None]:
    timing_log = _CURRENT_TIMING_LOG.get()
    if timing_log is None:
        yield
        return

    timing_log.start_section(section_name, datetime.now().astimezone())
    start = perf_counter()
    try:
        yield
    except BaseException:
        timing_log.finish_section(
            section_name,
            datetime.now().astimezone(),
            perf_counter() - start,
            status="失败",
        )
        raise
    else:
        timing_log.finish_section(
            section_name,
            datetime.now().astimezone(),
            perf_counter() - start,
            status="完成",
        )


def record_timing(section_name: str, elapsed_seconds: float) -> None:
    timing_log = _CURRENT_TIMING_LOG.get()
    if timing_log is None:
        return
    timing_log.record(section_name, elapsed_seconds)


def record_timing_metric(metric_name: str, value: int) -> None:
    timing_log = _CURRENT_TIMING_LOG.get()
    if timing_log is None:
        return
    timing_log.record_metric(metric_name, value)


def record_timing_label(label_name: str, value: str) -> None:
    timing_log = _CURRENT_TIMING_LOG.get()
    if timing_log is None:
        return
    timing_log.record_label(label_name, value)


def _nearest_rank(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("百分位样本不能为空。")
    ordered = sorted(values)
    index = max(0, math.ceil(percentile * len(ordered)) - 1)
    return ordered[index]


def _summed_stage_durations_by_thread(
    snapshots: Iterable[TimingSnapshot],
) -> dict[str, list[float]]:
    durations_by_stage: dict[str, list[float]] = defaultdict(list)
    for snapshot in snapshots:
        thread_totals: dict[str, float] = defaultdict(float)
        for section in snapshot.sections:
            thread_totals[section.name] += section.elapsed_seconds
        for stage_name, elapsed_seconds in thread_totals.items():
            durations_by_stage[stage_name].append(elapsed_seconds)
    return dict(durations_by_stage)


def _duration_distribution_text(values: list[float]) -> str:
    return (
        f"样本{len(values)}，"
        f"线程秒总和={_format_duration(sum(values))}，"
        f"P50={_format_duration(_nearest_rank(values, 0.50))}，"
        f"P95={_format_duration(_nearest_rank(values, 0.95))}，"
        f"P99={_format_duration(_nearest_rank(values, 0.99))}，"
        f"max={_format_duration(max(values))}"
    )


def _remaining_seconds(parent: float, *children: float) -> float:
    return max(0.0, parent - sum(children))


def _forum_sync_timing_lines(
    total_seconds: float,
    timing: ForumSyncTimingSnapshot,
) -> list[str]:
    fetch_local_seconds = _remaining_seconds(
        timing.fetch_seconds,
        timing.forum_page_request_seconds,
        timing.rate_limit_wait_seconds,
        timing.watermark_read_seconds,
        timing.database_upsert_seconds,
    )
    screening_local_seconds = _remaining_seconds(
        timing.screening_seconds,
        timing.database_read_seconds,
        timing.author_page_request_seconds,
    )
    unclassified_seconds = _remaining_seconds(
        total_seconds,
        timing.setup_seconds,
        timing.fetch_seconds,
        timing.screening_seconds,
        timing.config_merge_seconds,
        timing.config_save_seconds,
        timing.reporting_seconds,
    )
    reporting_and_other_seconds = (
        timing.reporting_seconds + unclassified_seconds
    )
    config_save_status = "已写入" if timing.config_saved else "未触发写入"
    return [
        f"- 论坛同步：{_format_duration(total_seconds)}",
        f"  - 准备：{_format_duration(timing.setup_seconds)}",
        (
            "  - 版面抓取与入库："
            f"{_format_duration(timing.fetch_seconds)}"
            f"（成功{timing.successful_page_count}页，"
            f"抓取{timing.fetched_thread_count}个主题）"
        ),
        (
            "    - 版面页请求："
            f"{_format_duration(timing.forum_page_request_seconds)}"
            f"（尝试{timing.forum_page_request_attempt_count}次，"
            f"限流重试{timing.rate_limit_retry_count}次）"
        ),
        (
            "    - 限流等待："
            f"{_format_duration(timing.rate_limit_wait_seconds)}"
        ),
        (
            "    - 水位读取："
            f"{_format_duration(timing.watermark_read_seconds)}"
        ),
        (
            "    - SQLite写入："
            f"{_format_duration(timing.database_upsert_seconds)}"
        ),
        (
            "    - 其余页处理与进度："
            f"{_format_duration(fetch_local_seconds)}"
        ),
        f"  - 数据库筛查：{_format_duration(timing.screening_seconds)}",
        (
            "    - SQLite读取："
            f"{_format_duration(timing.database_read_seconds)}"
            f"（筛查{timing.scanned_thread_count}条记录）"
        ),
        (
            "    - 只看作者请求："
            f"{_format_duration(timing.author_page_request_seconds)}"
            f"（请求{timing.author_page_request_count}次）"
        ),
        (
            "    - 本地规则匹配与进度："
            f"{_format_duration(screening_local_seconds)}"
        ),
        f"  - 配置合并：{_format_duration(timing.config_merge_seconds)}",
        (
            "  - 配置保存："
            f"{_format_duration(timing.config_save_seconds)}"
            f"（{config_save_status}）"
        ),
        (
            "  - 结果输出/其他："
            f"{_format_duration(reporting_and_other_seconds)}"
            f"（结果输出 {_format_duration(timing.reporting_seconds)}，"
            f"未归类 {_format_duration(unclassified_seconds)}）"
        ),
    ]


def write_batch_timing_summary(
    path: Path,
    *,
    task_name: str,
    started_at: datetime,
    wall_seconds: float,
    total_threads: int,
    snapshots: Iterable[TimingSnapshot],
    thread_failure_categories: Counter[str],
    expected_thread_failure_categories: Counter[str] | None = None,
    forum_sync_seconds: float | None = None,
    forum_sync_timing: ForumSyncTimingSnapshot | None = None,
    planning_seconds: float | None = None,
    water_level_seconds: float | None = None,
    batch_execution_seconds: float | None = None,
    peak_unstarted_configs: int | None = None,
    unstarted_config_seconds: float | None = None,
    max_config_start_wait_seconds: float | None = None,
) -> Path:
    snapshot_list = list(snapshots)
    image_failure_categories: Counter[str] = Counter()
    processing_reuse_results: Counter[str] = Counter()
    image_reference_modes: Counter[str] = Counter()
    sqlite_max_wait_microseconds = 0
    sqlite_peak_concurrency = 0
    for snapshot in snapshot_list:
        for metric_name, value in snapshot.metrics:
            prefix = "图片下载失败/"
            if metric_name.startswith(prefix):
                image_failure_categories[metric_name.removeprefix(prefix)] += value
            if metric_name == "SQLite最大排队微秒":
                sqlite_max_wait_microseconds = max(
                    sqlite_max_wait_microseconds,
                    value,
                )
            elif metric_name == "SQLite峰值并发":
                sqlite_peak_concurrency = max(sqlite_peak_concurrency, value)
        for label_name, value in snapshot.labels:
            if label_name in {"处理状态复用结果", "增量快路径结果"}:
                processing_reuse_results[value] += 1
            if label_name == "图片引用处理模式":
                image_reference_modes[value] += 1

    expected_failure_categories: Counter[str] = (
        Counter[str]()
        if expected_thread_failure_categories is None
        else expected_thread_failure_categories
    )
    failed_threads = sum(thread_failure_categories.values())
    expected_failed_threads = sum(expected_failure_categories.values())
    successful_threads = total_threads - failed_threads - expected_failed_threads
    image_failure_count = sum(image_failure_categories.values())
    if failed_threads:
        status = "失败"
    elif expected_failed_threads:
        status = "完成（含隐藏帖跳过）"
    elif image_failure_count:
        status = "完成（含预期图片下载失败，等待后续重试）"
    else:
        status = "完成"

    thread_summary = (
        f"帖子：总数{total_threads}，成功{successful_threads}，"
        f"隐藏跳过{expected_failed_threads}，失败{failed_threads}"
        if expected_failed_threads
        else f"帖子：总数{total_threads}，成功{successful_threads}，失败{failed_threads}"
    )
    lines = [
        f"开始时间：{_format_timestamp(started_at)}",
        f"任务：{task_name}",
    ]
    commit_id = git_commit_id()
    if commit_id is not None:
        lines.append(f"Commit ID：{commit_id}")
    lines.extend(
        [
            f"墙钟时间：{_format_duration(wall_seconds)}",
            thread_summary,
            f"状态：{status}",
            "",
            "处理状态复用：",
        ]
    )
    if processing_reuse_results:
        hit_count = processing_reuse_results.get("hit", 0)
        not_applicable_count = processing_reuse_results.get("not_applicable", 0)
        miss_count = (
            sum(processing_reuse_results.values())
            - hit_count
            - not_applicable_count
        )
        lines.append(
            f"命中{hit_count}，未命中{miss_count}，不适用{not_applicable_count}"
        )
        for reason, count in sorted(processing_reuse_results.items()):
            if reason not in {"hit", "not_applicable"}:
                lines.append(f"- {reason}: {count}")
    else:
        lines.append("不适用（本批次未执行处理状态复用）")

    lines.extend(["", "图片引用处理模式："])
    if image_reference_modes:
        lines.append(
            "，".join(
                f"{mode}={count}"
                for mode, count in sorted(image_reference_modes.items())
            )
        )
    else:
        lines.append("不适用（本批次无图片引用处理样本）")

    lines.extend(
        [
            "",
            "SQLite并发门：",
            (
                f"峰值并发={sqlite_peak_concurrency}，"
                f"最大排队={_format_duration(sqlite_max_wait_microseconds / 1_000_000)}"
            ),
        ]
    )

    has_command_phases = (
        forum_sync_seconds is not None
        or forum_sync_timing is not None
        or planning_seconds is not None
        or water_level_seconds is not None
    )
    if has_command_phases:
        lines.extend(["", "命令阶段耗时："])
        effective_forum_sync_seconds = forum_sync_seconds
        if (
            effective_forum_sync_seconds is None
            and forum_sync_timing is not None
        ):
            effective_forum_sync_seconds = forum_sync_timing.total_seconds
        if effective_forum_sync_seconds is not None:
            if forum_sync_timing is None:
                lines.append(
                    f"- 论坛同步：{_format_duration(effective_forum_sync_seconds)}"
                )
            else:
                lines.extend(
                    _forum_sync_timing_lines(
                        effective_forum_sync_seconds,
                        forum_sync_timing,
                    )
                )
        planning_line = (
            f"- 任务选择：{_format_duration(planning_seconds)}"
            if planning_seconds is not None
            else "- 任务选择：不适用"
        )
        if water_level_seconds is not None:
            planning_line += f"（含水位读取 {_format_duration(water_level_seconds)}）"
        lines.append(planning_line)
        batch_execution_value = (
            batch_execution_seconds
            if batch_execution_seconds is not None
            else wall_seconds
        )
        lines.append(f"- 批次执行：{_format_duration(batch_execution_value)}")

    has_queue_metrics = (
        peak_unstarted_configs is not None
        or unstarted_config_seconds is not None
        or max_config_start_wait_seconds is not None
    )
    if has_queue_metrics:
        lines.extend(
            [
                "",
                "批次队列：",
                f"- 峰值未启动主题数：{peak_unstarted_configs or 0}",
                "- 累计启动等待："
                f"{_format_duration(unstarted_config_seconds or 0.0)}",
                "- 最大启动等待："
                f"{_format_duration(max_config_start_wait_seconds or 0.0)}",
            ]
        )

    lines.extend(["", "主题总耗时（可用线程快照）："])
    thread_elapsed_values = [
        snapshot.elapsed_seconds for snapshot in snapshot_list
    ]
    if thread_elapsed_values:
        lines.append(_duration_distribution_text(thread_elapsed_values))
    else:
        lines.append("无线程快照")

    lines.extend(["", "阶段耗时（每帖同名阶段累计）："])
    durations_by_stage = _summed_stage_durations_by_thread(snapshot_list)
    if durations_by_stage:
        for stage_name in sorted(durations_by_stage):
            values = durations_by_stage[stage_name]
            lines.append(f"- {stage_name}: {_duration_distribution_text(values)}")
    else:
        lines.append("无阶段样本")

    lines.extend(["", "最慢主题（Top 5）："])
    slowest_snapshots = sorted(
        snapshot_list,
        key=lambda snapshot: (
            -snapshot.elapsed_seconds,
            snapshot.target or "",
            snapshot.task_name,
            snapshot.started_at,
        ),
    )[:5]
    if slowest_snapshots:
        for snapshot in slowest_snapshots:
            target = snapshot.target or "未知目标"
            lines.append(
                f"- {target}: {_format_duration(snapshot.elapsed_seconds)}"
                f"（状态：{snapshot.status}）"
            )
    else:
        lines.append("无线程快照")

    lines.extend(["", "失败分类："])
    if image_failure_categories:
        lines.append(f"预期图片下载失败：{image_failure_count}")
        for category, count in sorted(image_failure_categories.items()):
            lines.append(f"- {category}: {count}")
    else:
        lines.append("预期图片下载失败：0")
    if thread_failure_categories:
        lines.append(f"线程异常：{failed_threads}")
        for category, count in sorted(thread_failure_categories.items()):
            lines.append(f"- {category}: {count}")
    else:
        lines.append("线程异常：0")
    if expected_failure_categories:
        lines.append(f"预期线程跳过：{expected_failed_threads}")
        for category, count in sorted(expected_failure_categories.items()):
            lines.append(f"- {category}: {count}")

    started_log_path = _timestamped_log_path(path, started_at)
    write_text_atomically(started_log_path, "\n".join(lines) + "\n")
    _prune_timing_logs(path)
    return started_log_path

from __future__ import annotations

import math
import threading
from collections import Counter, defaultdict
from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TextIO

from nga_tools.core.atomic import write_text_atomically


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
    ) -> None:
        self._log_file = log_file
        self._task_name = task_name
        self._target = target
        self._started_at = datetime.now().astimezone()
        self._start = perf_counter()
        self._finished = False
        self._snapshot: TimingSnapshot | None = None
        self._sections: list[TimingSectionRecord] = []
        self._metrics: list[tuple[str, int]] = []
        self._labels: list[tuple[str, str]] = []
        self._write_header(task_name, target)

    def _write_header(self, task_name: str, target: str | None) -> None:
        started_at = _format_timestamp(self._started_at)
        self._log_file.write(f"开始时间：{started_at}\n")
        self._log_file.write(f"任务：{task_name}\n")
        if target is not None:
            self._log_file.write(f"目标：{target}\n")
        self._log_file.flush()

    def start_section(self, section_name: str, started_at: datetime) -> None:
        self._log_file.write(
            f"阶段：{section_name}，开始时间：{_format_timestamp(started_at)}\n"
        )
        self._log_file.flush()

    def finish_section(
        self,
        section_name: str,
        ended_at: datetime,
        elapsed_seconds: float,
        *,
        status: str,
    ) -> None:
        self._sections.append(
            TimingSectionRecord(section_name, elapsed_seconds, status)
        )
        self._log_file.write(
            f"阶段：{section_name}，结束时间：{_format_timestamp(ended_at)}，"
            f"耗时：{_format_duration(elapsed_seconds)}，状态：{status}\n"
        )
        self._log_file.flush()

    def record(self, section_name: str, elapsed_seconds: float) -> None:
        self._sections.append(
            TimingSectionRecord(section_name, elapsed_seconds, "完成")
        )
        self._log_file.write(
            f"阶段：{section_name}，耗时：{_format_duration(elapsed_seconds)}\n"
        )
        self._log_file.flush()

    def record_metric(self, metric_name: str, value: int) -> None:
        self._metrics.append((metric_name, value))
        self._log_file.write(f"指标：{metric_name}，值：{value}\n")
        self._log_file.flush()

    def record_label(self, label_name: str, value: str) -> None:
        self._labels.append((label_name, value))
        self._log_file.write(f"标签：{label_name}，值：{value}\n")
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

    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        timing_log = TimingLog(log_file, task_name=task_name, target=target)
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


def write_batch_timing_summary(
    path: Path,
    *,
    task_name: str,
    started_at: datetime,
    wall_seconds: float,
    total_threads: int,
    snapshots: Iterable[TimingSnapshot],
    thread_failure_categories: Counter[str],
) -> None:
    snapshot_list = list(snapshots)
    image_failure_categories: Counter[str] = Counter()
    processing_reuse_results: Counter[str] = Counter()
    for snapshot in snapshot_list:
        for metric_name, value in snapshot.metrics:
            prefix = "图片下载失败/"
            if metric_name.startswith(prefix):
                image_failure_categories[metric_name.removeprefix(prefix)] += value
        for label_name, value in snapshot.labels:
            if label_name in {"处理状态复用结果", "增量快路径结果"}:
                processing_reuse_results[value] += 1

    failed_threads = sum(thread_failure_categories.values())
    successful_threads = total_threads - failed_threads
    image_failure_count = sum(image_failure_categories.values())
    if failed_threads:
        status = "失败"
    elif image_failure_count:
        status = "完成（含预期图片下载失败，等待后续重试）"
    else:
        status = "完成"

    lines = [
        f"开始时间：{_format_timestamp(started_at)}",
        f"任务：{task_name}",
        f"墙钟时间：{_format_duration(wall_seconds)}",
        f"帖子：总数{total_threads}，成功{successful_threads}，失败{failed_threads}",
        f"状态：{status}",
        "",
        "处理状态复用：",
    ]
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

    lines.extend(["", "阶段耗时（每帖同名阶段累计）："])
    durations_by_stage = _summed_stage_durations_by_thread(snapshot_list)
    if durations_by_stage:
        for stage_name in sorted(durations_by_stage):
            values = durations_by_stage[stage_name]
            lines.append(
                f"- {stage_name}: 样本{len(values)}，"
                f"P50={_format_duration(_nearest_rank(values, 0.50))}，"
                f"P95={_format_duration(_nearest_rank(values, 0.95))}"
            )
    else:
        lines.append("无阶段样本")

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

    write_text_atomically(path, "\n".join(lines) + "\n")

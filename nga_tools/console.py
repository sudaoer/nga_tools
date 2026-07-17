from __future__ import annotations

from collections import Counter
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from enum import StrEnum
from pathlib import Path
import sys
from threading import RLock
import unicodedata
from typing import Protocol, TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    Task,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.progress_bar import ProgressBar
from rich.table import Column


class WarningCategory(StrEnum):
    DOWNLOAD_RETRY = "下载重试"
    IMAGE_DOWNLOAD = "图片下载"
    IMAGE_PROCESSING = "图片处理"
    AUDIO_DOWNLOAD = "音频下载"
    AUDIO_PROCESSING = "音频处理"
    FLOOR_MAP = "楼层映射"
    POST_CONTENT = "帖子内容"
    PROCESSING_STATE = "处理状态"
    CACHE = "缓存"
    PDF = "PDF生成"
    TASK_FAILURE = "任务失败"


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


class Reporter(Protocol):
    @property
    def console(self) -> Console:
        raise NotImplementedError

    def info(self, message: str) -> None:
        raise NotImplementedError

    def warning(self, category: WarningCategory, message: str) -> None:
        raise NotImplementedError

    def progress(
        self,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        raise NotImplementedError


class ConsoleReporter:
    def __init__(self, console: Console | None = None) -> None:
        self._console = console

    def _active_console(self) -> Console:
        if self._console is not None:
            return self._console
        return Console(file=sys.stdout, soft_wrap=True)

    @property
    def console(self) -> Console:
        return self._active_console()

    def info(self, message: str) -> None:
        self._active_console().print(message, markup=False)

    def warning(self, category: WarningCategory, message: str) -> None:
        del category
        self._active_console().print(f"警告：{message}", markup=False)

    def progress(
        self,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        if completed is None or total is None:
            self.info(message)
            return
        self.info(f"{message} ({completed}/{total})")


class WarningLogReporter:
    def __init__(self, reporter: Reporter, log_file: TextIO) -> None:
        self._reporter = reporter
        self._log_file = log_file

    @property
    def console(self) -> Console:
        return self._reporter.console

    def info(self, message: str) -> None:
        self._reporter.info(message)

    def warning(self, category: WarningCategory, message: str) -> None:
        self._reporter.warning(category, message)
        self._log_file.write(f"警告：{message}\n")
        self._log_file.flush()

    def progress(
        self,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        self._reporter.progress(message, completed=completed, total=total)


_DEFAULT_REPORTER = ConsoleReporter()
_CURRENT_REPORTER: ContextVar[Reporter | None] = ContextVar(
    "nga_tools_reporter",
    default=None,
)


class WarningSummaryCollector:
    def __init__(self) -> None:
        self._lock = RLock()
        self._counts: Counter[WarningCategory] = Counter()
        self._thread_labels: set[str] = set()

    def record(
        self,
        category: WarningCategory,
        *,
        thread_label: str | None = None,
    ) -> None:
        with self._lock:
            self._counts[category] += 1
            if thread_label is not None:
                self._thread_labels.add(thread_label)

    def snapshot(self) -> tuple[Counter[WarningCategory], int]:
        with self._lock:
            return self._counts.copy(), len(self._thread_labels)


class WarningSummaryReporter:
    def __init__(
        self,
        reporter: Reporter,
        collector: WarningSummaryCollector,
        *,
        thread_label: str | None = None,
        parent_collector: WarningSummaryCollector | None = None,
    ) -> None:
        self._reporter = reporter
        self._collector = collector
        self._thread_label = thread_label
        self._parent_collector = parent_collector

    @property
    def console(self) -> Console:
        return self._reporter.console

    def info(self, message: str) -> None:
        self._reporter.info(message)

    def warning(self, category: WarningCategory, message: str) -> None:
        del message
        self._collector.record(category, thread_label=self._thread_label)
        if self._parent_collector is not None:
            self._parent_collector.record(
                category,
                thread_label=self._thread_label,
            )

    def progress(
        self,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        self._reporter.progress(message, completed=completed, total=total)


_CURRENT_WARNING_COLLECTOR: ContextVar[WarningSummaryCollector | None] = (
    ContextVar(
        "nga_tools_warning_collector",
        default=None,
    )
)


def get_reporter() -> Reporter:
    reporter = _CURRENT_REPORTER.get()
    return _DEFAULT_REPORTER if reporter is None else reporter


@contextmanager
def use_reporter(reporter: Reporter) -> Generator[None]:
    token = _CURRENT_REPORTER.set(reporter)
    try:
        yield
    finally:
        _CURRENT_REPORTER.reset(token)


def get_warning_collector() -> WarningSummaryCollector | None:
    return _CURRENT_WARNING_COLLECTOR.get()


def _warning_counts_text(counts: Counter[WarningCategory]) -> str:
    return "，".join(
        f"{category.value}{count}条"
        for category, count in sorted(
            counts.items(),
            key=lambda item: (-item[1], item[0].value),
        )
    )


def _report_thread_warning_summary(
    reporter: Reporter,
    label: str,
    collector: WarningSummaryCollector,
) -> None:
    counts, _ = collector.snapshot()
    total = sum(counts.values())
    if total == 0:
        return
    reporter.info(
        f"警告汇总：{label}：共{total}条；{_warning_counts_text(counts)}。"
    )


def _report_command_warning_summary(
    reporter: Reporter,
    collector: WarningSummaryCollector,
) -> None:
    counts, affected_thread_count = collector.snapshot()
    total = sum(counts.values())
    if total == 0:
        reporter.info("警告总计：共0条。")
        return
    affected_text = (
        f"，涉及{affected_thread_count}个帖子"
        if affected_thread_count
        else ""
    )
    reporter.info(
        f"警告总计：共{total}条{affected_text}；"
        f"{_warning_counts_text(counts)}。"
    )


@contextmanager
def use_command_warning_summary() -> Generator[WarningSummaryCollector]:
    reporter = get_reporter()
    collector = WarningSummaryCollector()
    summary_reporter = WarningSummaryReporter(reporter, collector)
    reporter_token = _CURRENT_REPORTER.set(summary_reporter)
    collector_token = _CURRENT_WARNING_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _CURRENT_WARNING_COLLECTOR.reset(collector_token)
        _CURRENT_REPORTER.reset(reporter_token)
        _report_command_warning_summary(reporter, collector)


@contextmanager
def use_thread_warning_summary(
    label: str,
    *,
    parent_collector: WarningSummaryCollector | None = None,
    summary_reporter: Reporter | None = None,
) -> Generator[WarningSummaryCollector]:
    reporter = get_reporter()
    collector = WarningSummaryCollector()
    effective_parent = (
        get_warning_collector()
        if parent_collector is None
        else parent_collector
    )
    grouped_reporter = WarningSummaryReporter(
        reporter,
        collector,
        thread_label=label,
        parent_collector=effective_parent,
    )
    reporter_token = _CURRENT_REPORTER.set(grouped_reporter)
    collector_token = _CURRENT_WARNING_COLLECTOR.set(collector)
    try:
        yield collector
    finally:
        _CURRENT_WARNING_COLLECTOR.reset(collector_token)
        _CURRENT_REPORTER.reset(reporter_token)
        _report_thread_warning_summary(
            reporter if summary_reporter is None else summary_reporter,
            label,
            collector,
        )


@contextmanager
def use_warning_log(path: str | Path) -> Generator[None]:
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    reporter = get_reporter()
    with log_path.open("w", encoding="utf-8") as log_file:
        with use_reporter(WarningLogReporter(reporter, log_file)):
            yield


def report_info(message: str) -> None:
    get_reporter().info(message)


def report_warning(category: WarningCategory, message: str) -> None:
    get_reporter().warning(category, message)


def report_progress(
    message: str,
    *,
    completed: int | None = None,
    total: int | None = None,
) -> None:
    get_reporter().progress(message, completed=completed, total=total)


def _progress_text(completed: int | None, total: int | None) -> str:
    if completed is None or total is None:
        return ""
    return f"{completed}/{total}"


def _single_line_column(
    *,
    min_width: int | None = None,
    max_width: int | None = None,
    width: int | None = None,
    ratio: int | None = None,
) -> Column:
    return Column(
        min_width=min_width,
        max_width=max_width,
        width=width,
        ratio=ratio,
        no_wrap=True,
        overflow="ellipsis",
    )


class _StageBarColumn(BarColumn):
    """Render child-stage progress without completing the parent task."""

    def render(self, task: Task) -> ProgressBar:
        stage_completed = task.fields.get("stage_completed")
        stage_total = task.fields.get("stage_total")
        if (
            isinstance(stage_completed, int | float)
            and isinstance(stage_total, int | float)
        ):
            completed = float(stage_completed)
            total = float(stage_total)
        else:
            completed = task.completed
            total = task.total
        return ProgressBar(
            total=max(0, total) if total is not None else None,
            completed=max(0, completed),
            width=None if self.bar_width is None else max(1, self.bar_width),
            pulse=not task.started,
            animation_time=task.get_time(),
            style=self.style,
            complete_style=self.complete_style,
            finished_style=self.finished_style,
            pulse_style=self.pulse_style,
        )


class BackupConfigTaskReporter:
    def __init__(
        self,
        parent: BackupConfigsProgressDisplay,
        task_id: TaskID,
        label: str,
    ) -> None:
        self._parent = parent
        self._task_id = task_id
        self._label = label
        self._warning_collector = WarningSummaryCollector()

    @property
    def console(self) -> Console:
        return self._parent.console

    @property
    def task_id(self) -> TaskID:
        return self._task_id

    @property
    def label(self) -> str:
        return self._label

    @property
    def warning_collector(self) -> WarningSummaryCollector:
        return self._warning_collector

    def info(self, message: str) -> None:
        self._parent.update_task(self._task_id, message)

    def warning(self, category: WarningCategory, message: str) -> None:
        del message
        self._warning_collector.record(category, thread_label=self._label)

    def progress(
        self,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        self._parent.update_task(
            self._task_id,
            message,
            completed=completed,
            total=total,
        )


class BackupConfigsProgressDisplay:
    def __init__(self, total: int, console: Console | None = None) -> None:
        self._console = console if console is not None else Console()
        self._lock = RLock()
        self._progress = Progress(
            SpinnerColumn(),
            TextColumn(
                "{task.description}",
                markup=False,
                table_column=_single_line_column(
                    min_width=28,
                    max_width=56,
                    ratio=2,
                ),
            ),
            _StageBarColumn(bar_width=24),
            TextColumn(
                "{task.fields[progress_text]}",
                justify="right",
                markup=False,
                table_column=_single_line_column(width=11),
            ),
            TextColumn(
                "{task.fields[status]}",
                markup=False,
                table_column=_single_line_column(
                    min_width=16,
                    max_width=32,
                    ratio=1,
                ),
            ),
            TimeElapsedColumn(),
            console=self._console,
            expand=False,
        )
        self._total_task = self._progress.add_task(
            "总进度",
            total=total,
            completed=0,
            progress_text=_progress_text(0, total),
            status="",
        )

    @property
    def console(self) -> Console:
        return self._console

    def __enter__(self) -> BackupConfigsProgressDisplay:
        self._progress.start()
        return self

    def __exit__(
        self,
        exc_type: object,
        exc: object,
        traceback: object,
    ) -> None:
        del exc_type, exc, traceback
        self._progress.stop()

    def start_thread(
        self,
        *,
        index: int,
        total: int,
        label: str,
    ) -> BackupConfigTaskReporter:
        with self._lock:
            task_id = self._progress.add_task(
                f"[{index}/{total}] {label}",
                total=None,
                progress_text="",
                status="正在增量备份",
                stage_completed=None,
                stage_total=None,
            )
        return BackupConfigTaskReporter(self, task_id, label)

    def update_task(
        self,
        task_id: TaskID,
        message: str,
        *,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        with self._lock:
            has_stage_progress = completed is not None and total is not None
            self._progress.update(
                task_id,
                status=message,
                progress_text=(
                    _progress_text(completed, total)
                    if has_stage_progress
                    else ""
                ),
                stage_completed=completed if has_stage_progress else None,
                stage_total=total if has_stage_progress else None,
            )

    def finish_thread(
        self,
        reporter: BackupConfigTaskReporter,
        *,
        status: str,
    ) -> None:
        with self._lock:
            counts, _ = reporter.warning_collector.snapshot()
            warning_total = sum(counts.values())
            if warning_total:
                self._progress.console.print(
                    f"警告汇总：{reporter.label}：共{warning_total}条；"
                    f"{_warning_counts_text(counts)}。",
                    markup=False,
                )
            total_task = self._progress.tasks[self._total_task]
            completed = int(total_task.completed) + 1
            total = int(total_task.total or completed)
            self._progress.update(
                self._total_task,
                completed=completed,
                progress_text=_progress_text(completed, total),
                status=status,
            )
            self._progress.update(reporter.task_id, visible=False)

    def visible_task_descriptions(self) -> list[str]:
        with self._lock:
            return [
                task.description
                for task in self._progress.tasks
                if task.visible
            ]

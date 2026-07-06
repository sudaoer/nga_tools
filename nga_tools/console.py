from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
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
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Column


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

    def warning(self, message: str) -> None:
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
        self._console = console if console is not None else Console()

    @property
    def console(self) -> Console:
        return self._console

    def info(self, message: str) -> None:
        self._console.print(message, markup=False)

    def warning(self, message: str) -> None:
        self._console.print(f"警告：{message}", markup=False)

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

    def warning(self, message: str) -> None:
        self._reporter.warning(message)
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


def report_warning(message: str) -> None:
    get_reporter().warning(message)


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

    @property
    def console(self) -> Console:
        return self._parent.console

    @property
    def task_id(self) -> TaskID:
        return self._task_id

    def info(self, message: str) -> None:
        self._parent.update_task(self._task_id, message)

    def warning(self, message: str) -> None:
        self._parent.warning(self._label, message)

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
            BarColumn(bar_width=24),
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
            if completed is not None:
                self._progress.update(
                    task_id,
                    completed=completed,
                    total=total,
                    status=message,
                    progress_text=_progress_text(completed, total),
                )
            else:
                self._progress.update(
                    task_id,
                    completed=0,
                    total=None,
                    status=message,
                    progress_text="",
                )

    def finish_thread(
        self,
        reporter: BackupConfigTaskReporter,
        *,
        status: str,
    ) -> None:
        with self._lock:
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

    def warning(self, label: str, message: str) -> None:
        with self._lock:
            self._progress.console.print(
                f"警告：{label}：{message}",
                markup=False,
            )

    def visible_task_descriptions(self) -> list[str]:
        with self._lock:
            return [
                task.description
                for task in self._progress.tasks
                if task.visible
            ]

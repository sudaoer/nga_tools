from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TextIO


class TimingLog:
    def __init__(
        self,
        log_file: TextIO,
        *,
        task_name: str,
        target: str | None,
    ) -> None:
        self._log_file = log_file
        self._start = perf_counter()
        self._finished = False
        self._write_header(task_name, target)

    def _write_header(self, task_name: str, target: str | None) -> None:
        started_at = _format_timestamp(datetime.now().astimezone())
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
        self._log_file.write(
            f"阶段：{section_name}，结束时间：{_format_timestamp(ended_at)}，"
            f"耗时：{_format_duration(elapsed_seconds)}，状态：{status}\n"
        )
        self._log_file.flush()

    def record(self, section_name: str, elapsed_seconds: float) -> None:
        self._log_file.write(
            f"阶段：{section_name}，耗时：{_format_duration(elapsed_seconds)}\n"
        )
        self._log_file.flush()

    def finish(self, status: str) -> None:
        if self._finished:
            return
        self._finished = True
        elapsed_seconds = perf_counter() - self._start
        self._log_file.write(
            f"总耗时：{_format_duration(elapsed_seconds)}，状态：{status}\n"
        )
        self._log_file.flush()


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
) -> Generator[None]:
    if not enabled:
        yield
        return

    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as log_file:
        timing_log = TimingLog(log_file, task_name=task_name, target=target)
        token = _CURRENT_TIMING_LOG.set(timing_log)
        try:
            yield
        except BaseException:
            timing_log.finish("失败")
            raise
        else:
            timing_log.finish("完成")
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

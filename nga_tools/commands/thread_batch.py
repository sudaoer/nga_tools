from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import ExitStack
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime
from dataclasses import dataclass
from time import perf_counter

from nga_tools.console import (
    BackupConfigsProgressDisplay,
    get_reporter,
    report_info,
    report_progress,
    report_warning,
    use_reporter,
    use_warning_log,
)
from nga_tools.core.output_lock import use_thread_output_lock
from nga_tools.core.paths import (
    batch_timing_log_path,
    timing_log_path,
    warning_log_path,
)
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.ngaclient import is_hidden_thread_error
from nga_tools.timing import (
    BatchTimingCollector,
    TimingSnapshot,
    use_timing_log,
    write_batch_timing_summary,
)

ThreadConfigAction = Callable[[ThreadConfig], str | None]
ThreadBatchFailure = tuple[ThreadConfig, Exception]
ThreadBatchSuccess = tuple[int, str]


@dataclass(frozen=True)
class ThreadBatchResult:
    successes: tuple[ThreadBatchSuccess, ...]
    failures: tuple[ThreadBatchFailure, ...]
    hidden_threads: tuple[ThreadBatchFailure, ...]


def thread_config_label(thread_config: ThreadConfig) -> str:
    tid = thread_config_tid(thread_config)
    aid = thread_config_aid(thread_config)
    return (
        f"{thread_config_name(thread_config)} "
        f"(tid: {tid}, aid: {aid})"
    )


def _run_thread_config_with_progress(
    *,
    index: int,
    total: int,
    thread_config: ThreadConfig,
    progress: BackupConfigsProgressDisplay,
    action: ThreadConfigAction,
    progress_text: str,
    failure_text: str,
    write_warning_log: bool,
    write_timing_log: bool,
    timing_log_enabled: bool,
    task_name: str,
    lock_thread_output: bool,
    timing_snapshot_callback: Callable[[TimingSnapshot], None] | None,
) -> str | None:
    tid = thread_config_tid(thread_config)
    aid = thread_config_aid(thread_config)
    label = thread_config_label(thread_config)
    task_reporter = progress.start_thread(
        index=index,
        total=total,
        label=label,
    )
    with ExitStack() as stack:
        stack.enter_context(use_reporter(task_reporter))
        try:
            if lock_thread_output:
                stack.enter_context(use_thread_output_lock(tid, aid))
            if write_warning_log:
                stack.enter_context(use_warning_log(warning_log_path(tid, aid)))
            if write_timing_log:
                stack.enter_context(
                    use_timing_log(
                        timing_log_path(tid, aid),
                        task_name=task_name,
                        target=label,
                        enabled=timing_log_enabled,
                        on_finish=timing_snapshot_callback,
                    )
                )
            report_progress(progress_text)
            result = action(thread_config)
        except Exception as error:
            report_warning(f"{failure_text}：{error}")
            progress.finish_thread(task_reporter, status="失败")
            raise

    progress.finish_thread(task_reporter, status="完成")
    return result


def _run_thread_configs_sequential(
    thread_configs: list[ThreadConfig],
    progress: BackupConfigsProgressDisplay,
    *,
    action: ThreadConfigAction,
    progress_text: str,
    failure_text: str,
    write_warning_log: bool,
    write_timing_log: bool,
    timing_log_enabled: bool,
    task_name: str,
    lock_thread_output: bool,
    timing_snapshot_callback: Callable[[TimingSnapshot], None] | None,
) -> tuple[list[ThreadBatchSuccess], list[ThreadBatchFailure]]:
    successes: list[ThreadBatchSuccess] = []
    failures: list[ThreadBatchFailure] = []
    total = len(thread_configs)
    for index, thread_config in enumerate(thread_configs, start=1):
        try:
            message = _run_thread_config_with_progress(
                index=index,
                total=total,
                thread_config=thread_config,
                progress=progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
                write_warning_log=write_warning_log,
                write_timing_log=write_timing_log,
                timing_log_enabled=timing_log_enabled,
                task_name=task_name,
                lock_thread_output=lock_thread_output,
                timing_snapshot_callback=timing_snapshot_callback,
            )
        except Exception as error:
            failures.append((thread_config, error))
            continue
        if message is not None:
            successes.append((index, message))

    return successes, failures


def _run_thread_configs_parallel(
    thread_configs: list[ThreadConfig],
    worker_count: int,
    progress: BackupConfigsProgressDisplay,
    *,
    action: ThreadConfigAction,
    progress_text: str,
    failure_text: str,
    write_warning_log: bool,
    write_timing_log: bool,
    timing_log_enabled: bool,
    task_name: str,
    lock_thread_output: bool,
    timing_snapshot_callback: Callable[[TimingSnapshot], None] | None,
) -> tuple[list[ThreadBatchSuccess], list[ThreadBatchFailure]]:
    successes: list[ThreadBatchSuccess] = []
    failures: list[tuple[int, ThreadConfig, Exception]] = []
    total = len(thread_configs)
    with ThreadPoolExecutor(max_workers=min(worker_count, total)) as executor:
        future_context: dict[Future[str | None], tuple[int, ThreadConfig]] = {}
        for index, thread_config in enumerate(thread_configs, start=1):
            future = executor.submit(
                _run_thread_config_with_progress,
                index=index,
                total=total,
                thread_config=thread_config,
                progress=progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
                write_warning_log=write_warning_log,
                write_timing_log=write_timing_log,
                timing_log_enabled=timing_log_enabled,
                task_name=task_name,
                lock_thread_output=lock_thread_output,
                timing_snapshot_callback=timing_snapshot_callback,
            )
            future_context[future] = (index, thread_config)

        for future in as_completed(future_context):
            index, thread_config = future_context[future]
            try:
                message = future.result()
            except Exception as error:
                failures.append((index, thread_config, error))
                continue
            if message is not None:
                successes.append((index, message))

    ordered_failures = [
        (thread_config, error)
        for _, thread_config, error in sorted(failures, key=lambda item: item[0])
    ]
    return successes, ordered_failures


def _print_thread_batch_summary(
    *,
    total: int,
    failures: list[ThreadBatchFailure],
    summary_name: str,
) -> tuple[list[ThreadBatchFailure], list[ThreadBatchFailure]]:
    hidden_threads = [item for item in failures if is_hidden_thread_error(item[1])]
    unexpected_failures = [
        item for item in failures if not is_hidden_thread_error(item[1])
    ]
    success_count = total - len(failures)
    if hidden_threads:
        report_info(
            f"批量{summary_name}完成：成功{success_count}个，"
            f"隐藏跳过{len(hidden_threads)}个，"
            f"失败{len(unexpected_failures)}个。"
        )
    else:
        report_info(
            f"批量{summary_name}完成：成功{success_count}个，"
            f"失败{len(unexpected_failures)}个。"
        )
    if hidden_threads:
        for thread_config, error in hidden_threads:
            report_info(f"隐藏跳过：{thread_config_label(thread_config)}：{error}")
    if unexpected_failures:
        for thread_config, error in unexpected_failures:
            report_info(f"失败：{thread_config_label(thread_config)}：{error}")
    return unexpected_failures, hidden_threads


def run_thread_config_batch(
    *,
    action: ThreadConfigAction,
    progress_text: str,
    failure_text: str,
    summary_name: str,
    worker_count: int,
    write_warning_log: bool = True,
    write_timing_log: bool = False,
    timing_log_enabled: bool = True,
    task_name: str | None = None,
    lock_thread_output: bool = True,
    write_batch_timing_log: bool = False,
    thread_configs: list[ThreadConfig] | None = None,
    command_started_at: datetime | None = None,
    command_wall_start: float | None = None,
    forum_sync_seconds: float | None = None,
    planning_seconds: float | None = None,
    water_level_seconds: float | None = None,
) -> ThreadBatchResult:
    if worker_count <= 0:
        raise ValueError("workers必须大于0。")

    selected_thread_configs = (
        NGAThreadConfigs().get_thread_configs()
        if thread_configs is None
        else thread_configs
    )
    if not selected_thread_configs:
        report_info("没有找到任何帖子配置。")
        return ThreadBatchResult((), (), ())

    effective_task_name = task_name if task_name is not None else f"批量{summary_name}"
    batch_collector = (
        BatchTimingCollector()
        if write_batch_timing_log and timing_log_enabled and write_timing_log
        else None
    )
    batch_started_at = datetime.now().astimezone()
    batch_wall_start = perf_counter()
    with BackupConfigsProgressDisplay(
        len(selected_thread_configs),
        console=get_reporter().console,
    ) as progress:
        if worker_count == 1 or len(selected_thread_configs) == 1:
            successes, failures = _run_thread_configs_sequential(
                selected_thread_configs,
                progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
                write_warning_log=write_warning_log,
                write_timing_log=write_timing_log,
                timing_log_enabled=timing_log_enabled,
                task_name=effective_task_name,
                lock_thread_output=lock_thread_output,
                timing_snapshot_callback=(
                    batch_collector.add if batch_collector is not None else None
                ),
            )
        else:
            successes, failures = _run_thread_configs_parallel(
                selected_thread_configs,
                worker_count,
                progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
                write_warning_log=write_warning_log,
                write_timing_log=write_timing_log,
                timing_log_enabled=timing_log_enabled,
                task_name=effective_task_name,
                lock_thread_output=lock_thread_output,
                timing_snapshot_callback=(
                    batch_collector.add if batch_collector is not None else None
                ),
            )

    batch_wall_seconds = perf_counter() - batch_wall_start
    effective_started_at = (
        command_started_at
        if command_started_at is not None
        else batch_started_at
    )
    effective_wall_seconds = (
        perf_counter() - command_wall_start
        if command_wall_start is not None
        else batch_wall_seconds
    )
    if batch_collector is not None:
        write_batch_timing_summary(
            batch_timing_log_path(),
            task_name=effective_task_name,
            started_at=effective_started_at,
            wall_seconds=effective_wall_seconds,
            total_threads=len(selected_thread_configs),
            snapshots=batch_collector.snapshots(),
            thread_failure_categories=Counter(
                type(error).__name__
                for _, error in failures
                if not is_hidden_thread_error(error)
            ),
            expected_thread_failure_categories=Counter(
                "hidden_thread"
                for _, error in failures
                if is_hidden_thread_error(error)
            ),
            forum_sync_seconds=forum_sync_seconds,
            planning_seconds=planning_seconds,
            water_level_seconds=water_level_seconds,
            batch_execution_seconds=(
                batch_wall_seconds
                if command_wall_start is not None
                else None
            ),
        )

    for _, message in sorted(successes, key=lambda item: item[0]):
        report_info(message)
    unexpected_failures, hidden_threads = _print_thread_batch_summary(
        total=len(selected_thread_configs),
        failures=failures,
        summary_name=summary_name,
    )
    result = ThreadBatchResult(
        tuple(successes),
        tuple(unexpected_failures),
        tuple(hidden_threads),
    )
    if unexpected_failures:
        raise SystemExit(1)
    return result

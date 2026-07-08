from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed

from nga_tools.console import (
    BackupConfigsProgressDisplay,
    get_reporter,
    report_info,
    report_progress,
    report_warning,
    use_reporter,
    use_warning_log,
)
from nga_tools.core.paths import warning_log_path
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)

ThreadConfigAction = Callable[[ThreadConfig], str | None]
ThreadBatchFailure = tuple[ThreadConfig, Exception]
ThreadBatchSuccess = tuple[int, str]


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
) -> str | None:
    label = thread_config_label(thread_config)
    task_reporter = progress.start_thread(
        index=index,
        total=total,
        label=label,
    )
    log_path = warning_log_path(
        thread_config_tid(thread_config),
        thread_config_aid(thread_config),
    )
    with use_reporter(task_reporter), use_warning_log(log_path):
        report_progress(progress_text)
        try:
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
) -> None:
    success_count = total - len(failures)
    report_info(
        f"批量{summary_name}完成：成功{success_count}个，失败{len(failures)}个。"
    )
    if failures:
        for thread_config, error in failures:
            report_info(f"失败：{thread_config_label(thread_config)}：{error}")
        raise SystemExit(1)


def run_thread_config_batch(
    *,
    action: ThreadConfigAction,
    progress_text: str,
    failure_text: str,
    summary_name: str,
    worker_count: int,
) -> None:
    if worker_count <= 0:
        raise ValueError("workers必须大于0。")

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        report_info("没有找到任何帖子配置。")
        return

    with BackupConfigsProgressDisplay(
        len(thread_configs),
        console=get_reporter().console,
    ) as progress:
        if worker_count == 1 or len(thread_configs) == 1:
            successes, failures = _run_thread_configs_sequential(
                thread_configs,
                progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
            )
        else:
            successes, failures = _run_thread_configs_parallel(
                thread_configs,
                worker_count,
                progress,
                action=action,
                progress_text=progress_text,
                failure_text=failure_text,
            )

    for _, message in sorted(successes, key=lambda item: item[0]):
        report_info(message)
    _print_thread_batch_summary(
        total=len(thread_configs),
        failures=failures,
        summary_name=summary_name,
    )

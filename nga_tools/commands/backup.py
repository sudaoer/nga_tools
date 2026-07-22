from __future__ import annotations

from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from typing import Protocol

from nga_tools.backup.archive import backup_thread, backup_thread_sub
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    use_image_validation_cache,
)
from nga_tools.config import get_config, load_timing_log_enabled
from nga_tools.console import use_thread_warning_summary, use_warning_log
from nga_tools.backup.pdf import PdfRenderPool, generate_pdf
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.thread_batch import ThreadBatchResult, run_thread_config_batch
from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    required_int,
)
from nga_tools.core.output_lock import (
    use_thread_output_lock,
)
from nga_tools.core.paths import timing_log_path, warning_log_path
from nga_tools.forum.thread_configs import (
    ThreadConfig,
    thread_config_aid,
    thread_config_tid,
)
from nga_tools.ngaclient.session import ThreadLocalAPISessionPool, use_api_session
from nga_tools.ngaclient.api_runtime import use_api_runtime
from nga_tools.core.image_download_runtime import (
    use_audio_download_runtime,
    use_image_download_runtime,
)
from nga_tools.backup.image_index_writer import use_image_index_writer
from nga_tools.backup.image_store_metrics import use_image_store_metrics
from nga_tools.backup.image_store import use_image_download_coordination
from nga_tools.backup.image_store_runtime import (
    effective_image_store_workers,
    use_image_store_runtime,
)
from nga_tools.timing import use_timing_log


class BackupFetchFunc(Protocol):
    def __call__(
        self,
        tid: int,
        aid: int | None,
        *,
        write_json: bool,
        force_processing: bool = False,
    ) -> None:
        raise NotImplementedError


def _batch_worker_count(args: CommandArgs, default_worker_count: int) -> int:
    worker_arg = optional_int(args, "workers")
    return default_worker_count if worker_arg is None else worker_arg


def _thread_target_label(tid: int, aid: int | None) -> str:
    aid_text = str(aid) if aid is not None else "all"
    return f"tid={tid}, aid={aid_text}"


@contextmanager
def _use_thread_output_logs(
    *,
    task_name: str,
    tid: int,
    aid: int | None,
    timing_log_enabled: bool,
) -> Generator[None]:
    with ExitStack() as stack:
        stack.enter_context(use_thread_output_lock(tid, aid))
        stack.enter_context(
            use_thread_warning_summary(_thread_target_label(tid, aid))
        )
        stack.enter_context(use_warning_log(warning_log_path(tid, aid)))
        stack.enter_context(
            use_timing_log(
                timing_log_path(tid, aid),
                task_name=task_name,
                target=_thread_target_label(tid, aid),
                enabled=timing_log_enabled,
            )
        )
        yield


def run_backup_fetch_batch(
    args: CommandArgs,
    *,
    backup_func: BackupFetchFunc,
    progress_text: str,
    task_name: str,
    thread_configs: list[ThreadConfig] | None = None,
    raise_on_failure: bool = True,
) -> ThreadBatchResult:
    app_config = configure_network_limits_from_args(args)
    write_json = optional_bool(args, "write_json")
    force_processing = optional_bool(args, "force_processing")
    worker_count = _batch_worker_count(args, app_config.backup_configs_workers)
    validation_cache = ImageValidationCache()
    session_pool = ThreadLocalAPISessionPool()
    image_store_workers = effective_image_store_workers(
        worker_count,
        app_config.image_concurrency,
    )

    def action(thread_config: ThreadConfig) -> None:
        tid = thread_config_tid(thread_config)
        aid = thread_config_aid(thread_config)
        with (
            use_api_session(session_pool.session()),
            use_image_validation_cache(validation_cache),
        ):
            if force_processing:
                backup_func(
                    tid,
                    aid,
                    write_json=write_json,
                    force_processing=True,
                )
            else:
                backup_func(tid, aid, write_json=write_json)

    with (
        session_pool,
        use_api_runtime(app_config.api_concurrency),
        use_image_download_runtime(app_config.image_concurrency),
        use_audio_download_runtime(app_config.audio_concurrency),
        use_image_store_runtime(image_store_workers),
        use_image_index_writer(),
        use_image_store_metrics(),
        use_image_download_coordination(),
    ):
        return run_thread_config_batch(
            action=action,
            progress_text=progress_text,
            failure_text="备份失败",
            summary_name="备份",
            worker_count=worker_count,
            write_timing_log=True,
            timing_log_enabled=app_config.timing_log_enabled,
            task_name=task_name,
            write_batch_timing_log=True,
            thread_configs=thread_configs,
            raise_on_failure=raise_on_failure,
        )


def backup_all(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        run_backup_fetch_batch(
            args,
            backup_func=backup_thread,
            progress_text="正在完整备份",
            task_name="backup all --all-threads",
        )
        return

    app_config = configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    write_json = optional_bool(args, "write_json")
    force_processing = optional_bool(args, "force_processing")
    with (
        _use_thread_output_logs(
            task_name="backup all",
            tid=thread_tid,
            aid=thread_aid,
            timing_log_enabled=app_config.timing_log_enabled,
        ),
        use_api_runtime(app_config.api_concurrency),
        use_image_validation_cache(),
        use_image_download_runtime(app_config.image_concurrency),
        use_audio_download_runtime(app_config.audio_concurrency),
        use_image_store_runtime(1),
        use_image_index_writer(),
        use_image_store_metrics(),
        use_image_download_coordination(),
    ):
        if force_processing:
            backup_thread(
                thread_tid,
                thread_aid,
                write_json=write_json,
                force_processing=True,
            )
        else:
            backup_thread(thread_tid, thread_aid, write_json=write_json)


def backup_sub(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        run_backup_fetch_batch(
            args,
            backup_func=backup_thread_sub,
            progress_text="正在增量备份",
            task_name="backup sub --all-threads",
        )
        return

    app_config = configure_network_limits_from_args(args)
    thread_tid, thread_aid = resolve_command_thread_target(args)
    write_json = optional_bool(args, "write_json")
    force_processing = optional_bool(args, "force_processing")
    with (
        _use_thread_output_logs(
            task_name="backup sub",
            tid=thread_tid,
            aid=thread_aid,
            timing_log_enabled=app_config.timing_log_enabled,
        ),
        use_api_runtime(app_config.api_concurrency),
        use_image_validation_cache(),
        use_image_download_runtime(app_config.image_concurrency),
        use_audio_download_runtime(app_config.audio_concurrency),
        use_image_store_runtime(1),
        use_image_index_writer(),
        use_image_store_metrics(),
        use_image_download_coordination(),
    ):
        if force_processing:
            backup_thread_sub(
                thread_tid,
                thread_aid,
                write_json=write_json,
                force_processing=True,
            )
        else:
            backup_thread_sub(thread_tid, thread_aid, write_json=write_json)


def pdf_generate(args: CommandArgs) -> None:
    if optional_bool(args, "all_threads"):
        app_config = get_config()
        worker_count = _batch_worker_count(
            args,
            app_config.backup_configs_workers,
        )
        lou_per_pdf = required_int(args, "lou_per_pdf")
        pdf_workers = optional_int(args, "pdf_workers")

        with PdfRenderPool(pdf_workers) as pdf_renderer:
            def action(thread_config: ThreadConfig) -> None:
                generate_pdf(
                    tid=thread_config_tid(thread_config),
                    aid=thread_config_aid(thread_config),
                    lou_per_pdf=lou_per_pdf,
                    pdf_workers=pdf_workers,
                    pdf_renderer=pdf_renderer,
                )

            run_thread_config_batch(
                action=action,
                progress_text="正在生成PDF",
                failure_text="PDF生成失败",
                summary_name="PDF生成",
                worker_count=worker_count,
                write_timing_log=True,
                timing_log_enabled=app_config.timing_log_enabled,
                task_name="backup pdf --all-threads",
            )
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    with _use_thread_output_logs(
        task_name="backup pdf",
        tid=thread_tid,
        aid=thread_aid,
        timing_log_enabled=load_timing_log_enabled(),
    ):
        generate_pdf(
            tid=thread_tid,
            aid=thread_aid,
            lou_per_pdf=required_int(args, "lou_per_pdf"),
            pdf_workers=optional_int(args, "pdf_workers"),
        )

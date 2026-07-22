from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from time import perf_counter
from typing import Literal

from nga_tools.backup.archive import (
    backup_local_work_kind,
    backup_thread,
    backup_thread_sub,
    maintain_thread_backup,
)
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    use_image_validation_cache,
)
from nga_tools.commands.forum import sync_default_forum_watch
from nga_tools.commands.network import configure_network_limits_from_args
from nga_tools.commands.thread_batch import run_thread_config_batch
from nga_tools.commands.types import CommandArgs, optional_bool, optional_int
from nga_tools.console import report_info
from nga_tools.forum.ankebak_state import (
    AnkebakStateStore,
    AnkebakThreadState,
    ankebak_target_key,
)
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_tid,
)
from nga_tools.ngaclient.client import ForumThread
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


AnkebakMode = Literal["full", "sub", "maintenance"]


@dataclass(frozen=True)
class AnkebakJob:
    thread_config: ThreadConfig
    mode: AnkebakMode | None
    fresh_thread: ForumThread | None
    planning_error: Exception | None = None


def _worker_count(args: CommandArgs, default_worker_count: int) -> int:
    worker_arg = optional_int(args, "workers")
    return default_worker_count if worker_arg is None else worker_arg


def _jobs_for_threads(
    thread_configs: list[ThreadConfig],
    fresh_threads: tuple[ForumThread, ...],
    states: dict[str, AnkebakThreadState],
    *,
    now: datetime,
    full_backup_interval_hours: int,
) -> tuple[list[AnkebakJob], int]:
    fresh_by_tid = {thread["tid"]: thread for thread in fresh_threads}
    jobs: list[AnkebakJob] = []
    skipped_count = 0

    for thread_config in thread_configs:
        tid = thread_config_tid(thread_config)
        aid = thread_config_aid(thread_config)
        state = states.get(ankebak_target_key(tid, aid))
        fresh_thread = fresh_by_tid.get(tid)
        if (
            fresh_thread is not None
            and aid is not None
            and fresh_thread["authorid"] != aid
        ):
            fresh_thread = None

        if state is None or state.full_backup_schedule_decision(
            now,
            full_backup_interval_hours,
        ).should_run:
            jobs.append(AnkebakJob(thread_config, "full", fresh_thread))
            continue

        if fresh_thread is not None and not state.forum_signature_matches(
            fresh_thread
        ):
            jobs.append(AnkebakJob(thread_config, "sub", fresh_thread))
            continue

        try:
            local_work = backup_local_work_kind(tid, aid, now=now)
        except Exception as error:
            jobs.append(
                AnkebakJob(
                    thread_config,
                    None,
                    fresh_thread,
                    planning_error=error,
                )
            )
            continue
        if local_work == "refresh":
            jobs.append(AnkebakJob(thread_config, "sub", fresh_thread))
        elif local_work == "maintenance":
            jobs.append(AnkebakJob(thread_config, "maintenance", fresh_thread))
        else:
            skipped_count += 1

    return jobs, skipped_count


def backup_auto(args: CommandArgs) -> None:
    command_started_at = datetime.now().astimezone()
    command_wall_start = perf_counter()

    app_config = configure_network_limits_from_args(args)

    forum_sync_start = perf_counter()
    forum_result = sync_default_forum_watch(args)
    forum_sync_seconds = perf_counter() - forum_sync_start

    thread_configs = NGAThreadConfigs().get_thread_configs()
    if not thread_configs:
        report_info("没有找到任何帖子配置。")
        return

    planning_start = perf_counter()
    now = datetime.now().astimezone()
    state_store = AnkebakStateStore()

    water_level_start = perf_counter()
    states = state_store.load_states()
    water_level_seconds = perf_counter() - water_level_start

    jobs, skipped_count = _jobs_for_threads(
        thread_configs,
        forum_result.fresh_threads,
        states,
        now=now,
        full_backup_interval_hours=(
            app_config.ankebak_full_backup_interval_hours
        ),
    )
    mode_counts: dict[AnkebakMode, int] = {
        "full": 0,
        "sub": 0,
        "maintenance": 0,
    }
    for job in jobs:
        if job.mode is not None:
            mode_counts[job.mode] += 1
    planning_failure_count = sum(
        job.planning_error is not None for job in jobs
    )
    report_info(
        f"ankebak任务选择：配置{len(thread_configs)}个，"
        f"本轮新鲜主题{len(forum_result.fresh_threads)}个；"
        f"概率/到期完整备份{mode_counts['full']}个，"
        f"增量备份{mode_counts['sub']}个，"
        f"本地维护{mode_counts['maintenance']}个，"
        f"本地检查失败{planning_failure_count}个，"
        f"无变化跳过{skipped_count}个。"
    )
    planning_seconds = perf_counter() - planning_start
    if not jobs:
        report_info("没有需要执行的ankebak任务。")
        return

    jobs_by_target = {
        ankebak_target_key(
            thread_config_tid(job.thread_config),
            thread_config_aid(job.thread_config),
        ): job
        for job in jobs
    }
    validation_cache = ImageValidationCache()
    session_pool = ThreadLocalAPISessionPool()
    write_json = optional_bool(args, "write_json")
    worker_count = _worker_count(args, app_config.backup_configs_workers)
    image_store_workers = effective_image_store_workers(
        worker_count,
        app_config.image_concurrency,
    )

    def action(thread_config: ThreadConfig) -> None:
        tid = thread_config_tid(thread_config)
        aid = thread_config_aid(thread_config)
        job = jobs_by_target[ankebak_target_key(tid, aid)]
        if job.planning_error is not None:
            raise job.planning_error
        with (
            use_api_session(session_pool.session()),
            use_image_validation_cache(validation_cache),
        ):
            if job.mode == "full":
                backup_thread(
                    tid,
                    aid,
                    write_json=write_json,
                    schedule_missing_floor_retries=True,
                )
            elif job.mode == "sub":
                backup_thread_sub(
                    tid,
                    aid,
                    write_json=write_json,
                    allow_unchanged_author_fast_path=True,
                    schedule_missing_floor_retries=True,
                )
            else:
                maintain_thread_backup(
                    tid,
                    aid,
                    schedule_missing_floor_retries=True,
                )

        state_store.record_success(
            tid=tid,
            aid=aid,
            forum_thread=job.fresh_thread,
            completed_at=datetime.now().astimezone(),
            full_backup=job.mode == "full",
        )

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
        run_thread_config_batch(
            action=action,
            progress_text="正在执行智能备份",
            failure_text="ankebak失败",
            summary_name="ankebak",
            worker_count=worker_count,
            write_timing_log=True,
            timing_log_enabled=app_config.timing_log_enabled,
            task_name="backup auto",
            write_batch_timing_log=True,
            thread_configs=[job.thread_config for job in jobs],
            command_started_at=command_started_at,
            command_wall_start=command_wall_start,
            forum_sync_seconds=forum_sync_seconds,
            forum_sync_timing=forum_result.timing,
            planning_seconds=planning_seconds,
            water_level_seconds=water_level_seconds,
        )

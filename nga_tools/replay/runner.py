from __future__ import annotations

from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import TypeAlias, cast

import requests

from nga_tools import network_limits
from nga_tools.backup.archive import backup_thread
from nga_tools.commands.backup import run_backup_fetch_batch
from nga_tools.commands.thread_batch import ThreadBatchResult
from nga_tools.commands.types import (
    CommandArgs,
    optional_bool,
    optional_int,
    optional_str,
    required_str,
)
from nga_tools.config import get_config, use_config_override
from nga_tools.console import report_info
from nga_tools.core.atomic import write_json_atomically
from nga_tools.forum.thread_configs import (
    NGAThreadConfigs,
    ThreadConfig,
    thread_config_aid,
    thread_config_name,
    thread_config_tid,
)
from nga_tools.replay.offline import (
    normalized_server_url,
    use_replay_network_policy,
)
from nga_tools.replay.state import (
    InitialState,
    PreparationStats,
    prepare_target_state,
    validate_source_target_paths,
)
from nga_tools.replay.validation import ValidationStats, validate_replay_output
from nga_tools.timing import TimingSnapshot, git_commit_id

JsonObject: TypeAlias = dict[str, object]
_INITIAL_STATES: set[str] = {"empty", "warm", "existing"}


class ReplayServerClient:
    def __init__(self, server_url: str) -> None:
        self.server_url = normalized_server_url(server_url)
        self.session = requests.Session()
        self.session.trust_env = False

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> ReplayServerClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def _json_response(self, method: str, path: str) -> JsonObject:
        response = self.session.request(
            method,
            f"{self.server_url}{path}",
            timeout=30,
        )
        response.raise_for_status()
        value: object = response.json()
        if not isinstance(value, dict):
            raise ValueError(f"重放服务返回的{path}不是JSON对象。")
        data = cast(dict[object, object], value)
        if not all(isinstance(key, str) for key in data):
            raise ValueError(f"重放服务返回的{path}包含非字符串键。")
        return cast(JsonObject, data)

    def health(self) -> JsonObject:
        return self._json_response("GET", "/__replay__/health")

    def manifest(self) -> JsonObject:
        return self._json_response("GET", "/__replay__/manifest")

    def metrics(self) -> JsonObject:
        return self._json_response("GET", "/__replay__/metrics")

    def reset(self) -> JsonObject:
        return self._json_response("POST", "/__replay__/reset")


def _require_manifest_string(manifest: JsonObject, key: str) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"重放服务manifest缺少字符串字段：{key}")
    return value


def _validate_server_corpus(
    client: ReplayServerClient,
    source_output: Path,
    thread_config_path: Path,
) -> JsonObject:
    health = client.health()
    if health.get("status") != "ok":
        raise RuntimeError(f"重放服务未就绪：{health}")
    manifest = client.manifest()
    corpus_id = _require_manifest_string(manifest, "corpus_id")
    profile_id = _require_manifest_string(manifest, "profile_id")
    if health.get("corpus_id") != corpus_id:
        raise RuntimeError("重放服务health与manifest的corpus_id不一致。")
    if health.get("profile_id") != profile_id:
        raise RuntimeError("重放服务health与manifest的profile_id不一致。")
    manifest_source = Path(_require_manifest_string(manifest, "source_output")).resolve()
    manifest_thread_config = Path(
        _require_manifest_string(manifest, "thread_config")
    ).resolve()
    if manifest_source != source_output.resolve():
        raise ValueError(
            "重放服务语料与source-output不一致："
            f"server={manifest_source}, runner={source_output.resolve()}"
        )
    if manifest_thread_config != thread_config_path.resolve():
        raise ValueError(
            "重放服务语料与thread-config不一致："
            f"server={manifest_thread_config}, runner={thread_config_path.resolve()}"
        )
    return manifest


def _selected_thread_configs(args: CommandArgs) -> list[ThreadConfig]:
    configured = NGAThreadConfigs().get_thread_configs()
    if optional_bool(args, "all_threads"):
        return configured

    name = optional_str(args, "name")
    if name is not None:
        for thread_config in configured:
            if thread_config_name(thread_config) == name:
                return [thread_config]
        raise ValueError(f"未找到名称为{name}的帖子配置。")

    tid = optional_int(args, "tid")
    if tid is None:
        raise ValueError("必须通过--name、--tid或--all-threads选择帖子。")
    aid = optional_int(args, "aid")
    return [
        {
            "thread_name": f"replay-{tid}-{aid if aid is not None else 'all'}",
            "tid": tid,
            "aid": aid,
        }
    ]


def _processing_reuse_counts(
    snapshots: tuple[TimingSnapshot, ...],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for snapshot in snapshots:
        for label, value in snapshot.labels:
            if label == "处理状态复用结果":
                counts[value] += 1
    return dict(sorted(counts.items()))


def _image_failure_counts(
    snapshots: tuple[TimingSnapshot, ...],
) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for snapshot in snapshots:
        for metric, value in snapshot.metrics:
            if metric.startswith("图片下载失败/"):
                counts[metric.removeprefix("图片下载失败/")] += value
    return dict(sorted(counts.items()))


def _failure_payload(result: ThreadBatchResult) -> list[dict[str, object]]:
    failures = [*result.failures, *result.hidden_threads]
    return [
        {
            "tid": thread_config_tid(thread_config),
            "aid": thread_config_aid(thread_config),
            "kind": type(error).__name__,
            "message": str(error),
            "expected_hidden_thread": (thread_config, error) in result.hidden_threads,
        }
        for thread_config, error in failures
    ]


def _new_report_path(target_output: Path, started_at: datetime) -> Path:
    timestamp = started_at.strftime("%Y%m%dT%H%M%S%f")
    path = target_output / f"replay_run-{timestamp}.json"
    suffix = 2
    while path.exists():
        path = target_output / f"replay_run-{timestamp}-{suffix}.json"
        suffix += 1
    return path


def _write_run_report(
    *,
    target_output: Path,
    source_output: Path,
    thread_config_path: Path,
    server_url: str,
    manifest: JsonObject,
    preparation: PreparationStats,
    started_at: datetime,
    ended_at: datetime,
    backup_wall_seconds: float,
    worker_count: int,
    api_concurrency: int,
    image_concurrency: int,
    selected_configs: list[ThreadConfig],
    result: ThreadBatchResult,
    metrics_baseline: JsonObject,
    metrics_final: JsonObject,
    validation: ValidationStats | None,
    validation_error: str | None,
) -> Path:
    failures = len(result.failures)
    hidden_threads = len(result.hidden_threads)
    success_count = len(selected_configs) - failures - hidden_threads
    report_path = _new_report_path(target_output, started_at)
    payload: JsonObject = {
        "format_version": 1,
        "started_at": started_at.isoformat(),
        "ended_at": ended_at.isoformat(),
        "status": "failed" if failures or validation_error else "completed",
        "git_commit": git_commit_id(),
        "corpus_id": _require_manifest_string(manifest, "corpus_id"),
        "profile_hash": _require_manifest_string(manifest, "profile_id"),
        "profile": manifest.get("profile"),
        "server_url": server_url,
        "source_output": str(source_output.resolve()),
        "target_output": str(target_output.resolve()),
        "thread_config": str(thread_config_path.resolve()),
        "initial_state": preparation.initial_state,
        "concurrency": {
            "workers": worker_count,
            "api_concurrency": api_concurrency,
            "image_concurrency": image_concurrency,
        },
        "preparation": preparation.as_dict(),
        "backup_wall_seconds": backup_wall_seconds,
        "validation": None if validation is None else validation.as_dict(),
        "validation_error": validation_error,
        "threads": {
            "total": len(selected_configs),
            "successful": success_count,
            "failed": failures,
            "hidden_skipped": hidden_threads,
            "failures": _failure_payload(result),
        },
        "server_metrics_baseline": metrics_baseline,
        "server_metrics": metrics_final,
        "processing_state_reuse": _processing_reuse_counts(
            result.timing_snapshots
        ),
        "image_failure_categories": _image_failure_counts(
            result.timing_snapshots
        ),
        "batch_timing_log": (
            None
            if result.batch_timing_path is None
            else str(result.batch_timing_path.resolve())
        ),
    }
    write_json_atomically(report_path, payload, indent=2, trailing_newline=True)
    return report_path


def _initial_state(args: CommandArgs) -> InitialState:
    value = required_str(args, "initial_state")
    if value not in _INITIAL_STATES:
        raise ValueError("initial-state必须是empty、warm或existing。")
    return cast(InitialState, value)


def run_replay_backup(args: CommandArgs) -> None:
    source_output = Path(required_str(args, "source_output")).resolve()
    target_output = Path(required_str(args, "target_output")).resolve()
    server_url = normalized_server_url(required_str(args, "server_url"))
    thread_config_arg = optional_str(args, "thread_config")
    default_config = get_config()
    thread_config_path = Path(
        default_config.thread_config_file
        if thread_config_arg is None
        else thread_config_arg
    ).resolve()
    initial_state = _initial_state(args)
    validate_source_target_paths(source_output, target_output)

    worker_count = optional_int(args, "workers") or default_config.backup_configs_workers
    api_concurrency = optional_int(args, "api_concurrency") or default_config.api_concurrency
    image_concurrency = (
        optional_int(args, "image_concurrency") or default_config.image_concurrency
    )
    replay_config = replace(
        default_config,
        base_url=server_url,
        output_dir=str(target_output),
        thread_config_file=str(thread_config_path),
        nga_passport_uid="",
        nga_passport_cid="",
        api_concurrency=api_concurrency,
        image_concurrency=image_concurrency,
        backup_configs_workers=worker_count,
    )

    with ReplayServerClient(server_url) as client:
        manifest = _validate_server_corpus(
            client,
            source_output,
            thread_config_path,
        )
        report_info(
            "已校验重放服务："
            f"corpus_id={_require_manifest_string(manifest, 'corpus_id')}，"
            f"profile_hash={_require_manifest_string(manifest, 'profile_id')}"
        )
        with use_config_override(replay_config):
            selected_configs = _selected_thread_configs(args)
        if not selected_configs:
            raise ValueError("没有找到任何帖子配置。")
        preparation = prepare_target_state(
            initial_state,
            source_output,
            target_output,
        )
        report_info(
            f"目标状态准备完成：{initial_state}，"
            f"耗时{preparation.elapsed_seconds:.3f}s。"
        )

        previous_api_concurrency = network_limits.get_api_concurrency()
        previous_image_concurrency = network_limits.get_image_concurrency()
        try:
            with (
                use_config_override(replay_config),
                use_replay_network_policy(server_url),
            ):
                client.reset()
                metrics_baseline = client.metrics()
                started_at = datetime.now().astimezone()
                wall_start = perf_counter()
                result = run_backup_fetch_batch(
                    {
                        "workers": worker_count,
                        "api_concurrency": api_concurrency,
                        "image_concurrency": image_concurrency,
                    },
                    backup_func=backup_thread,
                    progress_text="正在执行离线完整备份",
                    task_name="replay run backup all",
                    thread_configs=selected_configs,
                    raise_on_failure=False,
                )
                backup_wall_seconds = perf_counter() - wall_start
                failed_targets = {
                    (thread_config_tid(thread_config), thread_config_aid(thread_config))
                    for thread_config, _error in (
                        *result.failures,
                        *result.hidden_threads,
                    )
                }
                validation_configs = [
                    thread_config
                    for thread_config in selected_configs
                    if (
                        thread_config_tid(thread_config),
                        thread_config_aid(thread_config),
                    )
                    not in failed_targets
                ]
                validation: ValidationStats | None = None
                validation_error: str | None = None
                try:
                    validation = validate_replay_output(
                        source_output,
                        target_output,
                        validation_configs,
                        initial_state,
                    )
                except Exception as error:
                    validation_error = f"{type(error).__name__}: {error}"
                metrics_final = client.metrics()
                ended_at = datetime.now().astimezone()
                report_path = _write_run_report(
                    target_output=target_output,
                    source_output=source_output,
                    thread_config_path=thread_config_path,
                    server_url=server_url,
                    manifest=manifest,
                    preparation=preparation,
                    started_at=started_at,
                    ended_at=ended_at,
                    backup_wall_seconds=backup_wall_seconds,
                    worker_count=worker_count,
                    api_concurrency=api_concurrency,
                    image_concurrency=image_concurrency,
                    selected_configs=selected_configs,
                    result=result,
                    metrics_baseline=metrics_baseline,
                    metrics_final=metrics_final,
                    validation=validation,
                    validation_error=validation_error,
                )
        finally:
            network_limits.configure_network_limits(
                api_concurrency=previous_api_concurrency,
                image_concurrency=previous_image_concurrency,
            )

    if result.failures or validation_error is not None:
        report_info(
            f"离线重放备份失败：墙钟时间{backup_wall_seconds:.3f}s，"
            f"报告：{report_path}"
        )
        raise SystemExit(1)
    report_info(
        f"离线重放备份完成：墙钟时间{backup_wall_seconds:.3f}s，"
        f"报告：{report_path}"
    )

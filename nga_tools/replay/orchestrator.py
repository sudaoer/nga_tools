from __future__ import annotations

import multiprocessing
import socket
import threading
import time
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from multiprocessing.synchronize import Event
from pathlib import Path
from typing import cast

import requests
import uvicorn

from nga_tools.commands.types import (
    CommandArgs,
    optional_int,
    optional_str,
    required_str,
)
from nga_tools.config import get_config
from nga_tools.console import report_info, use_command_warning_summary
from nga_tools.replay.profile import ReplayProfile, load_replay_profile
from nga_tools.replay.runner import ReplayServerClient, run_replay_backup
from nga_tools.replay.server import (
    DEFAULT_REPLAY_HOST,
    load_replay_app,
)
from nga_tools.replay.state import validate_source_target_paths

_STARTUP_TIMEOUT_SECONDS = 300.0
_HEALTH_REQUEST_TIMEOUT_SECONDS = 1.0
_POLL_INTERVAL_SECONDS = 0.1
_GRACEFUL_SHUTDOWN_SECONDS = 10.0
_FORCED_SHUTDOWN_SECONDS = 5.0
_INITIAL_STATES = {"empty", "warm", "existing"}


def _bind_replay_socket(port: int | None) -> socket.socket:
    replay_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        replay_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replay_socket.bind((DEFAULT_REPLAY_HOST, 0 if port is None else port))
        replay_socket.listen(128)
    except BaseException:
        replay_socket.close()
        raise
    return replay_socket


def _request_server_stop(stop_event: Event, server: uvicorn.Server) -> None:
    stop_event.wait()
    server.should_exit = True


def _serve_replay_process(
    source_output: Path,
    thread_config_path: Path,
    profile: ReplayProfile,
    port: int | None,
    startup_connection: Connection,
    stop_event: Event,
) -> None:
    startup_message_sent = False
    replay_socket: socket.socket | None = None
    try:
        replay_socket = _bind_replay_socket(port)
        socket_address = replay_socket.getsockname()
        actual_port = int(socket_address[1])
        app = load_replay_app(
            source_output=source_output,
            thread_config_path=thread_config_path,
            profile=profile,
        )
        if stop_event.is_set():
            return

        server_url = f"http://{DEFAULT_REPLAY_HOST}:{actual_port}"
        report_info(f"重放服务：{server_url}/")
        startup_connection.send(("starting", actual_port))
        startup_message_sent = True
        startup_connection.close()

        server = uvicorn.Server(
            uvicorn.Config(app, log_level="warning")
        )
        stop_thread = threading.Thread(
            target=_request_server_stop,
            args=(stop_event, server),
            daemon=True,
        )
        stop_thread.start()
        server.run(sockets=[replay_socket])
    except BaseException as error:
        if not startup_message_sent:
            try:
                startup_connection.send(
                    ("error", f"{type(error).__name__}: {error}")
                )
            except (BrokenPipeError, EOFError, OSError):
                pass
        raise
    finally:
        startup_connection.close()
        if replay_socket is not None:
            replay_socket.close()


def _run_replay_process(args: CommandArgs) -> None:
    with use_command_warning_summary():
        run_replay_backup(args)


def _receive_startup_port(
    connection: Connection,
    process: BaseProcess,
    deadline: float,
) -> int:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"重放服务未在{_STARTUP_TIMEOUT_SECONDS:.0f}秒内完成语料加载。"
            )
        if not connection.poll(min(_POLL_INTERVAL_SECONDS, remaining)):
            continue

        try:
            raw_message: object = connection.recv()
        except EOFError as error:
            exit_code = process.exitcode
            raise RuntimeError(
                "重放服务进程未报告监听端口"
                + ("。" if exit_code is None else f"，退出码：{exit_code}。")
            ) from error
        if not isinstance(raw_message, tuple):
            raise RuntimeError("重放服务进程返回了无效的启动消息。")
        message = cast(tuple[object, object], raw_message)
        if len(message) != 2 or not isinstance(message[0], str):
            raise RuntimeError("重放服务进程返回了无效的启动消息。")
        status, value = message
        if status == "error" and isinstance(value, str):
            raise RuntimeError(f"重放服务启动失败：{value}")
        if status == "starting" and type(value) is int:
            return value
        raise RuntimeError("重放服务进程返回了无效的启动消息。")


def _wait_until_healthy(
    server_url: str,
    process: BaseProcess,
    deadline: float,
) -> None:
    last_error = "服务尚未接受请求"
    with ReplayServerClient(
        server_url,
        timeout_seconds=_HEALTH_REQUEST_TIMEOUT_SECONDS,
    ) as client:
        while True:
            exit_code = process.exitcode
            if exit_code is not None:
                raise RuntimeError(
                    f"重放服务进程在就绪前退出，退出码：{exit_code}。"
                )
            try:
                health = client.health()
                if health.get("status") == "ok":
                    return
                last_error = f"health返回：{health}"
            except (requests.RequestException, ValueError) as error:
                last_error = f"{type(error).__name__}: {error}"

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"重放服务未在{_STARTUP_TIMEOUT_SECONDS:.0f}秒内就绪："
                    f"{last_error}"
                )
            time.sleep(min(_POLL_INTERVAL_SECONDS, remaining))


def _terminate_process(process: BaseProcess) -> None:
    if not process.is_alive():
        process.join(timeout=0)
        return
    process.terminate()
    process.join(timeout=_FORCED_SHUTDOWN_SECONDS)
    if process.is_alive():
        process.kill()
        process.join(timeout=_FORCED_SHUTDOWN_SECONDS)


def _stop_service(process: BaseProcess, stop_event: Event) -> None:
    stop_event.set()
    process.join(timeout=_GRACEFUL_SHUTDOWN_SECONDS)
    if process.is_alive():
        _terminate_process(process)


def _monitor_replay_run(
    service_process: BaseProcess,
    runner_process: BaseProcess,
) -> int:
    while True:
        runner_process.join(timeout=_POLL_INTERVAL_SECONDS)
        runner_exit_code = runner_process.exitcode
        if runner_exit_code is not None:
            return runner_exit_code
        service_exit_code = service_process.exitcode
        if service_exit_code is not None:
            raise RuntimeError(
                "重放服务进程在模拟运行期间退出，"
                f"退出码：{service_exit_code}。"
            )


def _runner_args(
    args: CommandArgs,
    *,
    server_url: str,
    source_output: Path,
    target_output: Path,
    thread_config_path: Path,
) -> CommandArgs:
    runner_args: CommandArgs = {
        "server_url": server_url,
        "source_output": str(source_output),
        "target_output": str(target_output),
        "thread_config": str(thread_config_path),
        "initial_state": required_str(args, "initial_state"),
    }
    for key in (
        "name",
        "tid",
        "aid",
        "all_threads",
        "workers",
        "api_concurrency",
        "image_concurrency",
    ):
        value = args.get(key)
        if value is not None:
            runner_args[key] = value
    return runner_args


def run_replay_test(args: CommandArgs) -> None:
    source_output = Path(required_str(args, "source_output")).resolve()
    target_output = Path(required_str(args, "target_output")).absolute()
    profile_path = Path(required_str(args, "profile")).resolve()
    thread_config_arg = optional_str(args, "thread_config")
    thread_config_path = Path(
        get_config().thread_config_file
        if thread_config_arg is None
        else thread_config_arg
    ).resolve()
    initial_state = required_str(args, "initial_state")
    if initial_state not in _INITIAL_STATES:
        raise ValueError("initial-state必须是empty、warm或existing。")
    port = optional_int(args, "port")
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("--port必须在1到65535之间。")

    validate_source_target_paths(source_output, target_output)
    profile = load_replay_profile(profile_path)
    context = multiprocessing.get_context("spawn")
    stop_event = context.Event()
    receive_connection, send_connection = context.Pipe(duplex=False)
    service_process = context.Process(
        target=_serve_replay_process,
        args=(
            source_output,
            thread_config_path,
            profile,
            port,
            send_connection,
            stop_event,
        ),
        name="nga-replay-service",
    )
    runner_process: BaseProcess | None = None
    service_started = False
    runner_started = False
    interrupted = False
    runner_exit_code: int | None = None
    try:
        service_process.start()
        service_started = True
        send_connection.close()
        startup_deadline = time.monotonic() + _STARTUP_TIMEOUT_SECONDS
        actual_port = _receive_startup_port(
            receive_connection,
            service_process,
            startup_deadline,
        )
        receive_connection.close()
        server_url = f"http://{DEFAULT_REPLAY_HOST}:{actual_port}"
        _wait_until_healthy(server_url, service_process, startup_deadline)
        report_info(f"重放服务已就绪：{server_url}/")

        runner_process = context.Process(
            target=_run_replay_process,
            args=(
                _runner_args(
                    args,
                    server_url=server_url,
                    source_output=source_output,
                    target_output=target_output,
                    thread_config_path=thread_config_path,
                ),
            ),
            name="nga-replay-runner",
        )
        runner_process.start()
        runner_started = True
        runner_exit_code = _monitor_replay_run(
            service_process,
            runner_process,
        )
    except KeyboardInterrupt:
        interrupted = True
    finally:
        receive_connection.close()
        send_connection.close()
        if runner_started and runner_process is not None and runner_process.is_alive():
            _terminate_process(runner_process)
        if service_started:
            _stop_service(service_process, stop_event)

    if interrupted:
        raise SystemExit(130)
    if runner_exit_code is None:
        raise RuntimeError("模拟运行进程未返回退出码。")
    if runner_exit_code != 0:
        raise SystemExit(runner_exit_code)

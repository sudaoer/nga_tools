from __future__ import annotations

import threading
from _thread import LockType
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from filelock import FileLock, Timeout

from nga_tools.core import paths

LOCK_FILENAME = ".nga_tools.lock"
OUTPUT_ROOT_LOCK_FILENAME = ".nga_tools-output.lock"

_PROCESS_LOCKS: dict[Path, LockType] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class ThreadOutputLockError(RuntimeError):
    pass


def thread_output_lock_path(tid: int, aid: int | None) -> Path:
    return output_folder_lock_path(Path(paths.get_folder(tid, aid)))


def output_folder_lock_path(folder: Path) -> Path:
    return folder / LOCK_FILENAME


def output_root_lock_path(output_root: Path) -> Path:
    return output_root / OUTPUT_ROOT_LOCK_FILENAME


def _lock_key(lock_path: Path) -> Path:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    return lock_path.resolve(strict=False)


def _process_lock_for(lock_path: Path) -> LockType:
    key = _lock_key(lock_path)
    with _PROCESS_LOCKS_GUARD:
        process_lock = _PROCESS_LOCKS.get(key)
        if process_lock is None:
            process_lock = threading.Lock()
            _PROCESS_LOCKS[key] = process_lock
        return process_lock


def _acquire_process_lock(process_lock: LockType, timeout: float) -> bool:
    if timeout == 0:
        return process_lock.acquire(blocking=False)
    if timeout < 0:
        process_lock.acquire()
        return True
    return process_lock.acquire(timeout=timeout)


def _lock_error(folder: Path, lock_path: Path) -> ThreadOutputLockError:
    return ThreadOutputLockError(
        f"输出目录正在被另一个任务使用：{folder}。"
        f"锁文件：{lock_path}。请等待当前任务结束后重试。"
    )


@contextmanager
def use_output_folder_lock(
    folder: Path,
    *,
    timeout: float = 0,
) -> Generator[None, None, None]:
    folder.mkdir(parents=True, exist_ok=True)
    lock_path = output_folder_lock_path(folder)
    process_lock = _process_lock_for(lock_path)
    if not _acquire_process_lock(process_lock, timeout):
        raise _lock_error(folder, lock_path)

    file_lock = FileLock(str(lock_path))
    try:
        try:
            with file_lock.acquire(timeout=timeout):
                yield
        except Timeout as error:
            raise _lock_error(folder, lock_path) from error
    finally:
        process_lock.release()


@contextmanager
def use_thread_output_lock(
    tid: int,
    aid: int | None,
    *,
    timeout: float = 0,
) -> Generator[None, None, None]:
    folder = Path(paths.get_folder(tid, aid))
    with use_output_folder_lock(folder, timeout=timeout):
        yield


@contextmanager
def use_output_root_lock(
    output_root: Path,
    *,
    timeout: float = 0,
) -> Generator[None, None, None]:
    output_root.mkdir(parents=True, exist_ok=True)
    lock_path = output_root_lock_path(output_root)
    process_lock = _process_lock_for(lock_path)
    if not _acquire_process_lock(process_lock, timeout):
        raise _lock_error(output_root, lock_path)

    file_lock = FileLock(str(lock_path))
    try:
        try:
            with file_lock.acquire(timeout=timeout):
                yield
        except Timeout as error:
            raise _lock_error(output_root, lock_path) from error
    finally:
        process_lock.release()

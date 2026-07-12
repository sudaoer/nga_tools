from __future__ import annotations

from nga_tools.backup.image_verify import (
    verify_all_downloaded_images,
    verify_downloaded_images,
)
from nga_tools.config import load_timing_log_enabled
from nga_tools.console import use_thread_warning_summary, use_warning_log
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import CommandArgs, optional_int, optional_str
from nga_tools.core.output_lock import use_thread_output_lock
from nga_tools.core.paths import timing_log_path, warning_log_path
from nga_tools.timing import use_timing_log


def image_verify(args: CommandArgs) -> None:
    name = optional_str(args, "name")
    tid = optional_int(args, "tid")
    aid = optional_int(args, "aid")
    if not name and tid is None:
        if aid is not None:
            raise ValueError("--aid必须与--tid或--name一起使用。")
        verify_all_downloaded_images()
        return

    thread_tid, thread_aid = resolve_command_thread_target(args)
    aid_text = str(thread_aid) if thread_aid is not None else "all"
    with (
        use_thread_output_lock(thread_tid, thread_aid),
        use_thread_warning_summary(f"tid={thread_tid}, aid={aid_text}"),
        use_warning_log(warning_log_path(thread_tid, thread_aid)),
        use_timing_log(
            timing_log_path(thread_tid, thread_aid),
            task_name="image verify",
            target=f"tid={thread_tid}, aid={aid_text}",
            enabled=load_timing_log_enabled(),
        ),
    ):
        verify_downloaded_images(thread_tid, thread_aid)

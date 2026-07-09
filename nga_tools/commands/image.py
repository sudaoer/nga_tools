from __future__ import annotations

from nga_tools.backup.images import (
    migrate_image_index,
    prune_legacy_image_links,
)
from nga_tools.backup.image_verify import (
    verify_all_downloaded_images,
    verify_downloaded_images,
)
from nga_tools.config import load_timing_log_enabled
from nga_tools.console import report_info, use_warning_log
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
        use_warning_log(warning_log_path(thread_tid, thread_aid)),
        use_timing_log(
            timing_log_path(thread_tid, thread_aid),
            task_name="image verify",
            target=f"tid={thread_tid}, aid={aid_text}",
            enabled=load_timing_log_enabled(),
        ),
    ):
        verify_downloaded_images(thread_tid, thread_aid)


def image_migrate(args: CommandArgs) -> None:
    del args
    result = migrate_image_index()
    report_info(
        "图片索引迁移完成："
        f"写入映射{result.mappings}条，"
        f"跳过损坏旧图片条目{result.broken_links}个，"
        f"扫描HTML文件{result.html_files}个，"
        f"更新HTML文件{result.updated_html_files}个，"
        f"更新图片引用{result.updated_image_refs}处。"
    )


def image_prune_links(args: CommandArgs) -> None:
    del args
    result = prune_legacy_image_links()
    if result.removed_directory is None:
        report_info("旧图片目录不存在，无需清理。")
        return
    report_info(
        "旧图片目录清理完成："
        f"删除旧图片条目{result.removed_links}个，"
        f"删除目录：{result.removed_directory}"
    )

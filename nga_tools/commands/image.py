from __future__ import annotations

from nga_tools.backup import image_index, image_store
from nga_tools.backup.image_verify import (
    verify_all_downloaded_images,
    verify_downloaded_images,
)
from nga_tools.config import (
    load_timing_log_enabled,
    load_timing_log_retention_days,
)
from nga_tools.console import (
    report_info,
    use_thread_warning_summary,
    use_warning_log,
)
from nga_tools.commands.resolve import resolve_command_thread_target
from nga_tools.commands.types import (
    CommandArgs,
    optional_int,
    optional_str,
    required_str,
)
from nga_tools.core.output_lock import use_thread_output_lock
from nga_tools.core.paths import timing_log_path, warning_log_path
from nga_tools.timing import use_timing_log


def _image_download_failure_message(
    url: str,
    failure: dict[str, object],
) -> str:
    failure_kind = failure.get("failure_kind", "unexpected_download")
    status = failure.get("http_status")
    status_text = f"，HTTP {status}" if type(status) is int else ""
    detail = failure.get("error", "unknown")
    return (
        f"图片下载失败：{url}（类别：{failure_kind}{status_text}，"
        f"详情：{detail}）"
    )


def image_add(args: CommandArgs) -> None:
    raw_url = required_str(args, "url")
    url = image_index.normalize_nga_image_url(raw_url.strip())
    image_store.parse_nga_image_url(url)

    existing_path = image_store.mapped_image_path_for_url(url)
    if existing_path is not None:
        report_info(f"图片已存在：{url}")
        report_info(f"本地文件：{existing_path}")
        return

    with image_store.use_image_download_coordination():
        result = image_store.download_image_tasks([{"url": url}])
    if result["failed"]:
        raise RuntimeError(
            _image_download_failure_message(url, dict(result["failed"][0]))
        )

    image_path = image_store.mapped_image_path_for_url(url)
    if image_path is None:
        raise RuntimeError(f"图片下载成功但未写入有效映射：{url}")

    report_info(f"图片添加完成：{url}")
    report_info(f"本地文件：{image_path}")


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
            retention_days=load_timing_log_retention_days(),
        ),
    ):
        verify_downloaded_images(thread_tid, thread_aid)

from __future__ import annotations

from pathlib import Path
from typing import Optional

from nga_tools.core.paths import get_folder
from nga_tools.console import WarningCategory, report_progress, report_warning
from nga_tools.core.atomic import write_json_atomically
from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import NGAPageError, PageData

_AUTHOR_EMPTY_PAGE_MESSAGE = "找不到内容 或 没有更多页了"


def write_page_json(folder_json: Path, page_number: int, page_data: PageData) -> None:
    path = folder_json / f"page_{page_number}.json"
    write_json_atomically(path, page_data, indent=4)


def _is_author_empty_page_error(
    error: NGAPageError,
    aid: Optional[int],
    page_number: int,
    page_count: int,
) -> bool:
    return (
        aid is not None
        and page_number > 1
        and page_number <= page_count
        and error.message == _AUTHOR_EMPTY_PAGE_MESSAGE
    )


def _empty_author_page_data(
    first_page_data: PageData,
    page_number: int,
) -> PageData:
    page_data = first_page_data.copy()
    page_data["currentPage"] = page_number
    page_data["msg"] = "作者筛选空页"
    page_data["result"] = []
    return page_data


def fetch_backup_page(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    page_number: int,
    page_count: int,
    first_page_data: PageData,
) -> PageData:
    try:
        return client.get_page(tid, aid, page_number)
    except NGAPageError as error:
        if not _is_author_empty_page_error(error, aid, page_number, page_count):
            raise
        report_warning(
            WarningCategory.POST_CONTENT,
            f"只看作者第{page_number}页为空，继续获取后续页面。",
        )
        return _empty_author_page_data(first_page_data, page_number)


def fetch_backup_pages(
    client: NGAClient,
    tid: int,
    aid: Optional[int],
    page_count: int,
    first_page_data: PageData,
    *,
    write_json: bool = False,
) -> dict[int, PageData]:
    folder_json = Path(get_folder(tid, aid, "debug_json")) if write_json else None
    page_data_by_page: dict[int, PageData] = {}
    for page_number in range(1, page_count + 1):
        report_progress(
            f"正在获取第{page_number}页",
            completed=page_number - 1,
            total=page_count,
        )
        page_data = fetch_backup_page(
            client,
            tid,
            aid,
            page_number,
            page_count,
            first_page_data,
        )
        if folder_json is not None:
            write_page_json(folder_json, page_number, page_data)
        page_data_by_page[page_number] = page_data
    report_progress(
        "页面获取完成",
        completed=page_count,
        total=page_count,
    )
    return page_data_by_page


def page_count_from_page_data(page_data: PageData) -> int:
    total_pages = page_data.get("totalPage", 1)
    if not isinstance(total_pages, int):
        raise ValueError(f"Invalid totalPage value: {total_pages!r}")
    return total_pages


def author_total_lou_count_from_page_data(
    page_data: PageData,
    aid: Optional[int],
) -> int | None:
    """Return NGA ``vrows`` count for author-only pages.

    NGA author lous are 0-based, so this count is not the max author lou.
    """
    if aid is None:
        return None
    total_lous = page_data.get("vrows")
    if type(total_lous) is int:
        return total_lous
    return None

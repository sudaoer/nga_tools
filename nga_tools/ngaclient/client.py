from typing import Any, Optional, TypeAlias, TypedDict, cast

import requests

from nga_tools.config import get_config
from nga_tools.network_limits import api_request_slot

Tid: TypeAlias = int | str
Aid: TypeAlias = Optional[int | str]
PageData: TypeAlias = dict[str, Any]


class ForumThread(TypedDict):
    tid: int
    fid: int
    subject: str
    author: str
    authorid: int
    postdate: int
    lastpost: int
    replies: int
    forumname: str


class ForumThreadPage(TypedDict):
    fid: int
    forumname: str
    current_page: int
    total_page: int
    per_page: int
    total: int
    threads: list[ForumThread]


class NGAForumPageError(Exception):
    def __init__(self, code: object, message: str) -> None:
        super().__init__(f"Error fetching forum page: {message}")
        self.code = code
        self.message = message


def _required_int(data: dict[str, object], key: str, source: object) -> int:
    value = data.get(key)
    if type(value) is int:
        return value
    raise ValueError(f"NGA forum thread has invalid {key}: {source!r}")


def _required_str(data: dict[str, object], key: str, source: object) -> str:
    value = data.get(key)
    if isinstance(value, str):
        return value
    raise ValueError(f"NGA forum thread has invalid {key}: {source!r}")


def _forum_thread_forumname(
    thread: dict[str, object],
    *,
    default_forumname: str,
    source: object,
) -> str:
    value = thread.get("forumname")
    if isinstance(value, str):
        return value
    if value is None:
        return default_forumname
    raise ValueError(f"NGA forum thread has invalid forumname: {source!r}")


def _parse_forum_thread(raw_thread: object, *, default_forumname: str) -> ForumThread:
    if not isinstance(raw_thread, dict):
        raise ValueError(f"NGA forum thread is not an object: {raw_thread!r}")

    thread = cast(dict[str, object], raw_thread)
    source: object = thread
    return {
        "tid": _required_int(thread, "tid", source),
        "fid": _required_int(thread, "fid", source),
        "subject": _required_str(thread, "subject", source),
        "author": _required_str(thread, "author", source),
        "authorid": _required_int(thread, "authorid", source),
        "postdate": _required_int(thread, "postdate", source),
        "lastpost": _required_int(thread, "lastpost", source),
        "replies": _required_int(thread, "replies", source),
        "forumname": _forum_thread_forumname(
            thread,
            default_forumname=default_forumname,
            source=source,
        ),
    }


class NGAClient:
    def __init__(self) -> None:
        app_config = get_config()
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": app_config.user_agent,
                "Cookie": (
                    f"ngaPassportUid={app_config.nga_passport_uid}; "
                    f"ngaPassportCid={app_config.nga_passport_cid};"
                ),
            }
        )
        self.base_url = app_config.base_url
        self.page_cache: dict[str, PageData] = {}

    def page_cache_key(self, tid: Tid, aid: Aid, page: int) -> str:
        return f"{tid}_{aid if aid else 'all'}_page_{page}"

    def get_page_count(self, tid: Tid, aid: Aid) -> int:
        first_page_data = self.get_page(tid, aid, 1)
        total_pages = first_page_data.get("totalPage", 1)
        if not isinstance(total_pages, int):
            raise ValueError(f"Invalid totalPage value: {total_pages!r}")
        return total_pages

    def get_page(self, tid: Tid, aid: Aid, page: int) -> PageData:
        if not tid and not page:
            raise ValueError("Either tid or page must be provided.")
        if page < 1:
            raise ValueError("Page number must be greater than 0.")

        cache_key = self.page_cache_key(tid, aid, page)
        if cache_key in self.page_cache:
            return self.page_cache[cache_key]

        url = f"{self.base_url}/app_api.php?__lib=post&__act=list"
        data = {
            "page": str(page),
            "tid": str(tid),
        }
        if aid:
            data["authorid"] = str(aid)

        with api_request_slot():
            response = self.session.post(url, data=data, timeout=30)
        response.raise_for_status()

        json_data = response.json()
        if not isinstance(json_data, dict):
            raise ValueError("NGA response is not a JSON object.")

        page_data = cast(PageData, json_data)
        if page_data.get("code") != 0:
            raise Exception(
                f"Error fetching page: {page_data.get('msg', 'Unknown error')}"
            )

        self.page_cache[cache_key] = page_data
        return page_data

    def get_forum_thread_page(
        self,
        fid: int,
        page: int,
        *,
        order_by: str | None = None,
    ) -> ForumThreadPage:
        if page < 1:
            raise ValueError("Page number must be greater than 0.")

        url = f"{self.base_url}/app_api.php?__lib=subject&__act=list"
        data = {"fid": str(fid), "page": str(page)}
        if order_by is not None:
            data["order_by"] = order_by

        with api_request_slot():
            response = self.session.post(
                url,
                data=data,
                timeout=30,
            )
        response.raise_for_status()

        json_data = response.json()
        if not isinstance(json_data, dict):
            raise ValueError("NGA forum response is not a JSON object.")

        page_data = cast(dict[str, object], json_data)
        if page_data.get("code") != 0:
            message = page_data.get("msg")
            if not isinstance(message, str):
                message = "Unknown error"
            raise NGAForumPageError(page_data.get("code"), message)

        result = page_data.get("result")
        if not isinstance(result, dict):
            raise ValueError("NGA forum response is missing result object.")
        result_data = cast(dict[str, object], result)

        raw_threads = result_data.get("data")
        if not isinstance(raw_threads, list):
            raise ValueError("NGA forum response is missing thread list.")

        forumname = _required_str(page_data, "forumname", page_data)
        thread_items = cast(list[object], raw_threads)
        return {
            "fid": _required_int(page_data, "fid", page_data),
            "forumname": forumname,
            "current_page": _required_int(page_data, "currentPage", page_data),
            "total_page": _required_int(page_data, "totalPage", page_data),
            "per_page": _required_int(page_data, "perPage", page_data),
            "total": _required_int(page_data, "total", page_data),
            "threads": [
                _parse_forum_thread(raw_thread, default_forumname=forumname)
                for raw_thread in thread_items
            ],
        }

    def get_forum_threads(self, fid: int, page: int) -> list[ForumThread]:
        return self.get_forum_thread_page(fid, page)["threads"]

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


def _parse_forum_thread(raw_thread: object) -> ForumThread:
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
        "forumname": _required_str(thread, "forumname", source),
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

    def get_forum_threads(self, fid: int, page: int) -> list[ForumThread]:
        if page < 1:
            raise ValueError("Page number must be greater than 0.")

        url = f"{self.base_url}/app_api.php?__lib=subject&__act=list"
        with api_request_slot():
            response = self.session.post(
                url,
                data={"fid": str(fid), "page": str(page)},
                timeout=30,
            )
        response.raise_for_status()

        json_data = response.json()
        if not isinstance(json_data, dict):
            raise ValueError("NGA forum response is not a JSON object.")

        page_data = cast(dict[str, object], json_data)
        if page_data.get("code") != 0:
            raise Exception(
                f"Error fetching forum page: {page_data.get('msg', 'Unknown error')}"
            )

        result = page_data.get("result")
        if not isinstance(result, dict):
            raise ValueError("NGA forum response is missing result object.")
        result_data = cast(dict[str, object], result)

        raw_threads = result_data.get("data")
        if not isinstance(raw_threads, list):
            raise ValueError("NGA forum response is missing thread list.")

        thread_items = cast(list[object], raw_threads)
        return [_parse_forum_thread(raw_thread) for raw_thread in thread_items]

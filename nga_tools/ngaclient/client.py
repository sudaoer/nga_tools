from typing import Any, TypeAlias, cast

import requests

from nga_tools.config import get_config

Tid: TypeAlias = int | str
Aid: TypeAlias = int | str | None
PageData: TypeAlias = dict[str, Any]


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

        response = self.session.post(url, data=data)
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

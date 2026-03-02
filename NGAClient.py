import json
import config
import requests

#我们这里假定脚本运行时间很短，在一次运行中帖子不会发生变化
class NGAClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": config.UA,
                "Cookie": f"ngaPassportUid={config.NGAPASSPORTUID}; ngaPassportCid={config.NGAPASSPORTCID};",
            }
        )
        self.base_url = config.BASE_URL
        self.page_cache = {}

    def page_cache_key(self, tid, aid, page):
        return f"{tid}_{aid if aid else 'all'}_page_{page}"

    def get_page_count(self, tid, aid):
        first_page_data = self.get_page(tid, aid, 1)
        total_pages = first_page_data.get("totalPage", 1)
        return total_pages

    def get_page(self, tid, aid, page):
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

        if json_data.get("code")!=0:
            raise Exception(f"Error fetching page: {json_data.get('msg', 'Unknown error')}")

        self.page_cache[cache_key] = json_data
        return json_data

    
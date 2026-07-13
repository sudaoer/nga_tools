from collections.abc import Callable, Generator, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from threading import Lock
from typing import Any, Optional, TypeAlias, TypedDict, cast
from urllib.parse import parse_qs, urlparse

import requests

from nga_tools.config import get_config
from nga_tools.network_limits import api_request_slot, get_api_concurrency
from nga_tools.ngaclient.session import (
    ThreadLocalAPISessionPool,
    create_api_session,
    current_api_session,
)
from nga_tools.ngaclient.api_runtime import current_api_runtime

Tid: TypeAlias = int | str
Aid: TypeAlias = Optional[int | str]
PageData: TypeAlias = dict[str, Any]
PageProgressCallback: TypeAlias = Callable[[int, int, int], None]


@dataclass(frozen=True, slots=True)
class PidRedirectTarget:
    tid: int
    page_number: int


def _single_positive_query_int(query: dict[str, list[str]], key: str) -> int | None:
    values = query.get(key)
    if values is None or len(values) != 1:
        return None
    try:
        value = int(values[0])
    except ValueError:
        return None
    return value if value > 0 else None


def parse_pid_redirect_location(location: str) -> PidRedirectTarget | None:
    parsed = urlparse(location)
    if not parsed.path.endswith("/read.php"):
        return None
    query = parse_qs(parsed.query, keep_blank_values=True)
    tid = _single_positive_query_int(query, "tid")
    page_number = _single_positive_query_int(query, "page")
    if tid is None or page_number is None:
        return None
    return PidRedirectTarget(tid=tid, page_number=page_number)


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


class NGAPageError(Exception):
    def __init__(self, code: object, message: str) -> None:
        super().__init__(f"Error fetching page: {message}")
        self.code = code
        self.message = message


HIDDEN_THREAD_MESSAGE = "帖子被设为隐藏"


def is_hidden_thread_error(error: Exception) -> bool:
    return isinstance(error, NGAPageError) and error.message == HIDDEN_THREAD_MESSAGE


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
    def __init__(self, session: requests.Session | None = None) -> None:
        app_config = get_config()
        selected_session = session if session is not None else current_api_session()
        self.session = (
            selected_session if selected_session is not None else create_api_session()
        )
        self.base_url = app_config.base_url
        self.page_cache: dict[str, PageData] = {}
        self._stream_prefetch_cache: dict[str, PageData] = {}
        self._parallel_page_fetch_enabled = session is None

    def page_cache_key(self, tid: Tid, aid: Aid, page: int) -> str:
        return f"{tid}_{aid if aid else 'all'}_page_{page}"

    def clear_page_cache(self) -> int:
        cleared_count = len(self.page_cache) + len(self._stream_prefetch_cache)
        self.page_cache.clear()
        self._stream_prefetch_cache.clear()
        return cleared_count

    def get_page_count(self, tid: Tid, aid: Aid) -> int:
        first_page_data = self.get_page(tid, aid, 1)
        total_pages = first_page_data.get("totalPage", 1)
        if not isinstance(total_pages, int):
            raise ValueError(f"Invalid totalPage value: {total_pages!r}")
        return total_pages

    def get_pid_redirect_target(self, pid: int) -> PidRedirectTarget | None:
        return self._request_pid_redirect_target_with_session(self.session, pid)

    def _request_pid_redirect_target_with_session(
        self,
        session: requests.Session,
        pid: int,
    ) -> PidRedirectTarget | None:
        if pid <= 0:
            raise ValueError("PID must be greater than 0.")
        url = f"{self.base_url.rstrip('/')}/read.php"
        with api_request_slot():
            response = session.get(
                url,
                params={"pid": str(pid), "opt": "128"},
                allow_redirects=False,
                timeout=30,
            )
        response.raise_for_status()
        if response.status_code < 300 or response.status_code >= 400:
            return None
        location = response.headers.get("Location")
        if location is None:
            return None
        return parse_pid_redirect_location(location)

    def get_pid_redirect_targets(
        self,
        pids: Sequence[int],
    ) -> dict[int, PidRedirectTarget | None]:
        ordered_pids = list(dict.fromkeys(pids))
        for pid in ordered_pids:
            if pid <= 0:
                raise ValueError("PID must be greater than 0.")
        if not ordered_pids:
            return {}

        runtime = current_api_runtime()
        if runtime is None or not self._parallel_page_fetch_enabled:
            return {
                pid: self.get_pid_redirect_target(pid)
                for pid in ordered_pids
            }

        def fetch_runtime_target(
            session: requests.Session,
            pid: int,
        ) -> PidRedirectTarget | None:
            return self._request_pid_redirect_target_with_session(session, pid)

        targets: dict[int, PidRedirectTarget | None] = {}
        for pid, target in runtime.map_unordered(
            ordered_pids,
            fetch_runtime_target,
        ):
            targets[pid] = target
        return targets

    def _request_page(self, tid: Tid, aid: Aid, page: int) -> PageData:
        return self._request_page_with_session(self.session, tid, aid, page)

    def _request_page_with_session(
        self,
        session: requests.Session,
        tid: Tid,
        aid: Aid,
        page: int,
    ) -> PageData:
        if not tid and not page:
            raise ValueError("Either tid or page must be provided.")
        if page < 1:
            raise ValueError("Page number must be greater than 0.")

        url = f"{self.base_url}/app_api.php?__lib=post&__act=list"
        data = {
            "page": str(page),
            "tid": str(tid),
        }
        if aid:
            data["authorid"] = str(aid)

        with api_request_slot():
            response = session.post(url, data=data, timeout=30)
        response.raise_for_status()

        json_data = response.json()
        if not isinstance(json_data, dict):
            raise ValueError("NGA response is not a JSON object.")

        page_data = cast(PageData, json_data)
        if page_data.get("code") != 0:
            message = page_data.get("msg")
            if not isinstance(message, str):
                message = "Unknown error"
            raise NGAPageError(page_data.get("code"), message)

        return page_data

    def get_page(self, tid: Tid, aid: Aid, page: int) -> PageData:
        if not tid and not page:
            raise ValueError("Either tid or page must be provided.")
        if page < 1:
            raise ValueError("Page number must be greater than 0.")

        cache_key = self.page_cache_key(tid, aid, page)
        if cache_key in self.page_cache:
            return self.page_cache[cache_key]

        page_data = self._request_page(tid, aid, page)
        self.page_cache[cache_key] = page_data
        return page_data

    def get_pages(
        self,
        tid: Tid,
        aid: Aid,
        pages: Sequence[int],
        *,
        on_page_complete: PageProgressCallback | None = None,
    ) -> dict[int, PageData]:
        ordered_pages = list(dict.fromkeys(pages))
        for page in ordered_pages:
            if page < 1:
                raise ValueError("Page number must be greater than 0.")

        total = len(ordered_pages)
        if total == 0:
            return {}

        cached_pages: dict[int, PageData] = {}
        missing_pages: list[int] = []
        for page in ordered_pages:
            cached = self.page_cache.get(self.page_cache_key(tid, aid, page))
            if cached is None:
                missing_pages.append(page)
            else:
                cached_pages[page] = cached

        completed = 0
        if on_page_complete is not None:
            for page in ordered_pages:
                if page not in cached_pages:
                    continue
                completed += 1
                on_page_complete(page, completed, total)

        fetched_pages: dict[int, PageData] = {}
        worker_count = min(get_api_concurrency(), len(missing_pages))
        if worker_count <= 1 or not self._parallel_page_fetch_enabled:
            for page in missing_pages:
                fetched_pages[page] = self._request_page(tid, aid, page)
                completed += 1
                if on_page_complete is not None:
                    on_page_complete(page, completed, total)
        elif (runtime := current_api_runtime()) is not None:
            def fetch_runtime_page(
                session: requests.Session,
                page: int,
            ) -> PageData:
                return self._request_page_with_session(session, tid, aid, page)

            for page, page_data in runtime.map_unordered(
                missing_pages,
                fetch_runtime_page,
            ):
                fetched_pages[page] = page_data
                completed += 1
                if on_page_complete is not None:
                    on_page_complete(page, completed, total)
        else:
            session_pool = ThreadLocalAPISessionPool()

            def fetch_page(page: int) -> PageData:
                worker_client = NGAClient(session=session_pool.session())
                worker_client.base_url = self.base_url
                return worker_client._request_page(tid, aid, page)

            with session_pool:
                with ThreadPoolExecutor(max_workers=worker_count) as executor:
                    future_pages: dict[Future[PageData], int] = {
                        executor.submit(fetch_page, page): page
                        for page in missing_pages
                    }
                    try:
                        for future in as_completed(future_pages):
                            page = future_pages[future]
                            fetched_pages[page] = future.result()
                            completed += 1
                            if on_page_complete is not None:
                                on_page_complete(page, completed, total)
                    except BaseException:
                        for future in future_pages:
                            future.cancel()
                        raise

        for page, page_data in fetched_pages.items():
            self.page_cache[self.page_cache_key(tid, aid, page)] = page_data

        return {
            page: (
                cached_pages[page]
                if page in cached_pages
                else fetched_pages[page]
            )
            for page in ordered_pages
        }

    def iter_pages(
        self,
        tid: Tid,
        aid: Aid,
        pages: Sequence[int],
        *,
        on_page_complete: PageProgressCallback | None = None,
    ) -> Generator[tuple[int, PageData]]:
        """Yield pages in order and retain only unconsumed in-flight results."""
        ordered_pages = list(dict.fromkeys(pages))
        for page in ordered_pages:
            if page < 1:
                raise ValueError("Page number must be greater than 0.")
        total = len(ordered_pages)
        if total == 0:
            return

        completed = 0
        runtime = current_api_runtime()
        if runtime is None or not self._parallel_page_fetch_enabled:
            page_data_by_page = self.get_pages(tid, aid, ordered_pages)
            for page in ordered_pages:
                completed += 1
                if on_page_complete is not None:
                    on_page_complete(page, completed, total)
                yield page, page_data_by_page[page]
            return

        cached_pages: dict[int, PageData] = {}
        missing_pages: list[int] = []
        for page in ordered_pages:
            cache_key = self.page_cache_key(tid, aid, page)
            cached = self.page_cache.get(cache_key)
            if cached is None:
                cached = self._stream_prefetch_cache.get(cache_key)
            if cached is None:
                missing_pages.append(page)
            else:
                cached_pages[page] = cached

        completed_stream_pages: dict[int, PageData] = {}
        completed_stream_pages_lock = Lock()

        def fetch_runtime_page(
            session: requests.Session,
            page: int,
        ) -> PageData:
            page_data = self._request_page_with_session(session, tid, aid, page)
            with completed_stream_pages_lock:
                completed_stream_pages[page] = page_data
            return page_data

        fetched_iterator = runtime.map_ordered(missing_pages, fetch_runtime_page)
        try:
            for page in ordered_pages:
                if page in cached_pages:
                    page_data = cached_pages[page]
                    self._stream_prefetch_cache.pop(
                        self.page_cache_key(tid, aid, page),
                        None,
                    )
                else:
                    try:
                        fetched_page, page_data = next(fetched_iterator)
                    except StopIteration as error:
                        raise RuntimeError("NGA API流式页面提前结束。") from error
                    if fetched_page != page:
                        raise RuntimeError("NGA API流式页面顺序不一致。")
                    with completed_stream_pages_lock:
                        completed_stream_pages.pop(fetched_page, None)
                completed += 1
                if on_page_complete is not None:
                    on_page_complete(page, completed, total)
                yield page, page_data
        finally:
            fetched_iterator.close()
            with completed_stream_pages_lock:
                prefetched_pages = dict(completed_stream_pages)
            for page, page_data in prefetched_pages.items():
                self._stream_prefetch_cache[
                    self.page_cache_key(tid, aid, page)
                ] = page_data

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

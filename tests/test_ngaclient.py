from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest
import requests
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import NGAPageError
from nga_tools.network_limits import configure_network_limits
from nga_tools.ngaclient.session import (
    ThreadLocalAPISessionPool,
    use_api_session,
)


class _PageErrorResponse:
    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {
            "code": 35,
            "msg": "找不到内容 或 没有更多页了",
        }


class _SuccessfulPageResponse:
    def __init__(self, page: int) -> None:
        self.page = page

    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {
            "code": 0,
            "currentPage": self.page,
            "totalPage": 3,
            "result": [],
        }


class _ConcurrentRequestTracker:
    def __init__(self, expected_overlap: int) -> None:
        self.expected_overlap = expected_overlap
        self.release = Event()
        self.lock = Lock()
        self.active = 0
        self.max_active = 0
        self.pages: list[int] = []

    def enter(self, page: int) -> None:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.pages.append(page)
            if self.active >= self.expected_overlap:
                self.release.set()
        if not self.release.wait(timeout=2):
            raise RuntimeError("concurrent requests did not overlap")

    def exit(self) -> None:
        with self.lock:
            self.active -= 1


class _ConcurrentPageSession:
    def __init__(self, tracker: _ConcurrentRequestTracker) -> None:
        self.tracker = tracker
        self.closed = False

    def post(
        self,
        url: str,
        *,
        data: dict[str, str],
        timeout: int,
    ) -> _SuccessfulPageResponse:
        del url, timeout
        page = int(data["page"])
        self.tracker.enter(page)
        try:
            return _SuccessfulPageResponse(page)
        finally:
            self.tracker.exit()

    def close(self) -> None:
        self.closed = True


class _ForumThreadPageResponse:
    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {
            "code": 0,
            "fid": 784,
            "forumname": "二次元跑团综合",
            "currentPage": 544,
            "totalPage": 887,
            "perPage": 35,
            "total": 31045,
            "result": {
                "data": [
                    {
                        "tid": 35411723,
                        "fid": 784,
                        "author": "克爹海吗",
                        "authorid": 64473922,
                        "subject": "[交流]怎么才能把吞的楼找回来？",
                        "postdate": 1676624358,
                        "lastpost": 1676624459,
                        "replies": 1,
                        "forumname": None,
                    }
                ]
            },
        }


class NGAClientForumThreadPageTest:
    def test_forum_thread_uses_page_forumname_when_thread_forumname_is_null(
        self,
    ) -> None:
        config = SimpleNamespace(
            base_url="https://bbs.nga.cn",
            user_agent="test-agent",
            nga_passport_uid="uid",
            nga_passport_cid="cid",
        )

        with patch("nga_tools.ngaclient.client.get_config", return_value=config):
            client = NGAClient()
            client.session.post = MagicMock(return_value=_ForumThreadPageResponse())

            page = client.get_forum_thread_page(
                784,
                544,
                order_by="postdatedesc",
            )

        assert page['forumname'] == '二次元跑团综合'
        assert page['threads'][0]['forumname'] == '二次元跑团综合'
        assert page['threads'][0]['tid'] == 35411723


class NGAClientPageErrorTest:
    def test_page_error_exposes_code_and_message(self) -> None:
        config = SimpleNamespace(
            base_url="https://bbs.nga.cn",
            user_agent="test-agent",
            nga_passport_uid="uid",
            nga_passport_cid="cid",
        )

        with patch("nga_tools.ngaclient.client.get_config", return_value=config):
            client = NGAClient()
            client.session.post = MagicMock(return_value=_PageErrorResponse())

            with pytest.raises(NGAPageError) as context:
                client.get_page(123, 456, 6)

        assert context.value.code == 35
        assert context.value.message == '找不到内容 或 没有更多页了'
        assert str(context.value) == 'Error fetching page: 找不到内容 或 没有更多页了'


class NGAClientPageBatchTest:
    def test_fetches_pages_concurrently_with_thread_local_sessions(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=1)
        tracker = _ConcurrentRequestTracker(expected_overlap=2)
        worker_sessions: list[_ConcurrentPageSession] = []

        def create_worker_session() -> _ConcurrentPageSession:
            session = _ConcurrentPageSession(tracker)
            worker_sessions.append(session)
            return session

        completed_pages: list[int] = []
        parent_session = MagicMock(spec=requests.Session)
        with (
            patch(
                "nga_tools.ngaclient.client.create_api_session",
                return_value=parent_session,
            ),
            patch(
                "nga_tools.ngaclient.session.create_api_session",
                side_effect=create_worker_session,
            ),
        ):
            client = NGAClient()
            pages = client.get_pages(
                123,
                None,
                [1, 2, 3],
                on_page_complete=lambda page, _completed, _total: (
                    completed_pages.append(page)
                ),
            )

        assert list(pages) == [1, 2, 3]
        assert {page: data["currentPage"] for page, data in pages.items()} == {
            1: 1,
            2: 2,
            3: 3,
        }
        assert tracker.max_active == 2
        assert sorted(tracker.pages) == [1, 2, 3]
        assert sorted(completed_pages) == [1, 2, 3]
        assert len(worker_sessions) == 2
        assert all(session.closed for session in worker_sessions)
        parent_session.post.assert_not_called()

    def test_batch_deduplicates_pages_and_reuses_parent_cache(self) -> None:
        configure_network_limits(api_concurrency=1, image_concurrency=1)
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [
            _SuccessfulPageResponse(3),
            _SuccessfulPageResponse(1),
        ]
        client = NGAClient(session=session)

        first = client.get_pages(123, None, [3, 1, 3])
        cached = client.get_pages(123, None, [1, 3])

        assert list(first) == [3, 1]
        assert list(cached) == [1, 3]
        assert session.post.call_count == 2

    def test_failed_batch_does_not_commit_partial_parent_cache(self) -> None:
        configure_network_limits(api_concurrency=1, image_concurrency=1)
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [
            _SuccessfulPageResponse(1),
            RuntimeError("page two failed"),
        ]
        client = NGAClient(session=session)

        with pytest.raises(RuntimeError, match="page two failed"):
            client.get_pages(123, None, [1, 2])

        assert client.page_cache == {}

    def test_explicit_session_keeps_batch_on_calling_thread(self) -> None:
        configure_network_limits(api_concurrency=4, image_concurrency=1)
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [
            _SuccessfulPageResponse(1),
            _SuccessfulPageResponse(2),
        ]
        client = NGAClient(session=session)

        with patch("nga_tools.ngaclient.client.ThreadPoolExecutor") as executor:
            pages = client.get_pages(123, None, [1, 2])

        assert list(pages) == [1, 2]
        assert session.post.call_count == 2
        executor.assert_not_called()


class NGAClientSessionTest:
    def test_uses_bound_session_without_sharing_it_outside_context(self) -> None:
        config = SimpleNamespace(
            base_url="https://bbs.nga.cn",
            user_agent="test-agent",
            nga_passport_uid="uid",
            nga_passport_cid="cid",
        )
        bound_session = MagicMock(spec=requests.Session)
        fallback_session = MagicMock(spec=requests.Session)

        with (
            patch("nga_tools.ngaclient.client.get_config", return_value=config),
            patch(
                "nga_tools.ngaclient.client.create_api_session",
                return_value=fallback_session,
            ),
        ):
            with use_api_session(bound_session):
                assert NGAClient().session is bound_session
            assert NGAClient().session is fallback_session

    def test_thread_local_pool_reuses_per_thread_and_closes_every_session(
        self,
    ) -> None:
        barrier = Barrier(2)
        created_sessions = [
            MagicMock(spec=requests.Session),
            MagicMock(spec=requests.Session),
        ]

        def use_session_twice(
            pool: ThreadLocalAPISessionPool,
        ) -> tuple[requests.Session, requests.Session]:
            first = pool.session()
            barrier.wait()
            return first, pool.session()

        with patch(
            "nga_tools.ngaclient.session.create_api_session",
            side_effect=created_sessions,
        ):
            with ThreadLocalAPISessionPool() as pool:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(use_session_twice, pool) for _ in range(2)]
                    results = [future.result() for future in futures]

        assert results[0][0] is results[0][1]
        assert results[1][0] is results[1][1]
        assert results[0][0] is not results[1][0]
        for session in created_sessions:
            session.close.assert_called_once_with()

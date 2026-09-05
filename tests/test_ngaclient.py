from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

import pytest
import requests
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nga_tools.ngaclient import (
    NGAClient,
    PidRedirectTarget,
    is_thread_status_abnormal_error,
    parse_pid_redirect_location,
)
from nga_tools.ngaclient.client import NGAPageError
from nga_tools.ngaclient.api_runtime import FairAPIRuntime, use_api_runtime
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


class _PidRedirectResponse:
    def __init__(self, status_code: int, location: str | None = None) -> None:
        self.status_code = status_code
        self.headers = {} if location is None else {"Location": location}

    def raise_for_status(self) -> None:
        return


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


class _EarlyStopPageSession:
    def __init__(
        self,
        started_pages: list[int],
        started_lock: Lock,
        blocked_request_started: Event,
        release: Event,
    ) -> None:
        self.started_pages = started_pages
        self.started_lock = started_lock
        self.blocked_request_started = blocked_request_started
        self.release = release
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
        with self.started_lock:
            self.started_pages.append(page)
        if page != 1:
            self.blocked_request_started.set()
            if not self.release.wait(timeout=2):
                raise RuntimeError("early-stop request was not released")
        return _SuccessfulPageResponse(page)

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
    @pytest.mark.parametrize(
        "message",
        ["帖子被设为隐藏", "帖子被删除", "帖子正等待审核", "此帖子被锁定"],
    )
    def test_abnormal_thread_status_error_matches_ignored_messages(
        self,
        message: str,
    ) -> None:
        assert is_thread_status_abnormal_error(NGAPageError(None, message))

    def test_abnormal_thread_status_error_rejects_other_errors(self) -> None:
        assert not is_thread_status_abnormal_error(
            NGAPageError(None, "找不到内容 或 没有更多页了")
        )
        assert not is_thread_status_abnormal_error(RuntimeError("boom"))

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


class NGAClientPidRedirectTest:
    @pytest.mark.parametrize(
        ("location", "expected"),
        [
            (
                "/read.php?tid=43877379&page=1249#pid874888389Anchor",
                PidRedirectTarget(tid=43877379, page_number=1249),
            ),
            (
                "https://bbs.nga.cn/read.php?tid=43877379&page=1249"
                "#pid874888389Anchor",
                PidRedirectTarget(tid=43877379, page_number=1249),
            ),
            ("/read.php?tid=43877379&page=0", None),
            ("/read.php?tid=43877379", None),
            ("/thread.php?tid=43877379&page=1249", None),
        ],
    )
    def test_parses_relative_and_absolute_locations(
        self,
        location: str,
        expected: PidRedirectTarget | None,
    ) -> None:
        assert parse_pid_redirect_location(location) == expected

    def test_uses_configured_base_url_session_and_disables_following(self) -> None:
        config = SimpleNamespace(
            base_url="https://ngabbs.com/",
            user_agent="test-agent",
            nga_passport_uid="uid",
            nga_passport_cid="cid",
        )
        session = MagicMock(spec=requests.Session)
        session.get.return_value = _PidRedirectResponse(
            302,
            "/read.php?tid=43877379&page=1249#pid874888389Anchor",
        )

        with patch("nga_tools.ngaclient.client.get_config", return_value=config):
            target = NGAClient(session=session).get_pid_redirect_target(874888389)

        assert target == PidRedirectTarget(tid=43877379, page_number=1249)
        session.get.assert_called_once_with(
            "https://ngabbs.com/read.php",
            params={"pid": "874888389", "opt": "128"},
            allow_redirects=False,
            timeout=30,
        )

    def test_non_redirect_or_invalid_redirect_returns_none(self) -> None:
        session = MagicMock(spec=requests.Session)
        session.get.side_effect = [
            _PidRedirectResponse(200),
            _PidRedirectResponse(302, "/read.php?tid=123&page=invalid"),
        ]
        client = NGAClient(session=session)

        assert client.get_pid_redirect_target(100) is None
        assert client.get_pid_redirect_target(101) is None

    def test_rejects_non_positive_pid_without_request(self) -> None:
        session = MagicMock(spec=requests.Session)
        client = NGAClient(session=session)

        with pytest.raises(ValueError, match="PID"):
            client.get_pid_redirect_target(0)
        session.get.assert_not_called()

    def test_batch_uses_shared_api_runtime_concurrently(self) -> None:
        tracker = _ConcurrentRequestTracker(expected_overlap=2)
        client = NGAClient()

        def request_target(
            _session: requests.Session,
            pid: int,
        ) -> PidRedirectTarget:
            tracker.enter(pid)
            tracker.exit()
            return PidRedirectTarget(tid=123, page_number=pid - 100)

        with (
            patch.object(
                client,
                "_request_pid_redirect_target_with_session",
                side_effect=request_target,
            ),
            use_api_runtime(2),
        ):
            targets = client.get_pid_redirect_targets([101, 102])

        assert tracker.max_active == 2
        assert targets == {
            101: PidRedirectTarget(tid=123, page_number=1),
            102: PidRedirectTarget(tid=123, page_number=2),
        }


class NGAClientPageBatchTest:
    def test_fetches_independent_requests_concurrently_and_preserves_order(
        self,
    ) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=1)
        tracker = _ConcurrentRequestTracker(expected_overlap=2)
        worker_sessions: list[_ConcurrentPageSession] = []

        def create_worker_session() -> _ConcurrentPageSession:
            session = _ConcurrentPageSession(tracker)
            worker_sessions.append(session)
            return session

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
            pages = client.get_page_batch(
                [
                    (101, 456, 3),
                    (102, 789, 1),
                    (103, None, 2),
                ]
            )

        assert [page["currentPage"] for page in pages] == [3, 1, 2]
        assert tracker.max_active == 2
        assert sorted(tracker.pages) == [1, 2, 3]
        assert set(client.page_cache) == {
            client.page_cache_key(101, 456, 3),
            client.page_cache_key(102, 789, 1),
            client.page_cache_key(103, None, 2),
        }
        assert len(worker_sessions) == 2
        assert all(session.closed for session in worker_sessions)
        parent_session.post.assert_not_called()

    def test_independent_batch_deduplicates_requests_and_reuses_cache(
        self,
    ) -> None:
        configure_network_limits(api_concurrency=1, image_concurrency=1)
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [
            _SuccessfulPageResponse(3),
            _SuccessfulPageResponse(1),
        ]
        client = NGAClient(session=session)

        first = client.get_page_batch(
            [
                (101, 456, 3),
                (102, 789, 1),
                (101, 456, 3),
            ]
        )
        cached = client.get_page_batch([(102, 789, 1), (101, 456, 3)])

        assert [page["currentPage"] for page in first] == [3, 1, 3]
        assert [page["currentPage"] for page in cached] == [1, 3]
        assert session.post.call_count == 2

    def test_failed_independent_batch_does_not_commit_partial_cache(
        self,
    ) -> None:
        configure_network_limits(api_concurrency=1, image_concurrency=1)
        session = MagicMock(spec=requests.Session)
        session.post.side_effect = [
            _SuccessfulPageResponse(1),
            RuntimeError("second target failed"),
        ]
        client = NGAClient(session=session)

        with pytest.raises(RuntimeError, match="second target failed"):
            client.get_page_batch([(101, 456, 1), (102, 789, 1)])

        assert client.page_cache == {}

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
        assert client.clear_page_cache() == 2
        assert client.page_cache == {}
        assert client.clear_page_cache() == 0

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

    def test_command_runtime_schedules_batches_fairly(self) -> None:
        configure_network_limits(api_concurrency=4, image_concurrency=1)
        initial_large_started = Event()
        release_large = Event()
        starts: list[tuple[str, int]] = []
        start_lock = Lock()

        def fetch(
            _session: requests.Session,
            item: tuple[str, int],
        ) -> tuple[str, int]:
            with start_lock:
                starts.append(item)
                if len(starts) >= 4:
                    initial_large_started.set()
            if item[0] == "large" and item[1] <= 4:
                assert release_large.wait(timeout=2)
            return item

        sessions = [MagicMock(spec=requests.Session) for _ in range(4)]
        with patch(
            "nga_tools.ngaclient.api_runtime.create_api_session",
            side_effect=sessions,
        ):
            runtime = FairAPIRuntime(4)
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    large_future = executor.submit(
                        lambda: list(
                            runtime.map_unordered(
                                [("large", index) for index in range(1, 21)],
                                fetch,
                            )
                        )
                    )
                    assert initial_large_started.wait(timeout=2)
                    small_future = executor.submit(
                        lambda: list(
                            runtime.map_unordered([("small", 1)], fetch)
                        )
                    )
                    release_large.set()
                    assert len(large_future.result(timeout=3)) == 20
                    assert small_future.result(timeout=3) == [
                        (("small", 1), ("small", 1))
                    ]
            finally:
                runtime.close()

        small_index = starts.index(("small", 1))
        large_after_initial = [
            item for item in starts[4:small_index] if item[0] == "large"
        ]
        assert len(large_after_initial) < 4
        for session in sessions:
            session.close.assert_called_once_with()

    def test_runtime_callbacks_run_on_the_calling_thread(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=1)
        caller_thread = threading.get_ident()
        callback_threads: list[int] = []
        sessions = [
            MagicMock(spec=requests.Session),
            MagicMock(spec=requests.Session),
        ]
        for session in sessions:
            session.post.side_effect = lambda _url, *, data, timeout: (
                _SuccessfulPageResponse(int(data["page"]))
            )

        with (
            patch(
                "nga_tools.ngaclient.api_runtime.create_api_session",
                side_effect=sessions,
            ),
            use_api_runtime(2),
        ):
            client = NGAClient()
            pages = client.get_pages(
                123,
                None,
                [1, 2, 3],
                on_page_complete=lambda _page, _completed, _total: (
                    callback_threads.append(threading.get_ident())
                ),
            )

        assert list(pages) == [1, 2, 3]
        assert callback_threads == [caller_thread, caller_thread, caller_thread]

    def test_failed_runtime_batch_does_not_cancel_other_batch(self) -> None:
        sessions = [
            MagicMock(spec=requests.Session),
            MagicMock(spec=requests.Session),
        ]

        def fetch(_session: requests.Session, item: int) -> int:
            if item == 2:
                raise RuntimeError("page two failed")
            return item

        with patch(
            "nga_tools.ngaclient.api_runtime.create_api_session",
            side_effect=sessions,
        ):
            runtime = FairAPIRuntime(2)
            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    failed_future = executor.submit(
                        lambda: list(runtime.map_unordered([1, 2, 3], fetch))
                    )
                    healthy_future = executor.submit(
                        lambda: list(runtime.map_unordered([10, 11], fetch))
                    )
                    with pytest.raises(RuntimeError, match="page two failed"):
                        failed_future.result(timeout=3)
                    assert sorted(healthy_future.result(timeout=3)) == [
                        (10, 10),
                        (11, 11),
                    ]
            finally:
                runtime.close()

    def test_iter_pages_streams_without_populating_parent_cache(self) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=1)
        sessions = [
            MagicMock(spec=requests.Session),
            MagicMock(spec=requests.Session),
        ]
        for session in sessions:
            session.post.side_effect = lambda _url, *, data, timeout: (
                _SuccessfulPageResponse(int(data["page"]))
            )

        client = NGAClient()
        with (
            patch(
                "nga_tools.ngaclient.api_runtime.create_api_session",
                side_effect=sessions,
            ),
            use_api_runtime(2),
        ):
            pages = list(client.iter_pages(123, None, [3, 1, 2]))

        assert [page for page, _data in pages] == [3, 1, 2]
        assert client.page_cache == {}

    def test_iter_pages_close_retains_completed_inflight_pages_for_next_stream(
        self,
    ) -> None:
        configure_network_limits(api_concurrency=2, image_concurrency=1)
        started_pages: list[int] = []
        started_lock = Lock()
        blocked_request_started = Event()
        release = Event()
        sessions = [
            _EarlyStopPageSession(
                started_pages,
                started_lock,
                blocked_request_started,
                release,
            )
            for _ in range(2)
        ]

        client = NGAClient()
        with (
            patch(
                "nga_tools.ngaclient.api_runtime.create_api_session",
                side_effect=sessions,
            ),
            use_api_runtime(2),
        ):
            pages = client.iter_pages(123, None, list(range(1, 11)))
            first_page, _first_data = next(pages)
            assert first_page == 1
            assert blocked_request_started.wait(timeout=1)
            close_completed = Event()
            close_thread = threading.Thread(
                target=lambda: (pages.close(), close_completed.set()),
            )
            close_thread.start()
            assert not close_completed.wait(timeout=0.05)
            with started_lock:
                started_before_release = list(started_pages)
            release.set()
            close_thread.join(timeout=1)
            assert close_completed.is_set()
            prefetched_pages = sorted(set(started_before_release) - {1})
            replayed_pages = list(client.iter_pages(123, None, prefetched_pages))

        assert set(started_before_release).issubset({1, 2, 3})
        assert len(started_before_release) < 10
        assert [page for page, _data in replayed_pages] == prefetched_pages
        assert started_pages == started_before_release
        assert all(session.closed for session in sessions)
        assert client.page_cache == {}

    def test_iter_pages_close_preserves_unconsumed_prefetch_for_later_stream(
        self,
    ) -> None:
        sessions = [
            MagicMock(spec=requests.Session),
            MagicMock(spec=requests.Session),
        ]
        client = NGAClient()
        client._stream_prefetch_cache.update(
            {
                client.page_cache_key(123, None, 2): {
                    "code": 0,
                    "currentPage": 2,
                    "result": [],
                },
                client.page_cache_key(123, None, 3): {
                    "code": 0,
                    "currentPage": 3,
                    "result": [],
                },
            }
        )

        with (
            patch(
                "nga_tools.ngaclient.api_runtime.create_api_session",
                side_effect=sessions,
            ),
            use_api_runtime(2),
        ):
            first_stream = client.iter_pages(123, None, [2, 3])
            first_page, _first_data = next(first_stream)
            assert first_page == 2
            first_stream.close()
            later_pages = list(client.iter_pages(123, None, [3]))

        assert [page for page, _data in later_pages] == [3]
        for session in sessions:
            session.post.assert_not_called()


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

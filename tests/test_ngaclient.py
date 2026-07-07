from __future__ import annotations

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from nga_tools.ngaclient import NGAClient
from nga_tools.ngaclient.client import NGAPageError


class _PageErrorResponse:
    def raise_for_status(self) -> None:
        return

    def json(self) -> dict[str, object]:
        return {
            "code": 35,
            "msg": "找不到内容 或 没有更多页了",
        }


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


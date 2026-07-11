from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    StoredFloorMap,
)
from nga_tools.cli.parser import args_parse
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web import data as web_data
from nga_tools.web import database as web_database
from nga_tools.backup.post_overlay import POST_OVERLAYS_FILENAME
from nga_tools.backup.post_version_selection import POST_VERSION_SELECTIONS_FILENAME
from nga_tools.web.data import (
    ThreadConfig,
    read_posts,
    safe_output_file,
    scan_thread_summaries,
)
from nga_tools.web import server as web_server
from nga_tools.web.server import create_app


def _write_archive(
    thread_dir: Path,
    posts: list[dict[str, object]],
) -> None:
    thread_dir.mkdir(parents=True, exist_ok=True)
    ThreadArchiveStore(thread_dir).upsert_page(
        1,
        {"totalPage": 1, "result": posts},
        observed_at="2026-07-08T00:00:00+00:00",
    )


def _post(
    lou: int,
    content: str,
    *,
    pid: Optional[int] = None,
) -> dict[str, object]:
    return {
        "pid": lou if pid is None else pid,
        "lou": lou,
        "content": content,
        "postdate": 1783490000 + lou,
        "author": {
            "uid": 200 + lou,
            "username": f"author-{lou}",
        },
    }


def _write_image_mapping(output_dir: Path, url: str, unique_rel_path: str) -> None:
    connection = sqlite3.connect(output_dir / "image_index.sqlite3")
    try:
        connection.execute(
            """
            CREATE TABLE image_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO image_mappings (
                url,
                unique_rel_path,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (url, unique_rel_path, "2026-07-08T00:00:00+00:00", "2026-07-08T00:00:00+00:00"),
        )
        connection.commit()
    finally:
        connection.close()


def _write_forum_thread_db(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_dir / "forum_threads.sqlite3")
    try:
        connection.execute(
            """
            CREATE TABLE forum_threads_fid_784 (
                tid INTEGER PRIMARY KEY,
                aid INTEGER NOT NULL,
                author TEXT NOT NULL,
                subject TEXT NOT NULL,
                postdate INTEGER NOT NULL,
                postdate_text TEXT NOT NULL,
                lastpost INTEGER NOT NULL,
                lastpost_text TEXT NOT NULL,
                replies INTEGER NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO forum_threads_fid_784 (
                tid,
                aid,
                author,
                subject,
                postdate,
                postdate_text,
                lastpost,
                lastpost_text,
                replies
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                101,
                201,
                "Alice",
                "Sample Thread",
                1783400000,
                "2026-07-07 00:00:00",
                1783490000,
                "2026-07-08 00:00:00",
                12,
            ),
        )
        connection.commit()
    finally:
        connection.close()


class WebViewerDataTest:
    def test_scans_ready_and_legacy_backup_summaries(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        ready_dir = output_dir / "101_201"
        legacy_dir = output_dir / "102_all"
        _write_archive(ready_dir, [_post(1, "hello"), _post(2, "world")])
        (legacy_dir / "json").mkdir(parents=True)
        (legacy_dir / "json" / "page_1.json").write_text("{}", encoding="utf-8")
        metadata: ThreadConfig = {
            "thread_name": "sample",
            "tid": 101,
            "aid": 201,
            "subject": "Sample Thread",
            "author": "Alice",
            "link": "https://bbs.nga.cn/read.php?tid=101",
            "replies": 2,
            "postdate": 1783400000,
            "lastpost": 1783490000,
        }

        summaries = scan_thread_summaries(output_dir, {(101, "201"): metadata})
        by_dir = {item["dirName"]: item for item in summaries}

        assert by_dir["101_201"]["status"] == "ready"
        assert by_dir["101_201"]["statsLoaded"] is True
        assert by_dir["101_201"]["threadName"] == "sample"
        assert by_dir["101_201"]["subject"] == "Sample Thread"
        assert by_dir["101_201"]["postCount"] == 2
        assert by_dir["101_201"]["bodyWordCount"] == 0
        assert by_dir["101_201"]["bodyChineseCharCount"] == 0
        assert by_dir["101_201"]["bodyWordPostCount"] == 0
        assert by_dir["101_201"]["minLou"] == 1
        assert by_dir["101_201"]["maxLou"] == 2
        assert by_dir["101_201"]["authorUpdatedAt"] == 1783490002
        assert by_dir["101_201"]["link"] == (
            "https://bbs.nga.cn/read.php?tid=101&authorid=201"
        )
        assert "hasHtmlModified" not in by_dir["101_201"]
        assert by_dir["102_all"]["status"] == "needs_migration"

    def test_light_scan_skips_archive_stats(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        ready_dir = output_dir / "101_201"
        _write_archive(ready_dir, [_post(1, "hello")])

        def fail_read_archive_stats(_db_path: Path):
            raise AssertionError("light scan should not read archive stats")

        monkeypatch.setattr(
            web_data,
            "_read_archive_stats",
            fail_read_archive_stats,
        )

        summaries = scan_thread_summaries(output_dir, {}, detail="light")

        assert len(summaries) == 1
        assert summaries[0]["status"] == "ready"
        assert summaries[0]["statsLoaded"] is False
        assert summaries[0]["postCount"] is None

    def test_scans_stored_word_count_summary(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        ThreadArchiveStore(thread_dir).upsert_page(
            1,
            {
                "result": [
                    _post(1, "正文，" * 40),
                    _post(2, "短。"),
                ]
            },
        )

        summaries = scan_thread_summaries(output_dir, {})
        summary = summaries[0]

        assert summary["bodyWordCount"] == 120
        assert summary["bodyChineseCharCount"] == 80
        assert summary["bodyWordPostCount"] == 1

    def test_reads_fixed_floor_slots_without_shifting_missing_floors(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [
                _post(0, "floor zero"),
                _post(2, "floor two"),
            ],
        )

        result = read_posts(output_dir, 101, "201", page=1)

        assert result["page"] == 1
        assert result["pageStartLou"] == 0
        assert result["pageEndLou"] == 2
        assert len(result["slots"]) == 3
        assert result["slots"][0]["lou"] == 0
        assert result["slots"][0]["emptyReason"] is None
        assert result["slots"][1]["lou"] == 1
        assert result["slots"][1]["emptyReason"] == "missing"
        assert result["slots"][2]["lou"] == 2
        assert result["slots"][2]["emptyReason"] is None
        assert [item["lou"] for item in result["items"]] == [0, 2]

    def test_reads_last_page_without_tail_missing_slots(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(19, "page one end"), _post(21, "last")])

        result = read_posts(output_dir, 101, "201", page=2)

        assert result["pageStartLou"] == 20
        assert result["pageEndLou"] == 21
        assert [slot["lou"] for slot in result["slots"]] == [20, 21]
        assert result["slots"][0]["emptyReason"] == "missing"
        assert result["slots"][1]["emptyReason"] is None

    def test_reads_unfiltered_page_without_loading_full_thread(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "early"), _post(25, "target")])
        calls: list[set[int] | None] = []
        original = ThreadArchiveStore.read_effective_post_rows

        def wrapped_read_effective_post_rows(
            store: ThreadArchiveStore,
            lous: set[int] | None = None,
        ):
            calls.append(None if lous is None else set(lous))
            return original(store, lous)

        monkeypatch.setattr(
            ThreadArchiveStore,
            "read_effective_post_rows",
            wrapped_read_effective_post_rows,
        )

        result = read_posts(output_dir, 101, "201", page=2)

        assert calls == [set(range(20, 26))]
        assert result["postCount"] == 2
        assert result["matchingPostCount"] == 2
        assert [item["lou"] for item in result["items"]] == [25]

    def test_reads_filtered_page_with_full_thread_match_count(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(1, "needle early"), _post(25, "needle target")],
        )
        calls: list[set[int] | None] = []
        original = ThreadArchiveStore.read_effective_post_rows

        def wrapped_read_effective_post_rows(
            store: ThreadArchiveStore,
            lous: set[int] | None = None,
        ):
            calls.append(None if lous is None else set(lous))
            return original(store, lous)

        monkeypatch.setattr(
            ThreadArchiveStore,
            "read_effective_post_rows",
            wrapped_read_effective_post_rows,
        )

        result = read_posts(output_dir, 101, "201", page=2, query="needle")

        assert calls == [None]
        assert result["matchingPostCount"] == 2
        assert [item["lou"] for item in result["items"]] == [25]

    def test_reads_posts_and_rewrites_local_image_sources(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        image_url = "https://img.nga.178.com/attachments/mon_202607/08/abc.png"
        _write_archive(
            thread_dir,
            [
                _post(1, f"hello image[img]{image_url}[/img]"),
                _post(2, "not matched"),
            ],
        )
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        _write_image_mapping(output_dir, image_url, "images_unique/abc.png")
        ThreadArchiveStore(thread_dir).replace_floor_map(
            StoredFloorMap(
                version=FLOOR_MAP_VERSION,
                generation_version=FLOOR_MAP_GENERATION_VERSION,
                algorithm=FLOOR_MAP_HASH_ALGORITHM,
                tid=101,
                aid=201,
                input_signature="fixture",
                entries=[{"pid": 1, "author_lou": 1, "original_lou": 5}],
            )
        )
        legacy_floor_map = thread_dir / "floor_map.json"
        legacy_floor_map.write_text("{bad", encoding="utf-8")

        result = read_posts(
            output_dir,
            101,
            "201",
            page=1,
            query="hello",
        )

        assert result["matchingPostCount"] == 1
        assert result["items"][0]["authorName"] == "author-1"
        assert result["items"][0]["floorLabel"] == "第1楼（原5楼）"
        assert 'src="/api/files/images_unique/abc.png"' in result["items"][0]["html"]
        assert result["slots"][2]["emptyReason"] == "filtered"
        assert legacy_floor_map.read_text(encoding="utf-8") == "{bad"

    def test_reads_posts_sanitizes_untrusted_html(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        image_url = "https://img.nga.178.com/attachments/mon_202607/08/abc.png"
        _write_archive(
            thread_dir,
            [
                _post(
                    1,
                    (
                        '<p onclick="alert(1)">hello</p>'
                        '<script>alert("xss")</script>'
                        '<svg><animate onbegin="alert(1)" /></svg>'
                        f'[img]{image_url}[/img]'
                        '<a href="javascript:alert(3)" onclick="alert(4)">bad</a>'
                        '<span style="color:red;position:fixed">styled</span>'
                    ),
                )
            ],
        )
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        _write_image_mapping(output_dir, image_url, "images_unique/abc.png")

        result = read_posts(output_dir, 101, "201", page=1)
        html = result["items"][0]["html"]
        lowered_html = html.lower()

        assert 'src="/api/files/images_unique/abc.png"' in html
        assert "onclick" not in lowered_html
        assert "onerror" not in lowered_html
        assert "<script" not in lowered_html
        assert "<svg" not in lowered_html
        assert "javascript:" not in lowered_html
        assert "position" not in lowered_html

    def test_renders_web_only_bbcode_collapse_and_quote(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(1, "[quote]引用[/quote][collapse=标题][h]章节[/h]正文[/collapse]")],
        )

        result = read_posts(output_dir, 101, "201", page=1)
        html = result["items"][0]["html"]

        assert '<blockquote class="nga-quote">引用</blockquote>' in html
        assert '<details class="nga-collapse">' in html
        assert "<summary>标题</summary>" in html
        assert '<h4 class="nga-bbcode-heading">章节</h4>' in html

    def test_reads_posts_preserves_string_postdate(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        post = _post(1, "hello")
        post["postdate"] = "2025-06-17 13:42"
        _write_archive(thread_dir, [post])

        result = read_posts(output_dir, 101, "201", page=1)

        assert result["items"][0]["postdate"] == "2025-06-17 13:42"

    def test_safe_output_file_rejects_paths_outside_output_dir(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        allowed = output_dir / "images_unique" / "abc.png"
        allowed.parent.mkdir()
        allowed.write_bytes(b"image")
        outside = tmp_path / "secret.txt"
        outside.write_text("secret", encoding="utf-8")

        assert safe_output_file(output_dir, "images_unique/abc.png") == allowed
        assert safe_output_file(output_dir, "../secret.txt") is None


class WebServerTest:
    def test_posts_route_returns_fixed_floor_slots(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [_post(1, "hello"), _post(2, "world"), _post(19, "page end")],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads/101/201/posts", params={"page": "1"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["page"] == 1
        assert payload["pageSize"] == 20
        assert len(payload["slots"]) == 20
        assert payload["slots"][0]["emptyReason"] == "missing"
        assert payload["items"][0]["lou"] == 1

    def test_posts_route_dispatches_reader_through_threadpool(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        calls: list[str] = []

        async def fake_run_in_threadpool(func, *args, **kwargs):
            calls.append(func.__name__)
            return func(*args, **kwargs)

        monkeypatch.setattr(web_server, "run_in_threadpool", fake_run_in_threadpool)
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads/101/201/posts", params={"page": "1"})

        assert response.status_code == 200
        assert "read_posts" in calls

    def test_posts_route_reports_invalid_query_as_json_error(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads/101/201/posts", params={"limit": "0"})

        assert response.status_code == 422
        assert response.json() == {"error": "请求参数无效。"}

    def test_threads_route_light_detail_skips_archive_stats(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])

        def fail_read_archive_stats(_db_path: Path):
            raise AssertionError("light detail should not read archive stats")

        monkeypatch.setattr(
            web_data,
            "_read_archive_stats",
            fail_read_archive_stats,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads", params={"detail": "light"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["items"][0]["statsLoaded"] is False
        assert payload["items"][0]["postCount"] is None

    def test_threads_route_full_detail_uses_cache_until_refresh(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        calls: list[Path] = []
        original = web_data._read_archive_stats

        def wrapped_read_archive_stats(db_path: Path):
            calls.append(db_path)
            return original(db_path)

        monkeypatch.setattr(
            web_data,
            "_read_archive_stats",
            wrapped_read_archive_stats,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first_response = client.get("/api/threads", params={"detail": "full"})
        second_response = client.get("/api/threads", params={"detail": "full"})
        refreshed_response = client.get(
            "/api/threads",
            params={"detail": "full", "refresh": "1"},
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert refreshed_response.status_code == 200
        assert first_response.json()["items"][0]["statsLoaded"] is True
        assert len(calls) == 2

    def test_static_app_reports_missing_build_as_json_error(
        self,
        tmp_path: Path,
    ) -> None:
        client = TestClient(
            create_app(output_dir=tmp_path / "output", static_dir=tmp_path / "dist")
        )

        response = client.get("/")

        assert response.status_code == 503
        assert "缺少前端构建产物" in response.json()["error"]

    def test_admin_post_version_threads_report_multi_version_floor_counts(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        multi_version_dir = output_dir / "101_201"
        single_version_dir = output_dir / "102_202"
        legacy_dir = output_dir / "103_all"
        _write_archive(multi_version_dir, [_post(1, "before edit"), _post(2, "stable")])
        _write_archive(multi_version_dir, [_post(1, "after edit"), _post(2, "stable")])
        _write_archive(single_version_dir, [_post(1, "only version")])
        (legacy_dir / "json").mkdir(parents=True)
        (legacy_dir / "json" / "page_1.json").write_text("{}", encoding="utf-8")
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/admin/post-version-threads")
        filtered_response = client.get(
            "/api/admin/post-version-threads",
            params={"multi_version_only": "true"},
        )

        assert response.status_code == 200
        payload = response.json()
        by_dir = {item["dirName"]: item for item in payload["items"]}
        assert set(by_dir) == {"101_201", "102_202"}
        assert by_dir["101_201"]["multiVersionFloorCount"] == 1
        assert by_dir["102_202"]["multiVersionFloorCount"] == 0
        assert filtered_response.status_code == 200
        assert [
            item["dirName"] for item in filtered_response.json()["items"]
        ] == ["101_201"]

    def test_admin_post_version_threads_light_skips_full_archive_stats(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "before edit")])
        _write_archive(thread_dir, [_post(1, "after edit")])

        def fail_read_archive_stats(_db_path: Path):
            raise AssertionError("light version thread scan should not read full stats")

        monkeypatch.setattr(
            web_data,
            "_read_archive_stats",
            fail_read_archive_stats,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get(
            "/api/admin/post-version-threads",
            params={"detail": "light"},
        )

        assert response.status_code == 200
        item = response.json()["items"][0]
        assert item["dirName"] == "101_201"
        assert item["statsLoaded"] is False
        assert item["postCount"] is None
        assert item["multiVersionFloorCount"] == 1

    def test_admin_post_version_threads_use_cache_until_refresh(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "before edit")])
        _write_archive(thread_dir, [_post(1, "after edit")])
        calls: list[Path] = []
        original_read_count = web_data._read_multi_version_floor_count

        def wrapped_read_count(thread_folder: Path) -> int:
            calls.append(thread_folder)
            return original_read_count(thread_folder)

        monkeypatch.setattr(
            web_data,
            "_read_multi_version_floor_count",
            wrapped_read_count,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first_response = client.get(
            "/api/admin/post-version-threads",
            params={"detail": "light"},
        )
        second_response = client.get(
            "/api/admin/post-version-threads",
            params={"detail": "light"},
        )
        refreshed_response = client.get(
            "/api/admin/post-version-threads",
            params={"detail": "light", "refresh": "1"},
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert refreshed_response.status_code == 200
        assert len(calls) == 2

    def test_admin_post_version_selection_affects_reader_without_materialized_html(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "before edit")])
        _write_archive(thread_dir, [_post(1, "after edit")])
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            version_rows = {
                content: version_id
                for version_id, content in connection.execute(
                    "SELECT id, content FROM post_versions"
                ).fetchall()
            }
        old_version_id = version_rows["before edit"]
        latest_version_id = version_rows["after edit"]
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        groups_response = client.get("/api/admin/threads/101/201/post-versions")
        latest_response = client.put(
            "/api/admin/threads/101/201/post-version-selections/1",
            json={"versionId": latest_version_id},
        )
        select_response = client.put(
            "/api/admin/threads/101/201/post-version-selections/1",
            json={"versionId": old_version_id},
        )
        posts_response = client.get("/api/threads/101/201/posts", params={"page": "1"})

        assert groups_response.status_code == 200
        group = groups_response.json()["items"][0]
        assert group["lou"] == 1
        assert group["latestVersionId"] == latest_version_id
        assert {
            option["id"]: option["selectable"]
            for option in group["versions"]
        } == {latest_version_id: False, old_version_id: True}
        assert {option["id"]: option["content"] for option in group["versions"]} == {
            latest_version_id: "after edit",
            old_version_id: "before edit",
        }
        assert latest_response.status_code == 400
        assert latest_response.json() == {"error": "不能手动选择当前最新版。"}
        assert select_response.status_code == 200
        assert posts_response.status_code == 200
        payload = posts_response.json()
        assert "before edit" in payload["items"][0]["html"]
        assert payload["items"][0]["versionId"] == old_version_id
        assert payload["items"][0]["manualVersion"] is True
        assert (thread_dir / POST_VERSION_SELECTIONS_FILENAME).is_file()
        assert not (thread_dir / "html_modified").exists()

        clear_response = client.delete(
            "/api/admin/threads/101/201/post-version-selections/1"
        )
        refreshed_posts_response = client.get(
            "/api/threads/101/201/posts",
            params={"page": "1"},
        )

        assert clear_response.status_code == 200
        refreshed_payload = refreshed_posts_response.json()
        assert "after edit" in refreshed_payload["items"][0]["html"]
        assert refreshed_payload["items"][0]["manualVersion"] is False

    def test_admin_post_overlay_affects_reader_without_materialized_html(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "original"), _post(2, "other")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        detail_response = client.get("/api/admin/threads/101/201/overlays/1")
        preview_response = client.post(
            "/api/admin/threads/101/201/overlays/1/preview",
            json={"bbcode": '[quote]覆盖[/quote]<script>alert("x")</script>'},
        )
        save_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "[quote]覆盖[/quote]"},
        )
        posts_response = client.get("/api/threads/101/201/posts", params={"page": "1"})
        filtered_response = client.get(
            "/api/threads/101/201/posts",
            params={"page": "1", "q": "original"},
        )
        rejected_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "[img]https://example.test/a.png[/img]"},
        )

        assert detail_response.status_code == 200
        detail_payload = detail_response.json()
        assert detail_payload["hasOverlay"] is False
        assert detail_payload["bbcode"] == "original"
        assert detail_payload["html"] is None
        assert preview_response.status_code == 200
        preview_html = preview_response.json()["html"]
        assert '<blockquote class="nga-quote">覆盖</blockquote>' in preview_html
        assert "<script" not in preview_html.lower()
        assert save_response.status_code == 200
        save_payload = save_response.json()
        assert save_payload["hasOverlay"] is True
        assert save_payload["bbcode"] == "[quote]覆盖[/quote]"
        assert '<blockquote class="nga-quote">覆盖</blockquote>' in save_payload["html"]
        assert (thread_dir / POST_OVERLAYS_FILENAME).is_file()
        assert not (thread_dir / "html_modified").exists()
        assert posts_response.status_code == 200
        post_payload = posts_response.json()
        assert post_payload["items"][0]["hasOverlay"] is True
        assert '<blockquote class="nga-quote">覆盖</blockquote>' in (
            post_payload["items"][0]["html"]
        )
        assert "original" not in post_payload["items"][0]["html"]
        assert filtered_response.status_code == 200
        assert filtered_response.json()["matchingPostCount"] == 0
        assert rejected_response.status_code == 400
        assert "图片或媒体外链" in rejected_response.json()["error"]

        clear_response = client.delete("/api/admin/threads/101/201/overlays/1")
        refreshed_posts_response = client.get(
            "/api/threads/101/201/posts",
            params={"page": "1"},
        )

        assert clear_response.status_code == 200
        assert clear_response.json()["hasOverlay"] is False
        refreshed_payload = refreshed_posts_response.json()
        assert refreshed_payload["items"][0]["hasOverlay"] is False
        assert "original" in refreshed_payload["items"][0]["html"]


class WebDatabaseViewerTest:
    def test_databases_route_lists_project_sqlite_sources(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        _write_forum_thread_db(output_dir)
        _write_image_mapping(
            output_dir,
            "https://img.nga.178.com/attachments/mon_202607/08/abc.png",
            "images_unique/abc.png",
        )
        _write_archive(output_dir / "101_201", [_post(1, "hello")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/databases")

        assert response.status_code == 200
        payload = response.json()
        ids = {item["id"] for item in payload["items"]}
        assert {"forum_threads", "image_index", "archive:101_201"} <= ids
        by_id = {item["id"]: item for item in payload["items"]}
        assert by_id["forum_threads"]["relativePath"] == "forum_threads.sqlite3"
        assert by_id["archive:101_201"]["tableCount"] == 8

    def test_databases_route_uses_cache_until_refresh(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_forum_thread_db(output_dir)
        calls: list[Path] = []
        original_read_table_count = web_database._read_table_count

        def wrapped_read_table_count(db_path: Path) -> int:
            calls.append(db_path)
            return original_read_table_count(db_path)

        monkeypatch.setattr(
            web_database,
            "_read_table_count",
            wrapped_read_table_count,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first_response = client.get("/api/databases")
        second_response = client.get("/api/databases")
        refreshed_response = client.get("/api/databases", params={"refresh": "1"})

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert refreshed_response.status_code == 200
        assert len(calls) == 2

    def test_database_schema_and_rows_support_search_sort_and_detail(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_archive(
            output_dir / "101_201",
            [
                _post(1, "plain"),
                _post(2, "needle " + "x" * 300),
            ],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        schema_response = client.get("/api/databases/archive%3A101_201/schema")

        assert schema_response.status_code == 200
        table_names = {item["name"] for item in schema_response.json()["tables"]}
        assert {"page_snapshots", "post_versions"} <= table_names

        rows_response = client.get(
            "/api/databases/archive%3A101_201/tables/post_versions/rows",
            params={
                "q": "needle",
                "sort_by": "lou",
                "sort_direction": "desc",
                "limit": "1",
            },
        )

        assert rows_response.status_code == 200
        rows_payload = rows_response.json()
        assert rows_payload["total"] == 1
        row = rows_payload["rows"][0]
        assert row["rowId"] == 2
        assert row["cells"]["lou"]["value"] == 2
        assert row["cells"]["content"]["truncated"] is True

        detail_response = client.get(
            "/api/databases/archive%3A101_201/tables/post_versions/rows/2"
        )

        assert detail_response.status_code == 200
        detail_cell = detail_response.json()["row"]["cells"]["content"]
        assert detail_cell["value"] == "needle " + "x" * 300
        assert detail_cell["truncated"] is False

    def test_database_schema_uses_cache_until_refresh(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_forum_thread_db(output_dir)
        calls: list[str] = []
        original_read_row_count = web_database._read_row_count

        def wrapped_read_row_count(
            connection: sqlite3.Connection,
            table_name: str,
        ) -> Optional[int]:
            calls.append(table_name)
            return original_read_row_count(connection, table_name)

        monkeypatch.setattr(
            web_database,
            "_read_row_count",
            wrapped_read_row_count,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first_response = client.get("/api/databases/forum_threads/schema")
        second_response = client.get("/api/databases/forum_threads/schema")
        refreshed_response = client.get(
            "/api/databases/forum_threads/schema",
            params={"refresh": "1"},
        )

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert refreshed_response.status_code == 200
        assert calls == ["forum_threads_fid_784", "forum_threads_fid_784"]

    def test_database_rows_reject_unknown_sort_column(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_archive(output_dir / "101_201", [_post(1, "hello")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get(
            "/api/databases/archive%3A101_201/tables/post_versions/rows",
            params={"sort_by": "not_a_column"},
        )

        assert response.status_code == 400
        assert response.json() == {"error": "sort_by必须是当前表字段。"}

    def test_database_routes_reject_unknown_database_and_table(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_archive(output_dir / "101_201", [_post(1, "hello")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        missing_database = client.get("/api/databases/archive%3A101_999/schema")
        missing_table = client.get(
            "/api/databases/archive%3A101_201/tables/missing/rows"
        )

        assert missing_database.status_code == 404
        assert missing_table.status_code == 404


class WebCliTest:
    def test_web_serve_uses_default_localhost_and_random_port(self) -> None:
        args = args_parse(["web", "serve"])

        assert args["host"] == DEFAULT_WEB_HOST
        assert args["port"] == DEFAULT_WEB_PORT
        assert args["static_dir"] == DEFAULT_WEB_STATIC_DIR

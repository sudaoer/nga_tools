from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from nga_tools.cli.parser import args_parse
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.data import (
    ThreadConfig,
    read_posts,
    safe_output_file,
    scan_thread_summaries,
)
from nga_tools.web.server import create_app


def _write_archive(
    thread_dir: Path,
    posts: list[dict[str, object]],
) -> None:
    thread_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(thread_dir / "archive.sqlite3")
    try:
        connection.execute("CREATE TABLE page_snapshots (page_number INTEGER)")
        connection.execute("INSERT INTO page_snapshots (page_number) VALUES (1)")
        connection.execute(
            """
            CREATE TABLE post_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                pid INTEGER,
                lou INTEGER,
                post_json TEXT,
                content TEXT,
                last_seen_at TEXT
            )
            """
        )
        for post in posts:
            connection.execute(
                """
                INSERT INTO post_versions (
                    pid,
                    lou,
                    post_json,
                    content,
                    last_seen_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    post["pid"],
                    post["lou"],
                    json.dumps(post, ensure_ascii=False),
                    post["content"],
                    "2026-07-08T00:00:00+00:00",
                ),
            )
        connection.commit()
    finally:
        connection.close()


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


class WebViewerDataTest:
    def test_scans_ready_and_legacy_backup_summaries(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        ready_dir = output_dir / "101_201"
        legacy_dir = output_dir / "102_all"
        _write_archive(ready_dir, [_post(1, "hello"), _post(2, "world")])
        html_dir = ready_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text("<p>hello</p>", encoding="utf-8")
        (html_dir / "post_2.html").write_text("<p>world</p>", encoding="utf-8")
        (legacy_dir / "json").mkdir(parents=True)
        (legacy_dir / "json" / "page_1.json").write_text("{}", encoding="utf-8")
        metadata: ThreadConfig = {
            "thread_name": "sample",
            "tid": 101,
            "aid": 201,
            "subject": "Sample Thread",
            "author": "Alice",
            "replies": 2,
            "postdate": 1783400000,
            "lastpost": 1783490000,
        }

        summaries = scan_thread_summaries(output_dir, {(101, "201"): metadata})
        by_dir = {item["dirName"]: item for item in summaries}

        assert by_dir["101_201"]["status"] == "ready"
        assert by_dir["101_201"]["threadName"] == "sample"
        assert by_dir["101_201"]["subject"] == "Sample Thread"
        assert by_dir["101_201"]["postCount"] == 2
        assert by_dir["101_201"]["minLou"] == 1
        assert by_dir["101_201"]["maxLou"] == 2
        assert by_dir["102_all"]["status"] == "needs_migration"

    def test_reads_posts_and_rewrites_local_image_sources(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(
            thread_dir,
            [
                _post(1, "hello image"),
                _post(2, "not matched"),
            ],
        )
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        html_dir = thread_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text(
            '<p>hello</p><img src="../../images_unique/abc.png">',
            encoding="utf-8",
        )
        (html_dir / "post_2.html").write_text("<p>not matched</p>", encoding="utf-8")
        (thread_dir / "floor_map.json").write_text(
            json.dumps(
                {
                    "entries": [
                        {"pid": 1, "author_lou": 1, "original_lou": 5},
                    ]
                }
            ),
            encoding="utf-8",
        )

        result = read_posts(
            output_dir,
            101,
            "201",
            offset=0,
            limit=50,
            query="hello",
        )

        assert result["total"] == 1
        assert result["items"][0]["authorName"] == "author-1"
        assert result["items"][0]["floorLabel"] == "第1楼（原5楼）"
        assert 'src="/api/files/images_unique/abc.png"' in result["items"][0]["html"]

    def test_reads_posts_sanitizes_untrusted_html(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello image")])
        image_dir = output_dir / "images_unique"
        image_dir.mkdir(parents=True)
        (image_dir / "abc.png").write_bytes(b"image")
        html_dir = thread_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text(
            (
                '<p onclick="alert(1)">hello</p>'
                '<script>alert("xss")</script>'
                '<svg><animate onbegin="alert(1)" /></svg>'
                '<img src="../../images_unique/abc.png" onerror="alert(2)">'
                '<a href="javascript:alert(3)" onclick="alert(4)">bad</a>'
                '<span style="color:red;position:fixed">styled</span>'
            ),
            encoding="utf-8",
        )

        result = read_posts(output_dir, 101, "201", offset=0, limit=50)
        html = result["items"][0]["html"]
        lowered_html = html.lower()

        assert 'src="/api/files/images_unique/abc.png"' in html
        assert "onclick" not in lowered_html
        assert "onerror" not in lowered_html
        assert "<script" not in lowered_html
        assert "<svg" not in lowered_html
        assert "javascript:" not in lowered_html
        assert "position" not in lowered_html

    def test_reads_posts_preserves_string_postdate(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        post = _post(1, "hello")
        post["postdate"] = "2025-06-17 13:42"
        _write_archive(thread_dir, [post])
        html_dir = thread_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text("<p>hello</p>", encoding="utf-8")

        result = read_posts(output_dir, 101, "201", offset=0, limit=50)

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
    def test_posts_route_returns_existing_response_shape(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello"), _post(2, "world")])
        html_dir = thread_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text("<p>hello</p>", encoding="utf-8")
        (html_dir / "post_2.html").write_text("<p>world</p>", encoding="utf-8")
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads/101/201/posts", params={"limit": "1"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert payload["limit"] == 1
        assert payload["items"][0]["lou"] == 1

    def test_posts_route_reports_invalid_query_as_json_error(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        html_dir = thread_dir / "html_modified"
        html_dir.mkdir()
        (html_dir / "post_1.html").write_text("<p>hello</p>", encoding="utf-8")
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads/101/201/posts", params={"limit": "0"})

        assert response.status_code == 422
        assert response.json() == {"error": "请求参数无效。"}

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


class WebCliTest:
    def test_web_serve_uses_default_localhost_and_random_port(self) -> None:
        args = args_parse(["web", "serve"])

        assert args["host"] == DEFAULT_WEB_HOST
        assert args["port"] == DEFAULT_WEB_PORT
        assert args["static_dir"] == DEFAULT_WEB_STATIC_DIR

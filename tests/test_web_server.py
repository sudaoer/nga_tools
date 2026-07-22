from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.core.hashing import hash_text
from nga_tools.core.output_lock import use_output_root_lock
from nga_tools.web import thread_data as web_thread_data
from nga_tools.web.server import create_app
from tests.web_viewer_support import (
    _post,
    _write_archive,
    _write_audio_mapping,
    _write_image_mapping,
)


class WebServerTest:
    def test_output_file_serves_audio_byte_ranges(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        audio_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/15/range-bgm.mp3"
        )
        audio_path = _write_audio_mapping(output_dir, audio_url)
        content = audio_path.read_bytes()
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get(
            f"/api/files/audio_unique/{audio_path.name}",
            headers={"Range": "bytes=4-15"},
        )

        assert response.status_code == 206
        assert response.content == content[4:16]
        assert response.headers["content-range"] == f"bytes 4-15/{len(content)}"
        assert response.headers["accept-ranges"] == "bytes"
        assert response.headers["content-type"] == "audio/mpeg"

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
            web_thread_data,
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
            web_thread_data,
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
                source_hash: version_id
                for version_id, source_hash in connection.execute(
                    "SELECT id, source_hash FROM post_versions"
                ).fetchall()
            }
        old_version_id = version_rows[hash_text("before edit")]
        latest_version_id = version_rows[hash_text("after edit")]
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
        with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
            stored_selection = connection.execute(
                """
                SELECT lou, version_id
                FROM post_version_selections
                """
            ).fetchone()
        assert stored_selection == (1, old_version_id)
        assert not (thread_dir / "post_version_overrides.json").exists()
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
        assert ThreadArchiveStore(thread_dir).overlays.read_post_overlays()[1]["bbcode"] == (
            "[quote]覆盖[/quote]"
        )
        assert not (thread_dir / "post_overlays.json").exists()
        assert not (thread_dir / "html_modified").exists()
        assert posts_response.status_code == 200
        post_payload = posts_response.json()
        assert post_payload["items"][0]["hasOverlay"] is True
        assert '<blockquote class="nga-quote">覆盖</blockquote>' in (
            post_payload["items"][0]["html"]
        )
        assert "original" not in post_payload["items"][0]["html"]
        assert filtered_response.status_code == 200
        filtered_payload = filtered_response.json()
        assert filtered_payload["matchingPostCount"] == 0
        assert filtered_payload["slots"] == []
        assert filtered_payload["items"] == []
        assert rejected_response.status_code == 400
        assert "完整的NGA图片URL" in rejected_response.json()["error"]

        empty_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": ""},
        )
        empty_posts_response = client.get(
            "/api/threads/101/201/posts",
            params={"page": "1"},
        )

        assert empty_response.status_code == 200
        assert empty_response.json()["hasOverlay"] is True
        assert empty_response.json()["bbcode"] == ""
        assert empty_response.json()["html"] == ""
        empty_post = empty_posts_response.json()["items"][0]
        assert empty_post["hasOverlay"] is True
        assert empty_post["html"] == ""
        assert ThreadArchiveStore(thread_dir).overlays.read_post_overlays()[1]["bbcode"] == ""

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

    def test_overlay_uses_only_existing_local_nga_images(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "original")])
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/12/overlay.png"
        )
        missing_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/12/missing.png"
        )
        image_path = output_dir / "images_unique" / "overlay.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), color="white").save(image_path)
        _write_image_mapping(
            output_dir,
            image_url,
            "images_unique/overlay.png",
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        preview_response = client.post(
            "/api/admin/threads/101/201/overlays/1/preview",
            json={"bbcode": f"[img]{image_url}[/img]"},
        )
        save_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": f"[img]{image_url}[/img]"},
        )
        posts_response = client.get(
            "/api/threads/101/201/posts",
            params={"page": "1"},
        )
        missing_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": f"[img]{missing_url}[/img]"},
        )
        raw_html_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": f'<img src="{image_url}">'},
        )

        expected_src = '/api/files/images_unique/overlay.png'
        assert preview_response.status_code == 200
        assert f'src="{expected_src}"' in preview_response.json()["html"]
        assert save_response.status_code == 200
        assert f'src="{expected_src}"' in save_response.json()["html"]
        assert f'src="{expected_src}"' in posts_response.json()["items"][0]["html"]
        assert missing_response.status_code == 400
        assert "尚未下载" in missing_response.json()["error"]
        assert raw_html_response.status_code == 400
        assert "只支持[img]" in raw_html_response.json()["error"]

    def test_admin_writes_wait_for_output_layout_operations(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_archive(output_dir / "101_201", [_post(1, "original")])
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        with use_output_root_lock(output_dir):
            blocked = client.put(
                "/api/admin/threads/101/201/overlays/1",
                json={"bbcode": "blocked"},
            )

        saved = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "saved"},
        )

        assert blocked.status_code == 409
        assert "输出目录正在被另一个任务使用" in blocked.json()["error"]
        assert saved.status_code == 200

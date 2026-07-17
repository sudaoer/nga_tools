from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from PIL import Image

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.core.hashing import hash_text
from nga_tools.web import image_usage as web_image_usage
from nga_tools.web import server_state as web_server_state
from nga_tools.web.image_problem_markup import annotate_image_problem_html
from nga_tools.web.image_usage import ImageProblemIssue
from nga_tools.web.server import create_app
from tests.web_viewer_support import _post, _write_archive, _write_image_mapping


class WebImageUsageTest:
    def test_lists_image_problem_posts_by_kind_and_builds_overlay_link(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        unmapped_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/unmapped.png"
        )
        missing_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/missing.png"
        )
        healthy_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/healthy.png"
        )
        _write_image_mapping(
            output_dir,
            missing_url,
            "images_unique/missing.png",
        )
        _write_image_mapping(
            output_dir,
            healthy_url,
            "images_unique/healthy.png",
        )
        healthy_path = output_dir / "images_unique" / "healthy.png"
        healthy_path.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), color="white").save(healthy_path)
        _write_archive(
            output_dir / "101_201",
            [
                _post(
                    0,
                    '<img src="./broken.png"><img src="./broken.png">'
                    f'<img src="about:blank" data-srcorg="{unmapped_url}">'
                    '<div class="foldBox">'
                    '<div class="collapse_btn">+问题图片...</div>'
                    '<div class="collapse_content">'
                    f"[img]{missing_url}[/img]"
                    "</div></div>"
                    f"[img]{healthy_url}[/img]",
                    pid=1000,
                ),
                _post(1, '<img src="https://example.test/other.png">', pid=1001),
            ],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/admin/image-problems")
        missing_only = client.get(
            "/api/admin/image-problems",
            params={"kind": "missing_file"},
        )
        second_page = client.get(
            "/api/admin/image-problems",
            params={"offset": 1, "limit": 1},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["problemPostCount"] == 2
        assert payload["problemThreadCount"] == 1
        assert payload["problemOccurrenceCount"] == 5
        assert payload["kindCounts"] == {
            "invalid_url": {"postCount": 2, "occurrenceCount": 3},
            "unmapped": {"postCount": 1, "occurrenceCount": 1},
            "missing_file": {"postCount": 1, "occurrenceCount": 1},
        }
        assert payload["query"] == ""
        assert payload["total"] == 2
        first = payload["items"][0]
        assert first["pid"] == 1000
        assert first["lou"] == 0
        assert first["issueCount"] == 4
        assert first["editUrl"] == (
            "/threads?tid=101&aid=201&page=1&lou_from=0&lou_to=0&overlay_lou=0"
        )
        assert first["issues"] == [
            {
                "kind": "invalid_url",
                "url": "./broken.png",
                "occurrenceCount": 2,
                "imageIndexes": [1, 2],
                "sourceIndexes": [],
                "relativePath": None,
            },
            {
                "kind": "unmapped",
                "url": unmapped_url,
                "occurrenceCount": 1,
                "imageIndexes": [3],
                "sourceIndexes": [],
                "relativePath": None,
            },
            {
                "kind": "missing_file",
                "url": missing_url,
                "occurrenceCount": 1,
                "imageIndexes": [4],
                "sourceIndexes": [],
                "relativePath": "images_unique/missing.png",
            },
        ]
        assert healthy_url not in json.dumps(first["issues"])
        rendered = BeautifulSoup(first["html"], "html.parser")
        markers = rendered.select(".image-problem-inline")
        assert len(markers) == 4
        assert [marker.find("strong").get_text(" ", strip=True) for marker in markers] == [
            "第1张图片 · 链接无效",
            "第2张图片 · 链接无效",
            "第3张图片 · 未建立本地映射",
            "第4张图片 · 本地文件缺失",
        ]
        folded_problem = rendered.find("details")
        assert folded_problem is not None
        assert folded_problem.has_attr("open")
        healthy_image = rendered.find(
            "img",
            attrs={"src": "/api/files/images_unique/healthy.png"},
        )
        assert healthy_image is not None
        assert healthy_image.find_parent(class_="image-problem-inline") is None

        assert missing_only.status_code == 200
        assert missing_only.json()["total"] == 1
        assert len(missing_only.json()["items"][0]["issues"]) == 3
        assert second_page.status_code == 200
        assert second_page.json()["items"][0]["pid"] == 1001

    def test_lists_invalid_bbcode_sources_without_attachment_recovery(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        healthy_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/16/healthy-source.png"
        )
        relative_source = "./mon_202607/16/relative-source.png"
        relative_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/16/relative-source.png"
        )
        malformed_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/16/malformed-source.png"
        )
        unclosed_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202607/16/unclosed-source.png"
        )
        for url, filename in (
            (healthy_url, "healthy-source.png"),
            (relative_url, "relative-source.png"),
            (malformed_url, "malformed-source.png"),
            (unclosed_url, "unclosed-source.png"),
        ):
            relative_path = f"images_unique/{filename}"
            _write_image_mapping(output_dir, url, relative_path)
            image_path = output_dir / relative_path
            image_path.parent.mkdir(parents=True, exist_ok=True)
            Image.new("RGB", (2, 2), color="white").save(image_path)

        hbrgo_like_post = _post(
            1,
            f"[img]{healthy_url}[/img]<br/>"
            f"[img]{relative_source}[/img</span></div>]",
            pid=4101,
        )
        hbrgo_like_post["attches"] = [
            {
                "type": "img",
                "attachurl": "mon_202607/16/relative-source.png",
            }
        ]
        _write_archive(
            output_dir / "101_201",
            [
                hbrgo_like_post,
                _post(2, f"[img]{relative_source}[/img]", pid=4102),
                _post(
                    3,
                    f"[img]{malformed_url}[/img</span></div>]",
                    pid=4103,
                ),
                _post(4, f"[img]{unclosed_url}", pid=4104),
            ],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/admin/image-problems")
        search = client.get(
            "/api/admin/image-problems",
            params={"q": "relative-source.png"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["problemPostCount"] == 4
        assert payload["problemOccurrenceCount"] == 4
        assert payload["kindCounts"] == {
            "invalid_url": {"postCount": 4, "occurrenceCount": 4},
            "unmapped": {"postCount": 0, "occurrenceCount": 0},
            "missing_file": {"postCount": 0, "occurrenceCount": 0},
        }

        items_by_pid = {item["pid"]: item for item in payload["items"]}
        relative_issue = items_by_pid[4101]["issues"][0]
        assert relative_issue == {
            "kind": "invalid_url",
            "url": relative_source,
            "occurrenceCount": 1,
            "imageIndexes": [],
            "sourceIndexes": [2],
            "relativePath": None,
        }
        assert items_by_pid[4102]["issues"][0]["sourceIndexes"] == [1]
        assert items_by_pid[4103]["issues"][0] == {
            "kind": "invalid_url",
            "url": malformed_url,
            "occurrenceCount": 1,
            "imageIndexes": [],
            "sourceIndexes": [1],
            "relativePath": None,
        }
        assert items_by_pid[4104]["issues"][0] == {
            "kind": "invalid_url",
            "url": unclosed_url,
            "occurrenceCount": 1,
            "imageIndexes": [],
            "sourceIndexes": [1],
            "relativePath": None,
        }
        unclosed_rendered = BeautifulSoup(items_by_pid[4104]["html"], "html.parser")
        assert unclosed_rendered.select_one(".image-problem-unlocated") is not None
        assert "第1个 [img] · 链接无效" in unclosed_rendered.get_text(
            " ", strip=True
        )

        rendered = BeautifulSoup(items_by_pid[4101]["html"], "html.parser")
        source_marker = rendered.select_one(".image-problem-inline-source")
        assert source_marker is not None
        assert source_marker.get_text() == f"[img]{relative_source}"
        assert "第2个 [img] · 链接无效" in rendered.get_text(" ", strip=True)
        assert rendered.select_one(".image-problem-unlocated") is None
        assert search.status_code == 200
        assert {item["pid"] for item in search.json()["items"]} == {4101, 4102}

        saved = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "已通过 overlay 删除无效图片写法"},
        )
        after_overlay = client.get("/api/admin/image-problems")

        assert saved.status_code == 200
        assert after_overlay.status_code == 200
        assert after_overlay.json()["problemPostCount"] == 3
        assert {item["pid"] for item in after_overlay.json()["items"]} == {
            4102,
            4103,
            4104,
        }

    def test_searches_image_problem_posts_across_cached_snapshot(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        missing_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/MissingSearch.png"
        )
        unmapped_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/UnmappedSearch.png"
        )
        missing_path = "images_unique/MissingSearch.png"
        _write_image_mapping(output_dir, missing_url, missing_path)
        _write_archive(
            output_dir / "101_201",
            [
                _post(
                    0,
                    f"UniqueBodyNeedle [img]{missing_url}[/img]",
                    pid=1100,
                ),
                _post(7, '<img src="./InvalidSearch.png">', pid=2207),
                _post(12, f"[img]{unmapped_url}[/img]", pid=3312),
            ],
        )

        def fake_thread_title(
            tid: int,
            _paths: list[Path],
            _metadata: object,
        ) -> str:
            return "Alpha Search Subject" if tid == 101 else f"tid {tid}"

        monkeypatch.setattr(web_image_usage, "_thread_title", fake_thread_title)
        calls: list[Path] = []
        original_build = web_server_state.build_image_usage_snapshot

        def wrapped_build(path: Path):
            calls.append(path)
            return original_build(path)

        monkeypatch.setattr(
            web_server_state,
            "build_image_usage_snapshot",
            wrapped_build,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        title_match = client.get(
            "/api/admin/image-problems",
            params={"q": "alpha search subject"},
        )
        author_match = client.get(
            "/api/admin/image-problems",
            params={"q": "  AUTHOR-7  "},
        )
        body_match = client.get(
            "/api/admin/image-problems",
            params={"q": "uniquebodyneedle"},
        )
        tid_match = client.get(
            "/api/admin/image-problems",
            params={"q": "tid=101"},
        )
        pid_match = client.get(
            "/api/admin/image-problems",
            params={"q": "pid 2207"},
        )
        floor_match = client.get(
            "/api/admin/image-problems",
            params={"q": "第7楼"},
        )
        url_match = client.get(
            "/api/admin/image-problems",
            params={"q": "unmappedsearch.PNG"},
        )
        path_match = client.get(
            "/api/admin/image-problems",
            params={"q": "IMAGES_UNIQUE/missingsearch.PNG"},
        )
        kind_match = client.get(
            "/api/admin/image-problems",
            params={"q": "Alpha Search Subject", "kind": "missing_file"},
        )
        second_match = client.get(
            "/api/admin/image-problems",
            params={
                "q": "Alpha Search Subject",
                "offset": 1,
                "limit": 1,
            },
        )
        no_match = client.get(
            "/api/admin/image-problems",
            params={"q": "does-not-exist"},
        )

        assert title_match.status_code == 200
        title_payload = title_match.json()
        assert title_payload["query"] == "alpha search subject"
        assert title_payload["total"] == 3
        assert title_payload["problemPostCount"] == 3
        assert title_payload["problemThreadCount"] == 1
        assert title_payload["problemOccurrenceCount"] == 3
        assert title_payload["kindCounts"] == {
            "invalid_url": {"postCount": 1, "occurrenceCount": 1},
            "unmapped": {"postCount": 1, "occurrenceCount": 1},
            "missing_file": {"postCount": 1, "occurrenceCount": 1},
        }
        assert author_match.json()["query"] == "AUTHOR-7"
        assert [item["pid"] for item in author_match.json()["items"]] == [2207]
        assert [item["pid"] for item in body_match.json()["items"]] == [1100]
        assert tid_match.json()["total"] == 3
        assert [item["pid"] for item in pid_match.json()["items"]] == [2207]
        assert [item["pid"] for item in floor_match.json()["items"]] == [2207]
        assert [item["pid"] for item in url_match.json()["items"]] == [3312]
        assert [item["pid"] for item in path_match.json()["items"]] == [1100]

        kind_payload = kind_match.json()
        assert kind_payload["total"] == 1
        assert kind_payload["problemPostCount"] == 3
        assert kind_payload["kindCounts"] == title_payload["kindCounts"]
        assert kind_payload["items"][0]["pid"] == 1100
        assert second_match.json()["total"] == 3
        assert second_match.json()["items"][0]["pid"] == 2207
        assert no_match.json()["total"] == 0
        assert no_match.json()["problemPostCount"] == 0
        assert no_match.json()["problemThreadCount"] == 0
        assert no_match.json()["problemOccurrenceCount"] == 0
        assert no_match.json()["kindCounts"] == {
            "invalid_url": {"postCount": 0, "occurrenceCount": 0},
            "unmapped": {"postCount": 0, "occurrenceCount": 0},
            "missing_file": {"postCount": 0, "occurrenceCount": 0},
        }
        assert calls == [output_dir]

    def test_image_problem_search_tracks_current_overlay_content(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        problem_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/overlay-search.png"
        )
        _write_image_mapping(
            output_dir,
            problem_url,
            "images_unique/overlay-search.png",
        )
        _write_archive(
            output_dir / "101_201",
            [_post(1, f"OriginalSearchNeedle [img]{problem_url}[/img]")],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        before = client.get(
            "/api/admin/image-problems",
            params={"q": "OriginalSearchNeedle"},
        )
        saved = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "OverlaySearchNeedle 已修复图片问题"},
        )
        old_content = client.get(
            "/api/admin/image-problems",
            params={"q": "OriginalSearchNeedle"},
        )
        overlay_content = client.get(
            "/api/admin/image-problems",
            params={"q": "overlaysearchneedle"},
        )

        assert before.status_code == 200
        assert before.json()["total"] == 1
        assert saved.status_code == 200
        assert old_content.status_code == 200
        assert old_content.json()["total"] == 0
        assert overlay_content.status_code == 200
        assert overlay_content.json()["total"] == 0

    def test_image_problem_markup_reports_unlocated_images(self) -> None:
        issue = ImageProblemIssue(
            kind="invalid_url",
            url="./not-rendered.png",
            occurrence_count=1,
            image_indexes=(2,),
            source_indexes=(),
            relative_path=None,
        )

        rendered = BeautifulSoup(
            annotate_image_problem_html("<p>只有正文</p>", (issue,)),
            "html.parser",
        )

        fallback = rendered.select_one(".image-problem-unlocated")
        assert fallback is not None
        assert "部分问题图片未能在当前渲染正文中定位" in fallback.get_text()
        assert "第2张图片 · 链接无效" in fallback.get_text()
        assert fallback.find("code").get_text() == "./not-rendered.png"

    def test_image_problem_cache_tracks_image_directory_changes(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        image_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/appears.png"
        )
        _write_image_mapping(
            output_dir,
            image_url,
            "images_unique/appears.png",
        )
        images_dir = output_dir / "images_unique"
        images_dir.mkdir()
        _write_archive(
            output_dir / "101_201",
            [_post(1, f"[img]{image_url}[/img]")],
        )
        calls: list[Path] = []
        original_build = web_server_state.build_image_usage_snapshot

        def wrapped_build(path: Path):
            calls.append(path)
            return original_build(path)

        monkeypatch.setattr(
            web_server_state,
            "build_image_usage_snapshot",
            wrapped_build,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        missing = client.get("/api/admin/image-problems")
        image_path = images_dir / "appears.png"
        image_path.write_bytes(b"available")
        available = client.get("/api/admin/image-problems")
        image_path.unlink()
        missing_again = client.get("/api/admin/image-problems")

        assert missing.json()["kindCounts"]["missing_file"]["postCount"] == 1
        assert available.json()["problemPostCount"] == 0
        assert missing_again.json()["problemPostCount"] == 1
        assert calls == [output_dir, output_dir, output_dir]

    def test_overlay_removes_post_from_current_image_problems(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        unused_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/unused.png"
        )
        problem_url = (
            "https://img.nga.178.com/attachments/mon_202607/16/problem.png"
        )
        _write_image_mapping(
            output_dir,
            unused_url,
            "images_unique/unused.png",
        )
        _write_archive(
            output_dir / "101_201",
            [_post(1, f"[img]{problem_url}[/img]")],
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        before = client.get("/api/admin/image-problems")
        saved = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "已用 overlay 修复正文"},
        )
        after = client.get("/api/admin/image-problems")

        assert before.status_code == 200
        assert before.json()["problemPostCount"] == 1
        assert saved.status_code == 200
        assert after.status_code == 200
        assert after.json()["problemPostCount"] == 0

    def test_counts_each_reference_groups_physical_images_and_includes_zero(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        first_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/first.png"
        )
        alias_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/alias.png"
        )
        unused_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/unused.png"
        )
        unmapped_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/unmapped.png"
        )
        _write_image_mapping(output_dir, first_url, "images_unique/shared.png")
        _write_image_mapping(output_dir, alias_url, "images_unique/shared.png")
        _write_image_mapping(output_dir, unused_url, "images_unique/unused.png")
        _write_archive(
            output_dir / "101_201",
            [
                _post(
                    1,
                    f"[img]{first_url}[/img][img]{first_url}[/img]"
                    f"[img]{alias_url}[/img][img]{unmapped_url}[/img]",
                )
            ],
        )
        invalid_thread_dir = output_dir / "102_all"
        invalid_thread_dir.mkdir()
        sqlite3.connect(invalid_thread_dir / "archive.sqlite3").close()
        archive_path = output_dir / "101_201" / "archive.sqlite3"
        image_index_path = output_dir / "image_index.sqlite3"
        archive_before = archive_path.read_bytes()
        image_index_before = image_index_path.read_bytes()
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/admin/image-usage", params={"limit": 1})
        second_page = client.get(
            "/api/admin/image-usage",
            params={"offset": 1, "limit": 1},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert payload["archiveCount"] == 1
        assert payload["postCount"] == 1
        assert payload["referenceCount"] == 4
        assert payload["mappedReferenceCount"] == 3
        assert payload["unmappedReferenceCount"] == 1
        assert payload["skippedArchives"][0]["dirName"] == "102_all"
        assert payload["items"] == [
            {
                "relativePath": "images_unique/shared.png",
                "fileUrl": "/api/files/images_unique/shared.png",
                "sourceUrl": alias_url,
                "mappingCount": 2,
                "usageCount": 3,
                "replyCount": 1,
                "threadCount": 1,
            }
        ]
        assert second_page.json()["items"][0]["relativePath"] == (
            "images_unique/unused.png"
        )
        assert second_page.json()["items"][0]["usageCount"] == 0
        assert archive_path.read_bytes() == archive_before
        assert image_index_path.read_bytes() == image_index_before

    def test_tracks_effective_version_overlay_and_cache_invalidation(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        image_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/version.png"
        )
        _write_image_mapping(output_dir, image_url, "images_unique/version.png")
        image_path = output_dir / "images_unique" / "version.png"
        image_path.parent.mkdir(parents=True)
        Image.new("RGB", (2, 2), color="white").save(image_path)
        thread_dir = output_dir / "101_201"
        store = ThreadArchiveStore(thread_dir)
        store.ingest.upsert_page(
            1,
            {"totalPage": 1, "result": [_post(1, f"[img]{image_url}[/img]")]},
            observed_at="2026-07-11T00:00:00+00:00",
        )
        store.ingest.upsert_page(
            1,
            {"totalPage": 1, "result": [_post(1, "当前正文无图片")]},
            observed_at="2026-07-11T01:00:00+00:00",
        )
        with closing(sqlite3.connect(store.db_path)) as connection:
            old_version_id = connection.execute(
                "SELECT id FROM post_versions WHERE source_hash = ?",
                (hash_text(f"[img]{image_url}[/img]"),),
            ).fetchone()[0]
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        latest = client.get("/api/admin/image-usage").json()
        selected = client.put(
            "/api/admin/threads/101/201/post-version-selections/1",
            json={"versionId": old_version_id},
        )
        historical = client.get("/api/admin/image-usage").json()
        overlaid = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "覆盖正文"},
        )
        replaced = client.get("/api/admin/image-usage").json()
        image_overlaid = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": f"[img]{image_url}[/img]"},
        )
        restored_by_overlay = client.get("/api/admin/image-usage").json()

        assert latest["items"][0]["usageCount"] == 0
        assert selected.status_code == 200
        assert historical["items"][0]["usageCount"] == 1
        assert overlaid.status_code == 200
        assert replaced["items"][0]["usageCount"] == 0
        assert image_overlaid.status_code == 200
        assert restored_by_overlay["items"][0]["usageCount"] == 1

    def test_sorts_by_usage_or_threads_and_groups_reply_details(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        frequent_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/frequent.png"
        )
        broad_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/broad.png"
        )
        _write_image_mapping(
            output_dir,
            frequent_url,
            "images_unique/frequent.png",
        )
        _write_image_mapping(output_dir, broad_url, "images_unique/broad.png")
        images_dir = output_dir / "images_unique"
        images_dir.mkdir()
        (images_dir / "frequent.png").write_bytes(b"frequent")
        (images_dir / "broad.png").write_bytes(b"broad")
        _write_archive(
            output_dir / "101_201",
            [
                _post(
                    1,
                    f"[img]{frequent_url}[/img]" * 4,
                    pid=1001,
                ),
                _post(2, f"引用B[img]{broad_url}[/img]", pid=1002),
                _post(4, f"再次引用B[img]{broad_url}[/img]", pid=1004),
            ],
        )
        _write_archive(
            output_dir / "101_202",
            [_post(7, f"重复归档[img]{broad_url}[/img]", pid=1002)],
        )
        _write_archive(
            output_dir / "102_all",
            [_post(3, f"另一个主题[img]{broad_url}[/img]", pid=2003)],
        )
        archive_path = output_dir / "101_201" / "archive.sqlite3"
        archive_before = archive_path.read_bytes()
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        by_usage = client.get(
            "/api/admin/image-usage",
            params={"sort": "usage"},
        )
        by_threads = client.get(
            "/api/admin/image-usage",
            params={"sort": "threads"},
        )
        detail = client.get(
            "/api/admin/image-usage/detail",
            params={"relative_path": "images_unique/broad.png"},
        )
        replies = client.get(
            "/api/admin/image-usage/replies",
            params={
                "relative_path": "images_unique/broad.png",
                "tid": 101,
                "limit": 1,
            },
        )
        second_reply_page = client.get(
            "/api/admin/image-usage/replies",
            params={
                "relative_path": "images_unique/broad.png",
                "tid": 101,
                "offset": 1,
                "limit": 1,
            },
        )
        missing_detail = client.get(
            "/api/admin/image-usage/detail",
            params={"relative_path": "images_unique/missing.png"},
        )

        assert by_usage.status_code == 200
        assert by_usage.json()["sort"] == "usage"
        assert by_usage.json()["items"][0]["relativePath"] == (
            "images_unique/frequent.png"
        )
        broad_item = by_threads.json()["items"][0]
        assert broad_item["relativePath"] == "images_unique/broad.png"
        assert broad_item["usageCount"] == 3
        assert broad_item["replyCount"] == 3
        assert broad_item["threadCount"] == 2

        assert detail.status_code == 200
        assert [group["tid"] for group in detail.json()["threads"]] == [101, 102]
        assert detail.json()["threads"][0]["replyCount"] == 2
        assert replies.status_code == 200
        assert replies.json()["total"] == 2
        reply = replies.json()["items"][0]
        assert reply["pid"] == 1002
        assert reply["occurrenceCount"] == 1
        assert reply["readerUrl"] == "/threads?tid=101&aid=201&page=1"
        assert 'src="/api/files/images_unique/broad.png"' in reply["html"]
        assert "引用B" in reply["html"]
        assert second_reply_page.status_code == 200
        assert second_reply_page.json()["items"][0]["pid"] == 1004
        assert missing_detail.status_code == 404
        assert archive_path.read_bytes() == archive_before

    def test_requires_readable_image_index(self, tmp_path: Path) -> None:
        client = TestClient(
            create_app(output_dir=tmp_path / "output", static_dir=tmp_path / "dist")
        )

        response = client.get("/api/admin/image-usage")
        problem_response = client.get("/api/admin/image-problems")

        assert response.status_code == 409
        assert response.json() == {"error": "缺少image_index.sqlite3。"}
        assert problem_response.status_code == 409
        assert problem_response.json() == {"error": "缺少image_index.sqlite3。"}

    def test_navigation_reads_do_not_invalidate_image_usage_cache(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        image_url = (
            "https://img.nga.178.com/attachments/mon_202607/11/navigation.png"
        )
        _write_image_mapping(
            output_dir,
            image_url,
            "images_unique/navigation.png",
        )
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, f"[img]{image_url}[/img]")])
        archive_path = thread_dir / "archive.sqlite3"
        archive_before = archive_path.read_bytes()
        calls: list[Path] = []
        original_build = web_server_state.build_image_usage_snapshot

        def wrapped_build(path: Path):
            calls.append(path)
            return original_build(path)

        monkeypatch.setattr(
            web_server_state,
            "build_image_usage_snapshot",
            wrapped_build,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first = client.get("/api/admin/image-usage")
        cache = client.app.state.viewer_context.image_usage_cache
        initial_snapshot = cache._snapshot
        initial_fingerprint = cache._fingerprint
        navigation_responses = [
            client.get("/api/threads", params={"detail": "full"}),
            client.get("/api/threads/101/201/posts", params={"page": 1}),
            client.get(
                "/api/admin/post-version-threads",
                params={"detail": "full"},
            ),
            client.get("/api/admin/threads/101/201/post-versions"),
            client.get("/api/databases/archive%3A101_201/schema"),
        ]
        returned = client.get("/api/admin/image-usage")

        assert first.status_code == 200
        assert all(response.status_code == 200 for response in navigation_responses)
        assert returned.status_code == 200
        assert calls == [output_dir]
        assert cache._snapshot is initial_snapshot
        assert cache._fingerprint == initial_fingerprint
        assert archive_path.read_bytes() == archive_before

        ThreadArchiveStore(thread_dir).ingest.upsert_page(
            1,
            {"totalPage": 1, "result": [_post(1, "正文已真实更新")]},
            observed_at="2026-07-11T01:00:00+00:00",
        )
        changed = client.get("/api/admin/image-usage")

        assert changed.status_code == 200
        assert calls == [output_dir, output_dir]

    def test_full_thread_read_does_not_modify_invalid_archive(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        invalid_dir = output_dir / "101_all"
        invalid_dir.mkdir(parents=True)
        archive_path = invalid_dir / "archive.sqlite3"
        sqlite3.connect(archive_path).close()
        archive_before = archive_path.read_bytes()
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/threads", params={"detail": "full"})

        assert response.status_code == 200
        assert response.json()["items"][0]["status"] == "invalid"
        assert archive_path.read_bytes() == archive_before
        with closing(sqlite3.connect(archive_path)) as connection:
            tables = connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            ).fetchall()
        assert tables == []

    def test_reuses_memory_cache_until_refresh(
        self,
        tmp_path: Path,
        monkeypatch,
    ) -> None:
        output_dir = tmp_path / "output"
        _write_image_mapping(
            output_dir,
            "https://img.nga.178.com/attachments/mon_202607/11/cache.png",
            "images_unique/cache.png",
        )
        calls: list[Path] = []
        original_build = web_server_state.build_image_usage_snapshot

        def wrapped_build(path: Path):
            calls.append(path)
            return original_build(path)

        monkeypatch.setattr(
            web_server_state,
            "build_image_usage_snapshot",
            wrapped_build,
        )
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        first = client.get("/api/admin/image-usage")
        second = client.get("/api/admin/image-usage")
        detail = client.get(
            "/api/admin/image-usage/detail",
            params={"relative_path": "images_unique/cache.png"},
        )
        refreshed = client.get("/api/admin/image-usage", params={"refresh": "1"})

        assert first.status_code == 200
        assert second.status_code == 200
        assert detail.status_code == 200
        assert refreshed.status_code == 200
        assert calls == [output_dir, output_dir]

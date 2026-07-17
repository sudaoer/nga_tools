from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fastapi.testclient import TestClient

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.thread_stores import (
    ARCHIVE_CACHE_DB_FILENAME,
    ARCHIVE_STATE_DB_FILENAME,
)
from nga_tools.forum.ankebak_state import AnkebakStateStore
from nga_tools.web import database as web_database
from nga_tools.web.server import create_app
from tests.web_viewer_support import (
    _post,
    _write_archive,
    _write_audio_mapping,
    _write_forum_thread_db,
    _write_image_mapping,
    _write_image_validation_cache,
)


class WebDatabaseViewerTest:
    def test_databases_route_lists_project_sqlite_sources(self, tmp_path: Path) -> None:
        output_dir = tmp_path / "output"
        _write_forum_thread_db(output_dir)
        _write_image_mapping(
            output_dir,
            "https://img.nga.178.com/attachments/mon_202607/08/abc.png",
            "images_unique/abc.png",
        )
        _write_image_validation_cache(output_dir, "images_unique/abc.png")
        _write_audio_mapping(
            output_dir,
            (
                "https://img.nga.178.com/attachments/"
                "mon_202607/15/database-bgm.mp3"
            ),
        )
        AnkebakStateStore(output_dir / "backup_state.sqlite3").load_states()
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        _store = ThreadArchiveStore(thread_dir)
        _store.state.ensure_schema()
        _store.cache.ensure_schema()
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        response = client.get("/api/databases")

        assert response.status_code == 200
        payload = response.json()
        ids = {item["id"] for item in payload["items"]}
        assert {
            "forum_threads",
            "backup_state",
            "image_index",
            "image_cache",
            "audio_index",
            "archive:101_201",
            "archive_state:101_201",
            "archive_cache:101_201",
        } <= ids
        by_id = {item["id"]: item for item in payload["items"]}
        assert by_id["forum_threads"]["relativePath"] == "forum_threads.sqlite3"
        assert by_id["backup_state"]["kind"] == "backup_state"
        assert by_id["image_cache"]["relativePath"] == "image_cache.sqlite3"
        assert by_id["audio_index"]["kind"] == "audio_index"
        assert by_id["audio_index"]["relativePath"] == "audio_index.sqlite3"
        assert by_id["archive_state:101_201"]["relativePath"] == (
            f"101_201/{ARCHIVE_STATE_DB_FILENAME}"
        )
        assert by_id["archive_cache:101_201"]["relativePath"] == (
            f"101_201/{ARCHIVE_CACHE_DB_FILENAME}"
        )
        assert by_id["archive:101_201"]["tableCount"] == 10

        state_schema = client.get(
            "/api/databases/archive_state%3A101_201/schema"
        )
        assert state_schema.status_code == 200
        assert "backup_pending_images" in {
            table["name"] for table in state_schema.json()["tables"]
        }

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

    def test_overlay_write_does_not_recreate_missing_table(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        thread_dir = output_dir / "101_201"
        _write_archive(thread_dir, [_post(1, "hello")])
        with sqlite3.connect(thread_dir / "archive.sqlite3") as connection:
            connection.execute("DROP TABLE post_overlays")
        client = TestClient(
            create_app(output_dir=output_dir, static_dir=tmp_path / "dist")
        )

        before_response = client.get("/api/databases")
        save_response = client.put(
            "/api/admin/threads/101/201/overlays/1",
            json={"bbcode": "database overlay"},
        )
        with sqlite3.connect(thread_dir / "archive.sqlite3") as connection:
            table_exists = connection.execute(
                """
                SELECT 1 FROM sqlite_schema
                WHERE type = 'table' AND name = 'post_overlays'
                """
            ).fetchone()

        assert before_response.status_code == 200
        assert save_response.status_code == 400
        before_items = {
            item["id"]: item for item in before_response.json()["items"]
        }
        assert before_items["archive:101_201"]["status"] == "invalid"
        assert table_exists is None

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
        assert {
            "archive_pages",
            "post_versions",
            "post_version_selections",
            "archive_change_state",
            "post_overlays",
        } <= table_names

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
        assert calls == [
            "forum_threads_fid_784",
            "storage_metadata",
            "forum_threads_fid_784",
            "storage_metadata",
        ]

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

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from nga_tools.backup.archive_store import ThreadArchiveStore


class ThreadArchiveStoreTest:
    def test_page_refresh_preserves_posts_missing_from_new_response(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "old visible"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 2, "pid": 1002, "content": "new visible"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            records = store.read_latest_post_records()

        assert [record['lou'] for record in records] == [1, 2]
        assert records[0]['post']['content'] == 'old visible'
        assert records[1]['post']['content'] == 'new visible'

    def test_same_lou_uses_latest_version_but_keeps_old_version(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            store = ThreadArchiveStore(Path(temp_dir_name))

            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"},
                    ],
                },
                observed_at="2026-07-07T01:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"},
                    ],
                },
                observed_at="2026-07-07T02:00:00+00:00",
            )

            records = store.read_latest_post_records()
            with closing(sqlite3.connect(store.db_path)) as connection:
                version_count = connection.execute(
                    "SELECT COUNT(*) FROM post_versions WHERE pid = 1001 AND lou = 1"
                ).fetchone()[0]

        assert len(records) == 1
        assert records[0]['post']['content'] == 'after edit'
        assert version_count == 2

    def test_migrate_json_pages_is_repeatable_and_keeps_json_files(self) -> None:
        with TemporaryDirectory() as temp_dir_name:
            thread_folder = Path(temp_dir_name)
            json_dir = thread_folder / "json"
            json_dir.mkdir()
            page_path = json_dir / "page_1.json"
            page_path.write_text(
                json.dumps(
                    {
                        "totalPage": 1,
                        "result": [
                            {"lou": 1, "pid": 1001, "content": "from json"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            store = ThreadArchiveStore(thread_folder)

            first = store.migrate_json_pages()
            second = store.migrate_json_pages()
            json_still_exists = page_path.is_file()

        assert json_still_exists
        assert first.page_files == 1
        assert first.page_snapshots_inserted == 1
        assert first.post_versions_inserted == 1
        assert second.page_snapshots_inserted == 0
        assert second.post_versions_inserted == 0


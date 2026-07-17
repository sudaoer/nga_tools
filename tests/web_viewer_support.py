from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Optional

from nga_tools.backup import audio_store
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.storage import ensure_storage_metadata


def _write_archive(
    thread_dir: Path,
    posts: list[dict[str, object]],
) -> None:
    thread_dir.mkdir(parents=True, exist_ok=True)
    ThreadArchiveStore(thread_dir).ingest.upsert_page(
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
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_dir / "image_index.sqlite3")
    try:
        ensure_storage_metadata(connection, role="image_index")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS image_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO image_mappings (url, unique_rel_path)
            VALUES (?, ?)
            """,
            (url, unique_rel_path),
        )
        connection.commit()
    finally:
        connection.close()



def _write_image_validation_cache(
    output_dir: Path,
    relative_path: str,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    with closing(
        sqlite3.connect(output_dir / "image_cache.sqlite3")
    ) as connection:
        ensure_storage_metadata(connection, role="image_cache")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS image_validation_cache (
                relative_path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "INSERT INTO image_validation_cache VALUES (?, 1, 1, 1, '')",
            (relative_path,),
        )
        connection.commit()



def _write_audio_mapping(
    output_dir: Path,
    url: str,
    *,
    content: bytes | None = None,
) -> Path:
    audio_content = (
        (b"\xff\xfb\x90\x64" + bytes(413)) * 10
        if content is None
        else content
    )
    content_hash = hashlib.sha256(audio_content).hexdigest()
    relative_path = f"audio_unique/{content_hash}.mp3"
    audio_path = output_dir / relative_path
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    audio_path.write_bytes(audio_content)
    index_path = audio_store.ensure_audio_index(output_dir)
    with closing(sqlite3.connect(index_path)) as connection:
        connection.execute(
            """
            INSERT INTO audio_mappings (
                url,
                unique_rel_path,
                content_sha256,
                content_bytes,
                duration_seconds,
                created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                relative_path,
                content_hash,
                len(audio_content),
                1.0,
                "2026-07-15T00:00:00+00:00",
                "2026-07-15T00:00:00+00:00",
            ),
        )
        connection.commit()
    return audio_path



def _write_forum_thread_db(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(output_dir / "forum_threads.sqlite3")
    try:
        ensure_storage_metadata(connection, role="forum_data")
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
                replies INTEGER NOT NULL,
                topic_type INTEGER NOT NULL DEFAULT 0,
                is_forum INTEGER NOT NULL DEFAULT 0
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
                    replies,
                    topic_type,
                    is_forum
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                0,
                0,
            ),
        )
        connection.commit()
    finally:
        connection.close()

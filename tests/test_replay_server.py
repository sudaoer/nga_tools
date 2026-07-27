from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    StoredFloorMap,
)
from nga_tools.replay.corpus import ReplayCorpusError, load_replay_corpus
from nga_tools.replay.profile import (
    ReplayProfile,
    ReplayProfileError,
    TrafficProfile,
    load_replay_profile,
)
from nga_tools.replay.rate_limit import SharedBandwidthLimiter
from nga_tools.replay.server import create_replay_app
from nga_tools.replay import server as replay_server
from nga_tools.storage import ensure_storage_metadata

IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "replay-image.png"
)
MISSING_IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "missing-image.png"
)
AUDIO_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "replay-audio.mp3"
)
MISSING_AUDIO_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "missing-audio.mp3"
)


def _author_post(
    lou: int,
    pid: int,
    content: str,
    *,
    attachment_urls: tuple[str, ...] = (),
) -> dict[str, object]:
    post: dict[str, object] = {
        "lou": lou,
        "pid": pid,
        "content": content,
        "author": {"uid": 456, "username": "author"},
        "postdate": "2026-07-12 12:34",
        "attches": [
            {"type": "img", "attachurl": url}
            for url in attachment_urls
        ],
    }
    return post


def _anonymous_post(lou: int, pid: int, content: str) -> dict[str, object]:
    return {
        "lou": lou,
        "pid": pid,
        "content": content,
        "author": {"uid": -1, "username": "匿名"},
        "postdate": 1_784_108_800,
        "attches": [],
    }


def _page(
    page_number: int,
    total_page: int,
    posts: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "code": 0,
        "currentPage": page_number,
        "totalPage": total_page,
        "vrows": 2,
        "result": posts,
    }


def _write_image_index(output_dir: Path, *, unsafe: bool = False) -> Path:
    images_dir = output_dir / "images_unique"
    images_dir.mkdir(parents=True, exist_ok=True)
    image_path = images_dir / "stored.png"
    image_path.write_bytes(b"replayed-image-bytes")
    connection = sqlite3.connect(output_dir / "image_index.sqlite3")
    try:
        ensure_storage_metadata(connection, role="image_index")
        connection.execute(
            """
            CREATE TABLE image_mappings (
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
            (
                IMAGE_URL,
                "../outside.png" if unsafe else "images_unique/stored.png",
            ),
        )
        if not unsafe:
            connection.execute(
                """
                INSERT INTO image_mappings (url, unique_rel_path)
                VALUES (?, ?)
                """,
                (
                    MISSING_IMAGE_URL,
                    "images_unique/missing.png",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return image_path


def _write_audio_index(output_dir: Path) -> Path:
    audio_dir = output_dir / "audio_unique"
    audio_dir.mkdir(parents=True, exist_ok=True)
    content = (b"\xff\xfb\x90\x64" + bytes(413)) * 10
    content_hash = hashlib.sha256(content).hexdigest()
    audio_path = audio_dir / f"{content_hash}.mp3"
    audio_path.write_bytes(content)
    with sqlite3.connect(output_dir / "audio_index.sqlite3") as connection:
        ensure_storage_metadata(connection, role="audio_index")
        connection.execute(
            """
            CREATE TABLE audio_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                content_bytes INTEGER NOT NULL,
                duration_seconds REAL NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE INDEX idx_audio_mappings_unique_rel_path
            ON audio_mappings(unique_rel_path)
            """
        )
        rows = [
            (
                AUDIO_URL,
                f"audio_unique/{content_hash}.mp3",
                content_hash,
                len(content),
                0.25,
            ),
            (
                MISSING_AUDIO_URL,
                "audio_unique/missing.mp3",
                "0" * 64,
                len(content),
                0.25,
            ),
        ]
        connection.executemany(
            """
            INSERT INTO audio_mappings VALUES (?, ?, ?, ?, ?, '', '')
            """,
            rows,
        )
    return audio_path


def _build_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    output_dir = tmp_path / "output"
    thread_dir = output_dir / "123_456"
    store = ThreadArchiveStore(thread_dir)
    store.ingest.upsert_page(
        1,
        _page(1, 1, [_author_post(0, 100, "old")]),
        observed_at="2026-07-10T00:00:00+00:00",
    )
    store.ingest.upsert_pages(
        {
            1: _page(
                1,
                2,
                [_author_post(0, 100, "latest", attachment_urls=(IMAGE_URL,))],
            ),
            2: _page(2, 2, [_author_post(21, 101, "second")]),
        },
        observed_at="2026-07-12T00:00:00+00:00",
    )
    store.floor_maps.replace_floor_map(
        StoredFloorMap(
            version=FLOOR_MAP_VERSION,
            generation_version=FLOOR_MAP_GENERATION_VERSION,
            algorithm=FLOOR_MAP_HASH_ALGORITHM,
            tid=123,
            aid=456,
            input_signature="test-signature",
            entries=[
                {"pid": 100, "author_lou": 0, "original_lou": 0},
                {"pid": 101, "author_lou": 21, "original_lou": 25},
                {
                    "pid": None,
                    "author_lou": 10,
                    "original_lou": 10,
                    "original_pid": 900,
                },
                {"pid": None, "author_lou": 11, "original_lou": 13},
                {
                    "pid": None,
                    "author_lou": 12,
                    "original_lou": None,
                    "candidate_original_lous": [11, 12],
                },
            ],
        )
    )
    store.ingest.upsert_recovered_posts(
        {
            10: {
                "original_pid": 900,
                "original_lou": 10,
                "content": "recovered anonymous",
                "raw_post": _anonymous_post(
                    10,
                    900,
                    "recovered anonymous",
                ),
            }
        },
        observed_at="2026-07-12T01:00:00+00:00",
    )
    thread_config = tmp_path / "thread_configs.json"
    thread_config.write_text(
        json.dumps(
            {
                "ThreadList": [
                    {
                        "thread_name": "replay-test",
                        "tid": 123,
                        "aid": 456,
                        "replies": 25,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    image_path = _write_image_index(output_dir)
    _write_audio_index(output_dir)
    return output_dir, thread_config, image_path


def _unlimited_profile() -> ReplayProfile:
    return ReplayProfile(
        api=TrafficProfile(
            latency_ms=0,
            bandwidth_bytes_per_second=0,
            max_inflight=4,
        ),
        image=TrafficProfile(
            latency_ms=0,
            bandwidth_bytes_per_second=0,
            max_inflight=4,
        ),
        chunk_bytes=4,
    )


def test_small_file_source_uses_one_thread_handoff(tmp_path: Path) -> None:
    media_path = tmp_path / "small.bin"
    payload = b"small replay payload"
    media_path.write_bytes(payload)

    async def collect() -> list[bytes]:
        return [
            chunk
            async for chunk in replay_server._file_source(
                media_path,
                len(payload),
                len(payload),
            )
        ]

    real_to_thread = asyncio.to_thread
    with patch(
        "nga_tools.replay.server.asyncio.to_thread",
        wraps=real_to_thread,
    ) as to_thread:
        chunks = asyncio.run(collect())

    assert chunks == [payload]
    assert to_thread.call_count == 1


class ReplayCorpusTest:
    def test_loads_content_pages_images_and_floor_map_original(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        archive_path = output_dir / "123_456" / "archive.sqlite3"
        with sqlite3.connect(archive_path) as connection:
            connection.execute(
                """
                UPDATE post_latest_metadata
                SET image_attachments_json = ?
                WHERE pid = 100 AND lou = 0
                """,
                (
                    json.dumps(
                        [
                            {
                                "url": IMAGE_URL,
                                "path": "mon_202607/12/replay-image.png",
                                "name": "replay-image.png",
                            }
                        ]
                    ),
                ),
            )
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        tracked_paths = [
            archive_path,
            Path(f"{archive_path}-wal"),
            Path(f"{archive_path}-shm"),
            output_dir / "image_index.sqlite3",
            output_dir / "audio_index.sqlite3",
        ]
        source_states_before = {
            path: (
                path.exists(),
                path.stat().st_size if path.exists() else 0,
                path.stat().st_mtime_ns if path.exists() else 0,
            )
            for path in tracked_paths
        }

        corpus = load_replay_corpus(output_dir, thread_config)

        source_states_after = {
            path: (
                path.exists(),
                path.stat().st_size if path.exists() else 0,
                path.stat().st_mtime_ns if path.exists() else 0,
            )
            for path in tracked_paths
        }
        assert source_states_after == source_states_before

        content_page = corpus.page(123, 456, 1)
        assert content_page is not None
        assert content_page.floor_map_original is False
        content_data = json.loads(content_page.payload)
        assert content_data["totalPage"] == 2
        assert content_data["vrows"] == 22
        assert content_data["result"] == [
            {
                "pid": 100,
                "lou": 0,
                "content": "latest",
                "author": {"uid": 456, "username": "author"},
                "postdate": "2026-07-12 12:34",
                "attches": [],
            }
        ]
        second_content_page = corpus.page(123, 456, 2)
        assert second_content_page is not None
        assert json.loads(second_content_page.payload)["result"][0]["content"] == (
            "second"
        )

        floor_map_first = corpus.page(123, None, 1)
        assert floor_map_first is not None
        assert floor_map_first.floor_map_original is True
        first_data = json.loads(floor_map_first.payload)
        assert first_data["totalPage"] == 2
        assert len(first_data["result"]) == 17
        assert {11, 12, 13}.isdisjoint(
            {post["lou"] for post in first_data["result"]}
        )
        assert first_data["result"][0]["pid"] == 100
        assert first_data["result"][0]["content"] == "latest"
        recovered = next(post for post in first_data["result"] if post["lou"] == 10)
        assert recovered["pid"] == 900
        assert recovered["content"] == "recovered anonymous"
        assert recovered["author"] == {"uid": -1, "username": "匿名"}

        floor_map_second = corpus.page(123, None, 2)
        assert floor_map_second is not None
        second_data = json.loads(floor_map_second.payload)
        assert [post["lou"] for post in second_data["result"]] == list(range(20, 26))
        assert second_data["result"][-1]["pid"] == 101
        assert second_data["result"][-1]["content"] == "second"
        assert corpus.page(123, None, 3) is None

        assert corpus.image(IMAGE_URL) is not None
        assert corpus.image(MISSING_IMAGE_URL) is None
        assert corpus.audio(AUDIO_URL) is not None
        assert corpus.audio(MISSING_AUDIO_URL) is None
        assert corpus.manifest.archive_content_post_count == 3
        assert corpus.manifest.archive_content_page_count == 2
        assert corpus.manifest.floor_map_original_thread_count == 1
        assert corpus.manifest.floor_map_original_page_count == 2
        assert corpus.manifest.locatable_pid_count == 3
        first_pid_target = corpus.pid_target(100)
        assert first_pid_target is not None
        assert first_pid_target.tid == 123
        assert first_pid_target.page_number == 1
        second_pid_target = corpus.pid_target(101)
        assert second_pid_target is not None
        assert second_pid_target.page_number == 2
        recovered_pid_target = corpus.pid_target(900)
        assert recovered_pid_target is not None
        assert recovered_pid_target.page_number == 1
        assert corpus.manifest.image_mapping_count == 2
        assert corpus.manifest.available_image_mapping_count == 1
        assert corpus.manifest.unavailable_image_mapping_count == 1
        manifest_data = corpus.manifest.as_dict()
        assert corpus.manifest.audio_mapping_count == 2
        assert corpus.manifest.available_audio_mapping_count == 1
        assert corpus.manifest.unavailable_audio_mapping_count == 1
        assert manifest_data["corpus_format_version"] == 8
        assert {
            "exact_page_count",
            "exact_page_payload_bytes",
            "synthetic_thread_count",
        }.isdisjoint(manifest_data)

        reloaded = load_replay_corpus(output_dir, thread_config)
        assert reloaded.manifest.corpus_id == corpus.manifest.corpus_id

    def test_loads_from_compact_archive_schema(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)

        corpus = load_replay_corpus(output_dir, thread_config)

        first_page = corpus.page(123, 456, 1)
        assert first_page is not None
        assert json.loads(first_page.payload)["result"][0]["content"] == "latest"
        assert corpus.manifest.archive_content_page_count == 2

    def test_rejects_unsupported_archive_schema(self, tmp_path: Path) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        archive_path = output_dir / "123_456" / "archive.sqlite3"
        with sqlite3.connect(archive_path) as connection:
            connection.execute("PRAGMA user_version = 0")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        with pytest.raises(ReplayCorpusError, match="版本不受支持"):
            load_replay_corpus(output_dir, thread_config)

    def test_synthesizes_original_pages_from_tid_all_content(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        original_store = ThreadArchiveStore(output_dir / "123_all")
        original_store.ingest.upsert_pages(
            {
                1: _page(
                    1,
                    2,
                    [
                        _author_post(0, 100, "original latest"),
                        _anonymous_post(10, 900, "recovered anonymous"),
                    ],
                ),
                2: _page(2, 2, [_author_post(25, 101, "original second")]),
            },
            observed_at="2026-07-12T02:00:00+00:00",
        )
        corpus = load_replay_corpus(output_dir, thread_config)

        first_page = corpus.page(123, None, 1)
        assert first_page is not None
        assert first_page.floor_map_original is False
        first_data = json.loads(first_page.payload)
        assert first_data["vrows"] == 26
        assert [post["lou"] for post in first_data["result"]] == [0, 10]
        assert first_data["result"][0]["content"] == "original latest"
        assert first_data["result"][1]["content"] == "recovered anonymous"

        second_page = corpus.page(123, None, 2)
        assert second_page is not None
        assert json.loads(second_page.payload)["result"] == [
            {
                "pid": 101,
                "lou": 25,
                "content": "original second",
                "author": {"uid": 456, "username": "author"},
                "postdate": "2026-07-12 12:34",
                "attches": [],
            }
        ]
        assert corpus.manifest.archive_content_post_count == 6
        assert corpus.manifest.archive_content_page_count == 4
        assert corpus.manifest.floor_map_original_thread_count == 0
        assert corpus.manifest.floor_map_original_page_count == 0

        author_archive = output_dir / "123_456" / "archive.sqlite3"
        with sqlite3.connect(author_archive) as connection:
            connection.execute("DELETE FROM floor_map_candidates")
            connection.execute("DELETE FROM floor_map_entries")
            connection.execute("DELETE FROM floor_map_state")
            connection.commit()
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

        corpus_without_floor_map = load_replay_corpus(output_dir, thread_config)
        original_page = corpus_without_floor_map.page(123, None, 1)
        assert original_page is not None
        assert original_page.floor_map_original is False
        assert corpus_without_floor_map.manifest.locatable_pid_count == 0

    def test_rejects_database_with_uncheckpointed_wal(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        archive_path = output_dir / "123_456" / "archive.sqlite3"
        connection = sqlite3.connect(archive_path)
        try:
            connection.execute("PRAGMA wal_autocheckpoint = 0")
            connection.execute("CREATE TABLE replay_uncheckpointed(value INTEGER)")
            connection.commit()

            with pytest.raises(ReplayCorpusError, match="未检查点的WAL"):
                load_replay_corpus(output_dir, thread_config)
        finally:
            connection.close()

    def test_rejects_image_mapping_outside_images_unique(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        (output_dir / "image_index.sqlite3").unlink()
        _write_image_index(output_dir, unsafe=True)

        with pytest.raises(ReplayCorpusError, match="路径越界"):
            load_replay_corpus(output_dir, thread_config)

    def test_allows_missing_audio_index(self, tmp_path: Path) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        (output_dir / "audio_index.sqlite3").unlink()

        corpus = load_replay_corpus(output_dir, thread_config)

        assert corpus.audio(AUDIO_URL) is None
        assert corpus.manifest.audio_mapping_count == 0
        assert corpus.manifest.available_audio_mapping_count == 0
        assert corpus.manifest.unavailable_audio_mapping_count == 0
        assert corpus.manifest.unique_audio_file_count == 0
        assert corpus.manifest.unique_audio_file_bytes == 0

    def test_rejects_audio_mapping_outside_audio_unique(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        with sqlite3.connect(output_dir / "audio_index.sqlite3") as connection:
            connection.execute(
                """
                UPDATE audio_mappings
                SET unique_rel_path = '../outside.mp3'
                WHERE url = ?
                """,
                (AUDIO_URL,),
            )

        with pytest.raises(ReplayCorpusError, match="音频索引路径越界"):
            load_replay_corpus(output_dir, thread_config)

    @pytest.mark.parametrize("invalid_metadata", ["hash", "size"])
    def test_treats_audio_with_invalid_content_metadata_as_unavailable(
        self,
        tmp_path: Path,
        invalid_metadata: str,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        with sqlite3.connect(output_dir / "audio_index.sqlite3") as connection:
            if invalid_metadata == "hash":
                connection.execute(
                    """
                    UPDATE audio_mappings
                    SET content_sha256 = ?
                    WHERE url = ?
                    """,
                    ("f" * 64, AUDIO_URL),
                )
            else:
                connection.execute(
                    """
                    UPDATE audio_mappings
                    SET content_bytes = 1
                    WHERE url = ?
                    """,
                    (AUDIO_URL,),
                )

        corpus = load_replay_corpus(output_dir, thread_config)

        assert corpus.audio(AUDIO_URL) is None
        assert corpus.manifest.audio_mapping_count == 2
        assert corpus.manifest.available_audio_mapping_count == 0
        assert corpus.manifest.unavailable_audio_mapping_count == 2
        assert corpus.manifest.unique_audio_file_count == 0
        assert corpus.manifest.unique_audio_file_bytes == 0


class ReplayServerTest:
    def test_serves_api_images_metrics_and_reset(self, tmp_path: Path) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        corpus = load_replay_corpus(output_dir, thread_config)
        client = TestClient(create_replay_app(corpus, _unlimited_profile()))

        exact_response = client.post(
            "/app_api.php?__lib=post&__act=list",
            data={"tid": "123", "authorid": "456", "page": "1"},
        )
        assert exact_response.status_code == 200
        assert exact_response.json()["result"][0]["content"] == "latest"

        floor_map_response = client.post(
            "/app_api.php?__lib=post&__act=list",
            data={"tid": "123", "page": "2"},
        )
        assert floor_map_response.status_code == 200
        assert floor_map_response.json()["result"][-1]["pid"] == 101

        redirect_response = client.get(
            "/read.php",
            params={"pid": "101", "opt": "128"},
            follow_redirects=False,
        )
        assert redirect_response.status_code == 302
        assert redirect_response.headers["location"] == (
            "/read.php?tid=123&page=2#pid101Anchor"
        )
        missing_redirect_response = client.get(
            "/read.php",
            params={"pid": "999", "opt": "128"},
            follow_redirects=False,
        )
        assert missing_redirect_response.status_code == 200
        assert "location" not in missing_redirect_response.headers

        missing_page_response = client.post(
            "/app_api.php?__lib=post&__act=list",
            data={"tid": "123", "authorid": "456", "page": "3"},
        )
        assert missing_page_response.status_code == 200
        assert missing_page_response.json()["code"] == -1

        image_response = client.get(
            "/__replay__/image",
            params={"url": IMAGE_URL},
        )
        assert image_response.status_code == 200
        assert image_response.content == b"replayed-image-bytes"
        missing_image_response = client.get(
            "/__replay__/image",
            params={"url": MISSING_IMAGE_URL},
        )
        assert missing_image_response.status_code == 404

        audio_response = client.get(
            "/__replay__/audio",
            params={"url": AUDIO_URL},
        )
        assert audio_response.status_code == 200
        assert audio_response.headers["content-type"].startswith("audio/mpeg")
        missing_audio_response = client.get(
            "/__replay__/audio",
            params={"url": MISSING_AUDIO_URL},
        )
        assert missing_audio_response.status_code == 404

        manifest = client.get("/__replay__/manifest").json()
        assert manifest["corpus_id"] == corpus.manifest.corpus_id
        assert manifest["profile_id"] == _unlimited_profile().profile_id

        metrics = client.get("/__replay__/metrics").json()
        assert metrics["api"]["requests"] == 5
        assert metrics["api"]["floor_map_original_requests"] == 1
        assert metrics["api"]["statuses"] == {"200": 4, "302": 1}
        assert metrics["api"]["operations"] == {
            "author_post_list": 1,
            "original_post_list": 1,
            "pid_redirect": 2,
        }
        assert metrics["image"]["requests"] == 2
        assert metrics["image"]["statuses"] == {"200": 1, "404": 1}
        assert metrics["audio"]["requests"] == 2
        assert metrics["audio"]["statuses"] == {"200": 1, "404": 1}

        reset_response = client.post("/__replay__/reset")
        assert reset_response.status_code == 200
        reset_metrics = client.get("/__replay__/metrics").json()
        assert reset_metrics["api"]["requests"] == 0
        assert reset_metrics["image"]["requests"] == 0
        assert reset_metrics["audio"]["requests"] == 0


class ReplayProfileTest:
    def test_loads_strict_profile_and_builds_stable_id(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "api": {
                        "latency_ms": 150,
                        "bandwidth_bytes_per_second": 2_000_000,
                        "max_inflight": 4,
                    },
                    "image": {
                        "latency_ms": 100,
                        "bandwidth_bytes_per_second": 20_000_000,
                        "max_inflight": 50,
                    },
                    "chunk_bytes": 65536,
                }
            ),
            encoding="utf-8",
        )

        first = load_replay_profile(profile_path)
        second = load_replay_profile(profile_path)

        assert first == second
        assert first.profile_id == second.profile_id
        assert first.api.latency_ms == 150
        assert first.audio == first.image

    def test_rejects_unknown_or_invalid_profile_fields(self, tmp_path: Path) -> None:
        profile_path = tmp_path / "profile.json"
        profile_path.write_text(
            json.dumps(
                {
                    "api": {
                        "latency_ms": -1,
                        "bandwidth_bytes_per_second": 1,
                        "max_inflight": 1,
                    },
                    "image": {
                        "latency_ms": 0,
                        "bandwidth_bytes_per_second": 1,
                        "max_inflight": 1,
                    },
                    "chunk_bytes": 1,
                    "jitter": True,
                }
            ),
            encoding="utf-8",
        )

        with pytest.raises(ReplayProfileError, match="未知配置项"):
            load_replay_profile(profile_path)


class ReplayRateLimitTest:
    def test_reserves_one_shared_bandwidth_timeline(self) -> None:
        now = 0.0
        waits: list[float] = []

        def clock() -> float:
            return now

        async def sleep(seconds: float) -> None:
            nonlocal now
            waits.append(seconds)
            now += seconds

        limiter = SharedBandwidthLimiter(
            100,
            clock=clock,
            sleep=sleep,
        )

        assert asyncio.run(limiter.wait_for_bytes(50)) == pytest.approx(0.5)
        assert asyncio.run(limiter.wait_for_bytes(25)) == pytest.approx(0.25)
        assert waits == pytest.approx([0.5, 0.25])
        limiter.reset()
        assert limiter.reserve_wait_seconds(10) == pytest.approx(0.1)

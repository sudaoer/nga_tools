from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    StoredFloorMap,
)
from nga_tools.cli import args_parse
from nga_tools.replay.corpus import ReplayCorpusError, load_replay_corpus
from nga_tools.replay.profile import (
    ReplayProfile,
    ReplayProfileError,
    TrafficProfile,
    load_replay_profile,
)
from nga_tools.replay.rate_limit import SharedBandwidthLimiter
from nga_tools.replay.server import (
    DEFAULT_REPLAY_HOST,
    DEFAULT_REPLAY_PORT,
    create_replay_app,
)

IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "replay-image.png"
)
MISSING_IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/12/"
    "missing-image.png"
)


def _author_post(lou: int, pid: int, content: str) -> dict[str, object]:
    return {
        "lou": lou,
        "pid": pid,
        "content": content,
        "author": {"uid": 456, "username": "author"},
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
                url, unique_rel_path, created_at, updated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                IMAGE_URL,
                "../outside.png" if unsafe else "images_unique/stored.png",
                "2026-07-12T00:00:00+00:00",
                "2026-07-12T00:00:00+00:00",
            ),
        )
        if not unsafe:
            connection.execute(
                """
                INSERT INTO image_mappings (
                    url, unique_rel_path, created_at, updated_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    MISSING_IMAGE_URL,
                    "images_unique/missing.png",
                    "2026-07-12T00:00:00+00:00",
                    "2026-07-12T00:00:00+00:00",
                ),
            )
        connection.commit()
    finally:
        connection.close()
    return image_path


def _build_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    output_dir = tmp_path / "output"
    thread_dir = output_dir / "123_456"
    store = ThreadArchiveStore(thread_dir)
    store.upsert_page(
        1,
        _page(1, 1, [_author_post(0, 100, "old")]),
        observed_at="2026-07-10T00:00:00+00:00",
    )
    store.upsert_pages(
        {
            1: _page(1, 2, [_author_post(0, 100, "latest")]),
            2: _page(2, 2, [_author_post(1, 101, "second")]),
        },
        observed_at="2026-07-12T00:00:00+00:00",
    )
    store.replace_floor_map(
        StoredFloorMap(
            version=FLOOR_MAP_VERSION,
            generation_version=FLOOR_MAP_GENERATION_VERSION,
            algorithm=FLOOR_MAP_HASH_ALGORITHM,
            tid=123,
            aid=456,
            input_signature="test-signature",
            entries=[
                {"pid": 100, "author_lou": 0, "original_lou": 0},
                {"pid": 101, "author_lou": 1, "original_lou": 25},
                {"pid": None, "author_lou": 2, "original_lou": 10},
                {
                    "pid": None,
                    "author_lou": 3,
                    "original_lou": None,
                    "candidate_original_lous": [11, 12],
                },
                {"pid": 0, "author_lou": 4, "original_lou": 2},
            ],
        )
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


class ReplayCorpusTest:
    def test_loads_latest_pages_images_and_synthetic_original(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        archive_path = output_dir / "123_456" / "archive.sqlite3"
        tracked_paths = [
            archive_path,
            Path(f"{archive_path}-wal"),
            Path(f"{archive_path}-shm"),
            output_dir / "image_index.sqlite3",
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

        exact_page = corpus.page(123, 456, 1)
        assert exact_page is not None
        assert exact_page.synthetic_original is False
        exact_data = json.loads(exact_page.payload)
        assert exact_data["result"][0]["content"] == "latest"

        synthetic_first = corpus.page(123, None, 1)
        assert synthetic_first is not None
        assert synthetic_first.synthetic_original is True
        first_data = json.loads(synthetic_first.payload)
        assert first_data["totalPage"] == 2
        assert len(first_data["result"]) == 17
        assert {10, 11, 12}.isdisjoint(
            {post["lou"] for post in first_data["result"]}
        )
        assert first_data["result"][0]["pid"] == 100

        synthetic_second = corpus.page(123, None, 2)
        assert synthetic_second is not None
        second_data = json.loads(synthetic_second.payload)
        assert [post["lou"] for post in second_data["result"]] == list(range(20, 26))
        assert second_data["result"][-1]["pid"] == 101
        assert corpus.page(123, None, 3) is None

        assert corpus.image(IMAGE_URL) is not None
        assert corpus.image(MISSING_IMAGE_URL) is None
        assert corpus.manifest.exact_page_count == 2
        assert corpus.manifest.synthetic_thread_count == 1
        assert corpus.manifest.locatable_pid_count == 2
        first_pid_target = corpus.pid_target(100)
        assert first_pid_target is not None
        assert first_pid_target.tid == 123
        assert first_pid_target.page_number == 1
        second_pid_target = corpus.pid_target(101)
        assert second_pid_target is not None
        assert second_pid_target.page_number == 2
        assert corpus.manifest.image_mapping_count == 2
        assert corpus.manifest.available_image_mapping_count == 1
        assert corpus.manifest.unavailable_image_mapping_count == 1

        reloaded = load_replay_corpus(output_dir, thread_config)
        assert reloaded.manifest.corpus_id == corpus.manifest.corpus_id

    def test_uses_newer_last_page_when_first_page_count_is_stale(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir, thread_config, _image_path = _build_source(tmp_path)
        store = ThreadArchiveStore(output_dir / "123_456")
        store.upsert_page(
            3,
            _page(3, 3, [_author_post(2, 102, "third")]),
            observed_at="2026-07-13T00:00:00+00:00",
        )

        corpus = load_replay_corpus(output_dir, thread_config)

        assert corpus.manifest.exact_page_count == 3
        for page_number in range(1, 4):
            replay_page = corpus.page(123, 456, page_number)
            assert replay_page is not None
            assert json.loads(replay_page.payload)["totalPage"] == 3
        assert corpus.page(123, 456, 4) is None

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

        synthetic_response = client.post(
            "/app_api.php?__lib=post&__act=list",
            data={"tid": "123", "page": "2"},
        )
        assert synthetic_response.status_code == 200
        assert synthetic_response.json()["result"][-1]["pid"] == 101

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

        manifest = client.get("/__replay__/manifest").json()
        assert manifest["corpus_id"] == corpus.manifest.corpus_id
        assert manifest["profile_id"] == _unlimited_profile().profile_id

        metrics = client.get("/__replay__/metrics").json()
        assert metrics["api"]["requests"] == 5
        assert metrics["api"]["synthetic_original_requests"] == 1
        assert metrics["api"]["statuses"] == {"200": 4, "302": 1}
        assert metrics["api"]["operations"] == {
            "author_post_list": 1,
            "original_post_list": 1,
            "pid_redirect": 2,
        }
        assert metrics["image"]["requests"] == 2
        assert metrics["image"]["statuses"] == {"200": 1, "404": 1}

        reset_response = client.post("/__replay__/reset")
        assert reset_response.status_code == 200
        reset_metrics = client.get("/__replay__/metrics").json()
        assert reset_metrics["api"]["requests"] == 0
        assert reset_metrics["image"]["requests"] == 0


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


class ReplayCliTest:
    def test_replay_serve_parses_with_safe_defaults(self) -> None:
        args = args_parse(
            [
                "replay",
                "serve",
                "--source-output",
                "output",
                "--profile",
                "replay_profile.json",
            ]
        )

        assert args["command"] == "replay"
        assert args["action"] == "serve"
        assert args["host"] == DEFAULT_REPLAY_HOST
        assert args["port"] == DEFAULT_REPLAY_PORT

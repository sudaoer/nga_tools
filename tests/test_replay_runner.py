from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sqlite3
import threading
import time
from collections.abc import Generator
from contextlib import closing, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from unittest.mock import patch

import pytest
import uvicorn
from PIL import Image

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.content_codec import decode_content
from nga_tools.backup.image_validation_store import IMAGE_CACHE_FILENAME
from nga_tools.backup.floor_models import (
    FLOOR_MAP_GENERATION_VERSION,
    FLOOR_MAP_HASH_ALGORITHM,
    FLOOR_MAP_VERSION,
    StoredFloorMap,
)
from nga_tools.cli import args_parse, dispatch_command
from nga_tools.commands.thread_batch import ThreadBatchResult
from nga_tools.core.downloads import download_files
from nga_tools.core.hashing import hash_text
from nga_tools.ngaclient.session import create_api_session
from nga_tools.replay.offline import (
    ReplayOfflineError,
    audio_request_url,
    assert_replay_request_allowed,
    image_request_url,
    use_replay_network_policy,
)
from nga_tools.replay.orchestrator import run_replay_test
from nga_tools.replay.runner import run_replay_backup
from nga_tools.replay.corpus import load_replay_corpus
from nga_tools.replay.profile import ReplayProfile, TrafficProfile
from nga_tools.replay.server import create_replay_app
from nga_tools.replay.state import (
    REPLAY_TARGET_MARKER_FILENAME,
    PreparationStats,
    prepare_target_state,
    source_state_fingerprint,
)
from nga_tools.replay.validation import ValidationStats, validate_replay_output
from nga_tools.storage import ensure_storage_metadata

IMAGE_URL = (
    "https://img.nga.178.com/attachments/mon_202607/13/"
    "runner-test.png"
)
AUDIO_URL = (
    "https://img.nga.178.com/attachments/mon_202607/13/"
    "runner-test.mp3"
)


def _page() -> dict[str, object]:
    return {
        "code": 0,
        "currentPage": 1,
        "totalPage": 1,
        "vrows": 3,
        "result": [
            {
                "lou": 0,
                "pid": 100,
                "content": (
                    f"runner body [img]{IMAGE_URL}[/img]"
                    f'<audio src="{AUDIO_URL}"></audio>'
                ),
                "author": {"uid": 456, "username": "author"},
            },
            {
                "lou": 2,
                "pid": 102,
                "content": "runner tail",
                "author": {"uid": 456, "username": "author"},
            },
        ],
    }


def _build_warm_source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source-output"
    thread_dir = source / "123_456"
    store = ThreadArchiveStore(thread_dir)
    historical_page = _page()
    historical_result = historical_page["result"]
    assert isinstance(historical_result, list)
    historical_first = historical_result[0]
    assert isinstance(historical_first, dict)
    historical_first["content"] = "runner historical body"
    store.ingest.upsert_page(
        1,
        historical_page,
        observed_at="2026-07-12T00:00:00+00:00",
    )
    store.ingest.upsert_page(1, _page(), observed_at="2026-07-13T00:00:00+00:00")
    store.floor_maps.replace_floor_map(
        StoredFloorMap(
            version=FLOOR_MAP_VERSION,
            generation_version=FLOOR_MAP_GENERATION_VERSION,
            algorithm=FLOOR_MAP_HASH_ALGORITHM,
            tid=123,
            aid=456,
            input_signature="runner-floor-map",
            entries=[
                {"pid": 100, "author_lou": 0, "original_lou": 0},
                {
                    "pid": None,
                    "author_lou": 1,
                    "original_lou": None,
                    "candidate_original_lous": [1, 2],
                },
                {"pid": 102, "author_lou": 2, "original_lou": 3},
            ],
        )
    )
    store.state.ensure_schema()
    store.cache.ensure_schema()
    with closing(sqlite3.connect(thread_dir / "archive.sqlite3")) as connection:
        version_id = connection.execute(
            """
            SELECT id
            FROM post_versions
            WHERE source_hash = ?
            """,
            (hash_text("runner historical body"),),
        ).fetchone()
    assert version_id is not None
    store.posts.upsert_post_version_selection(0, version_id[0])
    (thread_dir / "warnings.log").write_text("do not copy", encoding="utf-8")
    (thread_dir / "pdf").mkdir()
    (thread_dir / "pdf" / "old.pdf").write_bytes(b"pdf")
    (thread_dir / "debug_json").mkdir()
    (thread_dir / "debug_json" / "page_1.json").write_text(
        "{}", encoding="utf-8"
    )

    images_dir = source / "images_unique"
    images_dir.mkdir(parents=True)
    image_buffer = io.BytesIO()
    Image.new("RGB", (1, 1), color=(255, 0, 0)).save(image_buffer, format="PNG")
    image_bytes = image_buffer.getvalue()
    image_path = images_dir / f"{hashlib.sha256(image_bytes).hexdigest()}.png"
    image_path.write_bytes(image_bytes)
    with sqlite3.connect(source / "image_index.sqlite3") as connection:
        ensure_storage_metadata(connection, role="image_index")
        connection.executescript(
            """
            CREATE TABLE image_mappings (
                url TEXT PRIMARY KEY,
                unique_rel_path TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO image_mappings VALUES (?, ?)",
            (IMAGE_URL, f"images_unique/{image_path.name}"),
        )
        connection.commit()
    with sqlite3.connect(source / IMAGE_CACHE_FILENAME) as connection:
        ensure_storage_metadata(connection, role="image_cache")
        connection.executescript(
            """
            CREATE TABLE image_validation_cache (
                relative_path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                valid INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        stat_result = image_path.stat()
        connection.execute(
            "INSERT INTO image_validation_cache VALUES (?, ?, ?, 1, '')",
            (
                f"images_unique/{image_path.name}",
                stat_result.st_size,
                stat_result.st_mtime_ns,
            ),
        )
        connection.commit()

    audio_content = (b"\xff\xfb\x90\x64" + bytes(413)) * 10
    audio_hash = hashlib.sha256(audio_content).hexdigest()
    audio_dir = source / "audio_unique"
    audio_dir.mkdir()
    audio_path = audio_dir / f"{audio_hash}.mp3"
    audio_path.write_bytes(audio_content)
    with sqlite3.connect(source / "audio_index.sqlite3") as connection:
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
        connection.execute(
            "INSERT INTO audio_mappings VALUES (?, ?, ?, ?, ?, '', '')",
            (
                AUDIO_URL,
                f"audio_unique/{audio_path.name}",
                audio_hash,
                len(audio_content),
                0.25,
            ),
        )
        connection.commit()

    thread_config = tmp_path / "thread_configs.json"
    thread_config.write_text(
        json.dumps(
            {
                "ThreadList": [
                    {
                        "thread_name": "sample",
                        "tid": 123,
                        "aid": 456,
                        "replies": 3,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    return source, thread_config


def _write_replay_profile(tmp_path: Path) -> Path:
    profile_path = tmp_path / "replay_profile.json"
    profile_path.write_text(
        json.dumps(
            {
                "api": {
                    "latency_ms": 0,
                    "bandwidth_bytes_per_second": 0,
                    "max_inflight": 4,
                },
                "image": {
                    "latency_ms": 0,
                    "bandwidth_bytes_per_second": 0,
                    "max_inflight": 4,
                },
                "chunk_bytes": 4096,
            }
        ),
        encoding="utf-8",
    )
    return profile_path


@contextmanager
def _live_replay_server(source: Path, thread_config: Path) -> Generator[str]:
    profile = ReplayProfile(
        api=TrafficProfile(0, 0, 4),
        image=TrafficProfile(0, 0, 4),
        chunk_bytes=4096,
    )
    app = create_replay_app(
        load_replay_corpus(source, thread_config),
        profile,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="error", lifespan="off")
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        raise RuntimeError("测试重放服务启动失败。")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


class ReplayStateTest:
    def test_zero_length_wal_does_not_change_source_fingerprint(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        fingerprint = source_state_fingerprint(source)
        Path(f"{source / '123_456' / 'archive.sqlite3'}-wal").write_bytes(b"")
        assert source_state_fingerprint(source) == fingerprint

    def test_warm_uses_database_backups_and_copies_only_semantic_state(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "target-output"
        fingerprint_before = source_state_fingerprint(source)
        source_mtimes = {
            path: path.stat().st_mtime_ns
            for path in source.rglob("*")
            if path.is_file()
            and not path.name.endswith(("-wal", "-shm"))
        }

        stats = prepare_target_state("warm", source, target)

        assert stats.sqlite_database_count == 6
        assert stats.selection_file_count == 0
        assert stats.image_file_count == 1
        assert stats.audio_file_count == 1
        assert stats.validation_cache_path_updates == 0
        assert (target / REPLAY_TARGET_MARKER_FILENAME).is_file()
        assert source_state_fingerprint(source) == fingerprint_before
        assert {
            path: path.stat().st_mtime_ns for path in source_mtimes
        } == source_mtimes
        assert (target / "123_456" / "archive.sqlite3").is_file()
        assert (target / "123_456" / "archive_state.sqlite3").is_file()
        assert (target / "123_456" / "archive_cache.sqlite3").is_file()
        with sqlite3.connect(
            target / "123_456" / "archive.sqlite3"
        ) as connection:
            selection_rows = connection.execute(
                """
                SELECT selections.lou, versions.content
                FROM post_version_selections AS selections
                JOIN post_versions AS versions
                    ON versions.id = selections.version_id
                """
            ).fetchall()
        assert [
            (lou, decode_content(content))
            for lou, content in selection_rows
        ] == [(0, "runner historical body")]
        assert not (
            target / "123_456" / "post_version_overrides.json"
        ).exists()
        assert not (target / "123_456" / "warnings.log").exists()
        assert not (target / "123_456" / "pdf").exists()
        assert not (target / "123_456" / "debug_json").exists()
        source_image = next((source / "images_unique").iterdir())
        target_image = target / "images_unique" / source_image.name
        assert target_image.read_bytes() == source_image.read_bytes()
        with sqlite3.connect(target / IMAGE_CACHE_FILENAME) as connection:
            cached_path = connection.execute(
                "SELECT relative_path FROM image_validation_cache"
            ).fetchone()[0]
        assert cached_path == f"images_unique/{target_image.name}"

        validation = validate_replay_output(
            source,
            target,
            [{"thread_name": "sample", "tid": 123, "aid": 456}],
            "warm",
        )
        assert validation.checked_archive_count == 1
        assert validation.compared_archive_count == 1
        assert validation.image_mapping_mismatch_count == 0

    def test_validation_checks_all_floor_map_fields(self, tmp_path: Path) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "target-output"
        prepare_target_state("warm", source, target)
        with sqlite3.connect(target / "123_456" / "archive.sqlite3") as connection:
            connection.execute(
                "UPDATE floor_map_entries SET original_pid = 999 WHERE author_lou = 0"
            )
            connection.commit()

        with pytest.raises(RuntimeError, match="楼层映射"):
            validate_replay_output(
                source,
                target,
                [{"thread_name": "sample", "tid": 123, "aid": 456}],
                "warm",
            )

    def test_validation_checks_latest_post_metadata(self, tmp_path: Path) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "target-output"
        prepare_target_state("warm", source, target)
        with sqlite3.connect(target / "123_456" / "archive.sqlite3") as connection:
            connection.execute(
                "UPDATE post_latest_metadata SET author_name = 'changed'"
            )
            connection.commit()

        with pytest.raises(RuntimeError, match="正文或元数据"):
            validate_replay_output(
                source,
                target,
                [{"thread_name": "sample", "tid": 123, "aid": 456}],
                "warm",
            )

    def test_validation_ignores_latest_post_attachment_metadata(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "target-output"
        prepare_target_state("warm", source, target)
        with sqlite3.connect(target / "123_456" / "archive.sqlite3") as connection:
            connection.execute(
                "UPDATE post_latest_metadata SET image_attachments_json = 'changed'"
            )
            connection.commit()

        validation = validate_replay_output(
            source,
            target,
            [{"thread_name": "sample", "tid": 123, "aid": 456}],
            "warm",
        )

        assert validation.checked_archive_count == 1
        assert validation.compared_archive_count == 1

    def test_initial_state_guards_nonempty_and_nested_targets(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        nonempty = tmp_path / "nonempty"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("keep", encoding="utf-8")

        with pytest.raises(ValueError, match="必须不存在或为空"):
            prepare_target_state("empty", source, nonempty)
        with pytest.raises(ValueError, match="已存在"):
            prepare_target_state("existing", source, tmp_path / "missing")
        with pytest.raises(ValueError, match="互相嵌套"):
            prepare_target_state("empty", source, source / "nested")

        historical = tmp_path / "historical-output"
        historical.mkdir()
        with pytest.raises(ValueError, match="归属标记"):
            prepare_target_state("existing", source, historical)

    def test_existing_accepts_replay_owned_target(self, tmp_path: Path) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "existing-output"
        prepare_target_state("empty", source, target)

        stats = prepare_target_state("existing", source, target)

        assert stats.initial_state == "existing"
        marker = json.loads(
            (target / REPLAY_TARGET_MARKER_FILENAME).read_text(encoding="utf-8")
        )
        assert marker["source_output"] == str(source.resolve())
        assert marker["created_initial_state"] == "empty"

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows replay暂时跳过链接隔离检查",
    )
    def test_existing_rejects_root_and_internal_symlinks(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "symlink-output"
        prepare_target_state("empty", source, target)
        alias = tmp_path / "symlink-output-alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"当前平台不能创建符号链接：{error}")

        with pytest.raises(ValueError, match="target-output不能是符号链接"):
            prepare_target_state("existing", source, alias)

        (target / "linked-thread").symlink_to(
            source / "123_456",
            target_is_directory=True,
        )
        with pytest.raises(ValueError, match="不能包含符号链接"):
            prepare_target_state("existing", source, target)

    @pytest.mark.parametrize(
        "relative_path",
        [
            Path("123_456/archive.sqlite3"),
            Path("image_index.sqlite3"),
            Path(IMAGE_CACHE_FILENAME),
            Path("images_unique") / "linked-image.png",
        ],
    )
    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows replay暂时跳过链接隔离检查",
    )
    def test_existing_rejects_source_hardlinks(
        self,
        tmp_path: Path,
        relative_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / f"hardlink-{relative_path.name}"
        prepare_target_state("empty", source, target)
        if relative_path.parts[0] == "images_unique":
            source_path = next((source / "images_unique").iterdir())
        else:
            source_path = source / relative_path
        target_path = target / relative_path
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source_path, target_path)
        except OSError as error:
            pytest.skip(f"当前平台不能创建硬链接：{error}")

        with pytest.raises(ValueError, match="共享同一inode"):
            prepare_target_state("existing", source, target)

    @pytest.mark.skipif(
        os.name == "nt",
        reason="此测试在POSIX上模拟Windows链接策略",
    )
    def test_windows_policy_checks_directory_and_marker_only(
        self,
        tmp_path: Path,
    ) -> None:
        source, _thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "windows-existing-output"
        prepare_target_state("empty", source, target)
        marker_path = target / REPLAY_TARGET_MARKER_FILENAME
        marker_target = target / "owned-marker.json"
        marker_path.replace(marker_target)
        marker_path.symlink_to(marker_target.name)
        hardlink_path = target / "linked-archive.sqlite3"
        os.link(source / "123_456" / "archive.sqlite3", hardlink_path)
        (target / "linked-thread").symlink_to(
            source / "123_456",
            target_is_directory=True,
        )
        alias = tmp_path / "windows-existing-alias"
        alias.symlink_to(target, target_is_directory=True)

        with patch(
            "nga_tools.replay.state._is_windows",
            return_value=True,
        ):
            direct_stats = prepare_target_state("existing", source, target)
            alias_stats = prepare_target_state("existing", source, alias)

            historical = tmp_path / "windows-historical-output"
            historical.mkdir()
            with pytest.raises(ValueError, match="归属标记"):
                prepare_target_state("existing", source, historical)

            file_target = tmp_path / "windows-file-target"
            file_target.write_text("not a directory", encoding="utf-8")
            with pytest.raises(ValueError, match="已存在"):
                prepare_target_state("existing", source, file_target)

        assert direct_stats.initial_state == "existing"
        assert alias_stats.initial_state == "existing"


class ReplayOfflineTest:
    def test_maps_assets_and_rejects_non_server_origins(self) -> None:
        with use_replay_network_policy("http://127.0.0.1:8765"):
            mapped = image_request_url(IMAGE_URL)
            assert mapped.startswith("http://127.0.0.1:8765/__replay__/image?")
            assert_replay_request_allowed(mapped)
            mapped_audio = audio_request_url(AUDIO_URL)
            assert mapped_audio.startswith(
                "http://127.0.0.1:8765/__replay__/audio?"
            )
            assert_replay_request_allowed(mapped_audio)
            with pytest.raises(ReplayOfflineError, match="离线保护拒绝"):
                assert_replay_request_allowed("https://bbs.nga.cn/app_api.php")
            session = create_api_session()
            try:
                assert session.trust_env is False
                assert "Cookie" not in session.headers
            finally:
                session.close()

    def test_image_redirect_cannot_escape_replay_origin(
        self,
        tmp_path: Path,
    ) -> None:
        class RedirectHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "https://example.com/escaped.png")
                self.end_headers()

            def log_message(self, _format: str, *args: object) -> None:
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        server_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with use_replay_network_policy(server_url):
                summary = download_files(
                    [
                        {
                            "url": IMAGE_URL,
                            "request_url": f"{server_url}/redirect",
                            "save_path": str(tmp_path / "redirected.png"),
                        }
                    ],
                    retries=0,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        assert not summary["succeeded"]
        assert summary["failed"][0]["url"] == IMAGE_URL
        assert summary["failed"][0]["failure_kind"] == "http_3xx"
        assert summary["failed"][0]["http_status"] == 302
        assert not (tmp_path / "redirected.png").exists()


class ReplayRunnerTest:
    def test_replay_test_cli_uses_an_automatic_port(self, tmp_path: Path) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        profile_path = _write_replay_profile(tmp_path)
        target = tmp_path / "test-output"
        args = args_parse(
            [
                "replay",
                "test",
                "--source-output",
                str(source),
                "--profile",
                str(profile_path),
                "--target-output",
                str(target),
                "--thread-config",
                str(thread_config),
                "--initial-state",
                "empty",
                "--all-threads",
            ]
        )

        assert args["command"] == "replay"
        assert args["action"] == "test"
        assert args["port"] is None

        with pytest.raises(SystemExit) as exc_info:
            args_parse(
                [
                    "replay",
                    "test",
                    "--server-url",
                    "http://127.0.0.1:8765",
                    "--source-output",
                    str(source),
                    "--profile",
                    str(profile_path),
                    "--target-output",
                    str(target),
                    "--thread-config",
                    str(thread_config),
                    "--initial-state",
                    "empty",
                    "--all-threads",
                ]
            )
        assert exc_info.value.code == 2

    def test_replay_test_rejects_an_out_of_range_port(self, tmp_path: Path) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        profile_path = _write_replay_profile(tmp_path)
        with pytest.raises(ValueError, match="1到65535"):
            run_replay_test(
                {
                    "source_output": str(source),
                    "profile": str(profile_path),
                    "target_output": str(tmp_path / "test-output"),
                    "thread_config": str(thread_config),
                    "initial_state": "empty",
                    "all_threads": True,
                    "port": 65536,
                }
            )

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows的SO_REUSEADDR端口占用语义不同",
    )
    def test_replay_test_does_not_start_runner_when_port_is_busy(
        self,
        tmp_path: Path,
    ) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        profile_path = _write_replay_profile(tmp_path)
        target = tmp_path / "test-output"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            busy_port = listener.getsockname()[1]
            with pytest.raises(RuntimeError, match="重放服务启动失败"):
                run_replay_test(
                    {
                        "source_output": str(source),
                        "profile": str(profile_path),
                        "target_output": str(target),
                        "thread_config": str(thread_config),
                        "initial_state": "empty",
                        "all_threads": True,
                        "port": busy_port,
                    }
                )

        assert not target.exists()

    def test_replay_test_starts_and_stops_both_processes(
        self,
        tmp_path: Path,
    ) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        profile_path = _write_replay_profile(tmp_path)
        target = tmp_path / "test-output"
        fingerprint_before = source_state_fingerprint(source)

        dispatch_command(
            args_parse(
                [
                    "replay",
                    "test",
                    "--source-output",
                    str(source),
                    "--profile",
                    str(profile_path),
                    "--target-output",
                    str(target),
                    "--thread-config",
                    str(thread_config),
                    "--initial-state",
                    "empty",
                    "--all-threads",
                    "--workers",
                    "1",
                    "--api-concurrency",
                    "1",
                    "--image-concurrency",
                    "1",
                ]
            )
        )

        reports = list(target.glob("replay_run-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        assert report["status"] == "completed"
        assert report["initial_state"] == "empty"
        assert source_state_fingerprint(source) == fingerprint_before

        parsed_url = urlparse(report["server_url"])
        assert parsed_url.hostname == "127.0.0.1"
        assert parsed_url.port is not None
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", parsed_url.port))

    def test_replay_test_propagates_runner_failure_and_stops_service(
        self,
        tmp_path: Path,
    ) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        profile_path = _write_replay_profile(tmp_path)
        target = tmp_path / "test-output"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]

        with pytest.raises(SystemExit) as exc_info:
            run_replay_test(
                {
                    "source_output": str(source),
                    "profile": str(profile_path),
                    "target_output": str(target),
                    "thread_config": str(thread_config),
                    "initial_state": "empty",
                    "name": "missing-thread",
                    "port": port,
                }
            )

        assert exc_info.value.code == 1
        assert not target.exists()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

    @pytest.mark.skipif(
        os.name == "nt",
        reason="Windows replay暂时跳过链接隔离检查",
    )
    def test_runner_preserves_target_symlink_for_guard(self, tmp_path: Path) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "owned-target"
        prepare_target_state("empty", source, target)
        alias = tmp_path / "owned-target-alias"
        try:
            alias.symlink_to(target, target_is_directory=True)
        except OSError as error:
            pytest.skip(f"当前平台不能创建符号链接：{error}")

        with pytest.raises(ValueError, match="target-output不能是符号链接"):
            run_replay_backup(
                {
                    "server_url": "http://127.0.0.1:1",
                    "source_output": str(source),
                    "target_output": str(alias),
                    "thread_config": str(thread_config),
                    "initial_state": "existing",
                    "all_threads": True,
                }
            )

    def test_cli_and_runner_write_reproducible_report(self, tmp_path: Path) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        target = tmp_path / "run-output"
        server_url = "http://127.0.0.1:8765"
        args = args_parse(
            [
                "replay",
                "run",
                "--server-url",
                server_url,
                "--source-output",
                str(source),
                "--target-output",
                str(target),
                "--thread-config",
                str(thread_config),
                "--initial-state",
                "empty",
                "--all-threads",
                "--workers",
                "2",
            ]
        )
        manifest = {
            "corpus_id": "corpus-1",
            "profile_id": "profile-1",
            "profile": {"chunk_bytes": 4096},
            "source_output": str(source.resolve()),
            "thread_config": str(thread_config.resolve()),
        }
        metrics = {
            "reset_at": "2026-07-13T00:00:00+08:00",
            "api": {"requests": 1, "response_bytes": 100},
            "image": {"requests": 0, "response_bytes": 0},
        }

        def prepare(*_args: object, **_kwargs: object) -> PreparationStats:
            target.mkdir()
            return PreparationStats("empty", 0.01)

        with (
            patch(
                "nga_tools.replay.runner.ReplayServerClient.health",
                return_value={
                    "status": "ok",
                    "corpus_id": "corpus-1",
                    "profile_id": "profile-1",
                },
            ),
            patch(
                "nga_tools.replay.runner.ReplayServerClient.manifest",
                return_value=manifest,
            ),
            patch(
                "nga_tools.replay.runner.ReplayServerClient.reset",
                return_value={"status": "ok"},
            ),
            patch(
                "nga_tools.replay.runner.ReplayServerClient.metrics",
                side_effect=[
                    {
                        "reset_at": "2026-07-13T00:00:00+08:00",
                        "api": {"requests": 0},
                        "image": {"requests": 0},
                    },
                    metrics,
                ],
            ),
            patch("nga_tools.replay.runner.prepare_target_state", side_effect=prepare),
            patch(
                "nga_tools.replay.runner.run_backup_fetch_batch",
                return_value=ThreadBatchResult((), (), ()),
            ) as batch_mock,
            patch(
                "nga_tools.replay.runner.validate_replay_output",
                return_value=ValidationStats(0.02, 1, 1, 1, 0, 0, 0),
            ),
            patch(
                "nga_tools.replay.runner.client_runtime_metrics",
                return_value={
                    "api": {"capacity": 4},
                    "image": {"capacity": 16},
                    "image_store": {"store_attempts": 3},
                    "image_index_writer": {"transactions": 2},
                },
            ),
        ):
            run_replay_backup(args)

        batch_mock.assert_called_once()
        reports = list(target.glob("replay_run-*.json"))
        assert len(reports) == 1
        report = json.loads(reports[0].read_text(encoding="utf-8"))
        assert report["format_version"] == 2
        assert report["corpus_id"] == "corpus-1"
        assert report["profile_hash"] == "profile-1"
        assert report["initial_state"] == "empty"
        assert report["concurrency"]["workers"] == 2
        assert report["server_metrics"] == metrics
        assert report["client_runtime_metrics"] == {
            "api": {"capacity": 4},
            "image": {"capacity": 16},
            "image_store": {"store_attempts": 3},
            "image_index_writer": {"transactions": 2},
        }
        assert report["thread_batch_metrics"] == {
            "peak_unstarted_configs": 0,
            "unstarted_config_seconds": 0.0,
            "max_config_start_wait_seconds": 0.0,
        }

    def test_empty_existing_and_warm_run_end_to_end(self, tmp_path: Path) -> None:
        source, thread_config = _build_warm_source(tmp_path)
        tracked_source_paths = [
            source / "123_456" / "archive.sqlite3",
            source / "image_index.sqlite3",
            source / IMAGE_CACHE_FILENAME,
            next((source / "images_unique").iterdir()),
            source / "audio_index.sqlite3",
            next((source / "audio_unique").iterdir()),
        ]
        source_states = {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in tracked_source_paths
        }

        def run(target: Path, initial_state: str, server_url: str) -> None:
            run_replay_backup(
                {
                    "server_url": server_url,
                    "source_output": str(source),
                    "target_output": str(target),
                    "thread_config": str(thread_config),
                    "initial_state": initial_state,
                    "all_threads": True,
                    "workers": 1,
                    "api_concurrency": 1,
                    "image_concurrency": 1,
                }
            )

        empty_target = tmp_path / "empty-run"
        warm_target = tmp_path / "warm-run"
        with _live_replay_server(source, thread_config) as server_url:
            run(empty_target, "empty", server_url)
            run(empty_target, "existing", server_url)
            run(empty_target, "existing", server_url)
            run(warm_target, "warm", server_url)

        empty_reports = sorted(empty_target.glob("replay_run-*.json"))
        assert len(empty_reports) == 3
        assert len(list(warm_target.glob("replay_run-*.json"))) == 1
        first_existing = json.loads(empty_reports[-2].read_text(encoding="utf-8"))
        second_existing = json.loads(empty_reports[-1].read_text(encoding="utf-8"))
        for traffic_kind in ("api", "image", "audio"):
            first_traffic = first_existing["server_metrics"][traffic_kind]
            second_traffic = second_existing["server_metrics"][traffic_kind]
            for metric in (
                "requests",
                "response_bytes",
                "floor_map_original_requests",
                "statuses",
            ):
                assert first_traffic[metric] == second_traffic[metric]
        assert {
            path: (path.read_bytes(), path.stat().st_mtime_ns)
            for path in tracked_source_paths
        } == source_states
        with sqlite3.connect(warm_target / "123_456" / "archive.sqlite3") as connection:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

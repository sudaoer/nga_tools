from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup import audio_store
from nga_tools.core.nga_audio import (
    extract_nga_audio_urls,
    normalize_nga_audio_url,
)


def _audio_url(name: str, *, day: str = "15") -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202607/{day}/{name}.mp3"
    )


def _mp3_bytes(marker: int = 0) -> bytes:
    frame = b"\xff\xfb\x90\x64" + bytes([marker]) * 413
    return frame * 10


def test_extract_nga_audio_urls_normalizes_and_preserves_order() -> None:
    first = _audio_url("first")
    second = _audio_url("second")
    content = (
        f'<audio controls src="{first}?raw=1&amp;from=post"></audio>'
        f"<AUDIO SRC='{second}'></AUDIO>"
        '<audio src="https://example.com/not-nga.mp3"></audio>'
        '<audio src="javascript:alert(1)"></audio>'
    )

    assert extract_nga_audio_urls(content) == (
        f"{first}?raw=1&from=post",
        second,
    )


def test_normalize_nga_audio_url_rejects_noncanonical_sources() -> None:
    assert normalize_nga_audio_url(_audio_url("valid")) == _audio_url("valid")
    assert normalize_nga_audio_url(_audio_url("valid") + "#fragment") is None
    assert normalize_nga_audio_url(
        "http://img.nga.178.com/attachments/mon_202607/15/insecure.mp3"
    ) is None
    assert normalize_nga_audio_url(
        "https://img.nga.178.com/attachments/mon_202602/30/bad-date.mp3"
    ) is None
    assert normalize_nga_audio_url(
        "https://img.nga.178.com/attachments/mon_202607/15/not-audio.png"
    ) is None


def test_download_audio_tasks_deduplicates_identical_content(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    first_url = _audio_url("first")
    second_url = _audio_url("second")
    content = _mp3_bytes()
    content_hash = hashlib.sha256(content).hexdigest()

    def fake_download_files(
        tasks: list[audio_store.utils.DownloadTask],
        **kwargs: object,
    ) -> audio_store.utils.DownloadSummary:
        assert kwargs == {"resource_kind": "audio"}
        succeeded: list[audio_store.utils.DownloadFileResult] = []
        for task in tasks:
            path = Path(task["save_path"])
            path.write_bytes(content)
            succeeded.append(
                {
                    "url": task["url"],
                    "save_path": str(path),
                    "success": True,
                    "content_sha256": content_hash,
                    "content_bytes": len(content),
                }
            )
        return {"succeeded": succeeded, "failed": []}

    with patch(
        "nga_tools.backup.audio_store.utils.download_files",
        side_effect=fake_download_files,
    ):
        summary = audio_store.download_audio_tasks(
            [{"url": first_url}, {"url": second_url}],
            output_root=output_root,
        )

    assert len(summary["succeeded"]) == 2
    assert summary["failed"] == []
    mappings = audio_store.audio_mappings_for_urls(
        output_root,
        [first_url, second_url],
    )
    assert set(mappings) == {first_url, second_url}
    assert {mapping.unique_rel_path for mapping in mappings.values()} == {
        f"audio_unique/{content_hash}.mp3"
    }
    assert all(mapping.duration_seconds > 0 for mapping in mappings.values())
    assert list((output_root / "audio_unique").glob("*.mp3")) == [
        output_root / "audio_unique" / f"{content_hash}.mp3"
    ]

    with sqlite3.connect(output_root / audio_store.AUDIO_INDEX_FILENAME) as db:
        role = db.execute(
            "SELECT role FROM storage_metadata WHERE singleton = 1"
        ).fetchone()
    assert role == ("audio_index",)


def test_download_audio_tasks_rejects_invalid_payload(tmp_path: Path) -> None:
    output_root = tmp_path / "output"
    url = _audio_url("invalid")
    content = b"<html>not an mp3</html>"

    def fake_download_files(
        tasks: list[audio_store.utils.DownloadTask],
        **_kwargs: object,
    ) -> audio_store.utils.DownloadSummary:
        task = tasks[0]
        path = Path(task["save_path"])
        path.write_bytes(content)
        return {
            "succeeded": [
                {
                    "url": task["url"],
                    "save_path": str(path),
                    "success": True,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                    "content_bytes": len(content),
                }
            ],
            "failed": [],
        }

    with patch(
        "nga_tools.backup.audio_store.utils.download_files",
        side_effect=fake_download_files,
    ):
        summary = audio_store.download_audio_tasks(
            [{"url": url}],
            output_root=output_root,
        )

    assert summary["succeeded"] == []
    assert summary["failed"][0]["failure_kind"] == "audio_validation"
    assert audio_store.audio_mappings_for_urls(output_root, [url]) == {}
    assert not list((output_root / "audio_unique").glob("*.mp3"))


def test_audio_mapping_lookup_rejects_missing_or_corrupt_file(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    url = _audio_url("strict-local")
    source_path = tmp_path / "source.mp3"
    content = _mp3_bytes()
    source_path.write_bytes(content)
    result: audio_store.utils.DownloadFileResult = {
        "url": url,
        "save_path": str(source_path),
        "success": True,
        "content_sha256": hashlib.sha256(content).hexdigest(),
        "content_bytes": len(content),
    }
    stored = audio_store.store_downloaded_audio(
        source_path,
        url,
        result,
        output_root=output_root,
    )
    audio_store._upsert_audio_mappings(output_root, [stored.mapping])
    stored_path = stored.mapping.path(output_root)

    assert audio_store.audio_mappings_for_urls(output_root, [url]) == {
        url: stored.mapping
    }
    stored_path.write_bytes(_mp3_bytes(marker=1))
    assert stored_path.stat().st_size == len(content)
    assert audio_store.audio_mappings_for_urls(output_root, [url]) == {}
    stored_path.unlink()
    assert audio_store.audio_mappings_for_urls(output_root, [url]) == {}

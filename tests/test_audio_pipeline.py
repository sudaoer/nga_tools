from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.audio_pipeline import maintain_archived_audio
from nga_tools.backup.audio_store import AudioDownloadTask
from nga_tools.core.downloads import DownloadSummary


def _audio_url(name: str) -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202607/15/{name}.mp3"
    )


def _page(content: str, *, pid: int = 1001) -> dict[str, object]:
    return {
        "currentPage": 1,
        "totalPage": 1,
        "result": [{"lou": 1, "pid": pid, "content": content}],
    }


def _successful_downloads(
    tasks: list[AudioDownloadTask],
    **_kwargs: object,
) -> DownloadSummary:
    return {
        "succeeded": [
            {
                "url": task["url"],
                "save_path": f"/audio/{index}.mp3",
                "success": True,
            }
            for index, task in enumerate(tasks)
        ],
        "failed": [],
    }


def test_audio_pipeline_scans_all_versions_then_only_new_versions(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    store = ThreadArchiveStore(output_root / "123_456")
    first_url = _audio_url("first")
    second_url = _audio_url("second")
    third_url = _audio_url("third")
    store.upsert_page(
        1,
        _page(f'<audio src="{first_url}"></audio>'),
        observed_at="2026-07-15T00:00:00+00:00",
    )
    store.upsert_page(
        1,
        _page(f'<audio src="{second_url}"></audio>'),
        observed_at="2026-07-15T01:00:00+00:00",
    )

    with patch(
        "nga_tools.backup.audio_pipeline.download_audio_tasks",
        side_effect=_successful_downloads,
    ) as download_mock:
        first_result = maintain_archived_audio(123, 456, store, force=False)

    assert first_result.full_scan
    assert first_result.scanned_post_versions == 2
    assert first_result.discovered_urls == 2
    assert {task["url"] for task in download_mock.call_args.args[0]} == {
        first_url,
        second_url,
    }
    first_snapshot = store.read_backup_processing_snapshot()
    assert first_snapshot.audio_state is not None
    assert (
        first_snapshot.audio_state.processed_max_post_version_id
        == store.max_post_version_id()
    )

    store.upsert_page(
        1,
        _page(f'<audio src="{third_url}"></audio>'),
        observed_at="2026-07-15T02:00:00+00:00",
    )
    with patch(
        "nga_tools.backup.audio_pipeline.download_audio_tasks",
        side_effect=_successful_downloads,
    ) as download_mock:
        second_result = maintain_archived_audio(123, 456, store, force=False)

    assert not second_result.full_scan
    assert second_result.scanned_post_versions == 1
    assert download_mock.call_args.args[0] == [{"url": third_url}]


def test_audio_pipeline_persists_failure_and_force_retries_it(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "output"
    store = ThreadArchiveStore(output_root / "123_456")
    url = _audio_url("missing")
    store.upsert_page(
        1,
        _page(f'<audio src="{url}"></audio>'),
        observed_at="2026-07-15T00:00:00+00:00",
    )

    failed: DownloadSummary = {
        "succeeded": [],
        "failed": [
            {
                "url": url,
                "save_path": "",
                "success": False,
                "failure_kind": "http_4xx",
                "http_status": 404,
            }
        ],
    }
    with patch(
        "nga_tools.backup.audio_pipeline.download_audio_tasks",
        return_value=failed,
    ):
        maintain_archived_audio(123, 456, store, force=False)

    snapshot = store.read_backup_processing_snapshot()
    assert len(snapshot.pending_audio_retries) == 1
    retry = snapshot.pending_audio_retries[0]
    assert retry.url == url
    assert retry.failure_kind == "http_4xx"
    assert retry.http_status == 404
    assert retry.last_attempt_at is not None
    assert retry.last_attempt_at.tzinfo is not None

    with (
        patch(
            "nga_tools.backup.audio_pipeline.datetime.datetime"
        ) as datetime_mock,
        patch(
            "nga_tools.backup.audio_pipeline.download_audio_tasks",
            side_effect=_successful_downloads,
        ) as download_mock,
    ):
        datetime_mock.now.return_value = datetime(
            2026,
            7,
            15,
            tzinfo=timezone.utc,
        )
        maintain_archived_audio(123, 456, store, force=True)

    assert download_mock.call_args.args[0] == [{"url": url}]
    assert store.read_backup_processing_snapshot().pending_audio_retries == ()

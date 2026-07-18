from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from nga_tools.backup.image_retry import (
    pending_image_retries_after_attempt,
    select_image_retries,
    uses_probabilistic_backoff,
)
from nga_tools.backup.processing_state import PendingImageRetry
from nga_tools.core.downloads import DownloadFailureKind


_NOW = datetime(2026, 7, 14, tzinfo=timezone.utc)


def _retry(
    url: str,
    *,
    failure_kind: DownloadFailureKind = "http_4xx",
    http_status: int | None = 404,
    last_attempt_at: datetime | None = _NOW,
) -> PendingImageRetry:
    return PendingImageRetry(
        url=url,
        last_attempt_at=last_attempt_at,
        failure_kind=failure_kind if last_attempt_at is not None else None,
        http_status=http_status if last_attempt_at is not None else None,
    )


@pytest.mark.parametrize(
    "retry",
    [
        _retry(
            "https://example.invalid/redirect",
            failure_kind="http_3xx",
            http_status=302,
        ),
        _retry("https://example.invalid/missing", http_status=404),
        _retry("https://example.invalid/gone", http_status=410),
    ],
)
def test_persistent_http_failures_use_probabilistic_backoff(
    retry: PendingImageRetry,
) -> None:
    assert uses_probabilistic_backoff(retry)


@pytest.mark.parametrize(
    "retry",
    [
        _retry("https://example.invalid/rate-limit", http_status=429),
        _retry(
            "https://example.invalid/server",
            failure_kind="http_5xx",
            http_status=503,
        ),
        _retry(
            "https://example.invalid/timeout",
            failure_kind="timeout",
            http_status=None,
        ),
        _retry(
            "https://example.invalid/store",
            failure_kind="image_store",
            http_status=None,
        ),
        _retry("https://example.invalid/legacy", last_attempt_at=None),
    ],
)
def test_transient_and_legacy_failures_do_not_use_probabilistic_backoff(
    retry: PendingImageRetry,
) -> None:
    assert not uses_probabilistic_backoff(retry)


def test_recent_persistent_failure_is_deferred_but_transient_is_due() -> None:
    persistent = _retry("https://example.invalid/missing")
    transient = _retry(
        "https://example.invalid/timeout",
        failure_kind="timeout",
        http_status=None,
    )

    selection = select_image_retries(
        (persistent, transient),
        thread_target_key="123:456",
        now=_NOW,
        max_interval=timedelta(hours=168),
        force=False,
    )

    assert selection.due == (transient,)
    assert selection.deferred == (persistent,)


def test_many_same_age_persistent_failures_share_one_thread_gate() -> None:
    retries = tuple(
        _retry(f"https://example.invalid/missing-{index}")
        for index in range(20)
    )

    selection = select_image_retries(
        retries,
        thread_target_key="123:456",
        now=_NOW,
        max_interval=timedelta(hours=168),
        force=False,
    )

    assert selection.due == ()
    assert selection.deferred == retries


def test_shared_thread_gate_keeps_individual_age_thresholds() -> None:
    retries = (
        _retry(
            "https://example.invalid/older",
            last_attempt_at=_NOW - timedelta(days=4),
        ),
        _retry(
            "https://example.invalid/newer",
            last_attempt_at=_NOW - timedelta(days=2),
        ),
    )

    from unittest.mock import patch

    with patch(
        "nga_tools.backup.image_retry.shared_media_retry_ticket",
        return_value=0.1,
    ):
        selection = select_image_retries(
            retries,
            thread_target_key="123:456",
            now=_NOW,
            max_interval=timedelta(days=7),
            force=False,
        )

    assert selection.due == (retries[0],)
    assert selection.deferred == (retries[1],)


def test_deadline_and_force_select_persistent_failures() -> None:
    old_retry = _retry(
        "https://example.invalid/old",
        last_attempt_at=_NOW - timedelta(hours=168),
    )
    recent_retry = _retry("https://example.invalid/recent")

    deadline_selection = select_image_retries(
        (old_retry,),
        thread_target_key="123:456",
        now=_NOW,
        max_interval=timedelta(hours=168),
        force=False,
    )
    forced_selection = select_image_retries(
        (recent_retry,),
        thread_target_key="123:456",
        now=_NOW,
        max_interval=timedelta(hours=168),
        force=True,
    )

    assert deadline_selection.due == (old_retry,)
    assert deadline_selection.deferred == ()
    assert forced_selection.due == (recent_retry,)
    assert forced_selection.deferred == ()


def test_attempt_result_preserves_deferred_and_replaces_failed_metadata() -> None:
    deferred = _retry("https://example.invalid/deferred")

    retries = pending_image_retries_after_attempt(
        (deferred,),
        [
            {
                "url": "https://example.invalid/attempted",
                "save_path": "",
                "success": False,
                "failure_kind": "http_4xx",
                "http_status": 410,
            }
        ],
        attempted_at=_NOW + timedelta(hours=1),
    )

    assert retries == (
        PendingImageRetry(
            url="https://example.invalid/attempted",
            last_attempt_at=_NOW + timedelta(hours=1),
            failure_kind="http_4xx",
            http_status=410,
        ),
        deferred,
    )


def test_attempt_result_rejects_deferred_download_and_naive_time() -> None:
    deferred = _retry("https://example.invalid/deferred")
    failed = [
        {
            "url": deferred.url,
            "save_path": "",
            "success": False,
        }
    ]
    with pytest.raises(ValueError, match="延后图片"):
        pending_image_retries_after_attempt(
            (deferred,),
            failed,
            attempted_at=_NOW,
        )
    with pytest.raises(ValueError, match="包含时区"):
        pending_image_retries_after_attempt(
            (),
            (),
            attempted_at=datetime(2026, 7, 14),
        )

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from nga_tools.backup import image_store
from nga_tools.backup.image_pipeline import download_images
from nga_tools.backup.image_validation import (
    ImageValidationCache,
    canonical_image_path_key,
)
from nga_tools.core.image_formats import image_file_is_valid
from nga_tools.timing import use_timing_log


def _image_url(name: str) -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202607/11/{name}.png"
    )


def test_preparation_deep_validates_alias_mappings_once(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    image_path = output_dir / "images_unique" / "shared.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)
    first_url = _image_url("first")
    second_url = _image_url("second")

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_store.upsert_image_mappings(
            [(first_url, image_path), (second_url, image_path)]
        )
        with (
            patch(
                "nga_tools.backup.image_validation.image_file_is_valid",
                wraps=image_file_is_valid,
            ) as validation_mock,
            patch(
                "nga_tools.backup.image_store.canonical_image_path_key",
                wraps=canonical_image_path_key,
            ) as canonical_path_mock,
        ):
            preparation = image_store.prepare_image_download_tasks(
                [{"url": first_url}, {"url": second_url}]
            )

    assert preparation.pending_tasks == []
    assert validation_mock.call_count == 1
    assert canonical_path_mock.call_count == 1
    assert preparation.stats.mapping_hit_url_count == 2
    assert preparation.stats.unique_physical_path_count == 1
    assert preparation.stats.intra_thread_path_dedup_count == 1
    assert preparation.stats.deep_validation_path_count == 1


def test_validation_cache_single_flights_concurrent_calls(tmp_path: Path) -> None:
    image_path = tmp_path / "shared.png"
    Image.new("RGB", (1, 1), color="white").save(image_path)
    cache = ImageValidationCache()
    validation_entered = threading.Event()
    release_validation = threading.Event()

    def slow_validation(path: Path) -> bool:
        validation_entered.set()
        assert release_validation.wait(timeout=2)
        return image_file_is_valid(path)

    with (
        patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            side_effect=slow_validation,
        ) as validation_mock,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(cache.validate, image_path)
        assert validation_entered.wait(timeout=2)
        second = executor.submit(cache.validate, image_path)
        release_validation.set()
        outcomes = [first.result(timeout=2), second.result(timeout=2)]

    assert validation_mock.call_count == 1
    assert all(outcome.valid for outcome in outcomes)
    assert sum(outcome.source == "deep" for outcome in outcomes) == 1
    assert sum(outcome.source == "memory" for outcome in outcomes) == 1


def test_validation_cache_revalidates_changed_file(tmp_path: Path) -> None:
    image_path = tmp_path / "changed.png"
    Image.new("RGB", (1, 1), color="white").save(image_path)
    cache = ImageValidationCache()

    with patch(
        "nga_tools.backup.image_validation.image_file_is_valid",
        wraps=image_file_is_valid,
    ) as validation_mock:
        first = cache.validate(image_path)
        image_path.write_bytes(b"not an image anymore")
        second = cache.validate(image_path)

    assert first.valid
    assert not second.valid
    assert second.source == "deep"
    assert validation_mock.call_count == 2


def test_validation_cache_invalidation_forces_revalidation(tmp_path: Path) -> None:
    image_path = tmp_path / "invalidated.png"
    Image.new("RGB", (1, 1), color="white").save(image_path)
    cache = ImageValidationCache()

    with patch(
        "nga_tools.backup.image_validation.image_file_is_valid",
        wraps=image_file_is_valid,
    ) as validation_mock:
        assert cache.validate(image_path).valid
        assert cache.validate(image_path).source == "memory"
        cache.invalidate(image_path)
        after_invalidation = cache.validate(image_path)

    assert after_invalidation.source == "deep"
    assert validation_mock.call_count == 2


def test_validation_cache_does_not_cache_missing_path(tmp_path: Path) -> None:
    image_path = tmp_path / "later.png"
    cache = ImageValidationCache()

    with patch(
        "nga_tools.backup.image_validation.image_file_is_valid",
        wraps=image_file_is_valid,
    ) as validation_mock:
        assert not cache.validate(image_path).valid
        assert not cache.validate(image_path).valid
        Image.new("RGB", (1, 1), color="white").save(image_path)
        created = cache.validate(image_path)

    assert created.valid
    assert created.source == "deep"
    assert validation_mock.call_count == 1


def test_persistent_preload_queries_only_unseen_paths(tmp_path: Path) -> None:
    first_path = tmp_path / "first.png"
    second_path = tmp_path / "second.png"
    third_path = tmp_path / "third.png"
    cache = ImageValidationCache()

    with patch(
        "nga_tools.backup.image_store.load_persistent_validation_cache",
        return_value={},
    ) as load_mock:
        first_query_count = cache.preload({first_path, second_path})
        second_query_count = cache.preload({second_path, third_path})
        third_query_count = cache.preload({first_path, third_path})

    assert first_query_count == 2
    assert second_query_count == 1
    assert third_query_count == 0
    queried_path_sets = [call.args[0] for call in load_mock.call_args_list]
    assert queried_path_sets == [
        {
            canonical_image_path_key(first_path),
            canonical_image_path_key(second_path),
        },
        {canonical_image_path_key(third_path)},
    ]


def test_persistent_preload_single_flights_concurrent_paths(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "shared.png"
    cache = ImageValidationCache()
    load_entered = threading.Event()
    release_load = threading.Event()

    def slow_load(
        canonical_paths: set[str],
    ) -> dict[str, tuple[int, int, bool]]:
        assert canonical_paths == {canonical_image_path_key(image_path)}
        load_entered.set()
        assert release_load.wait(timeout=2)
        return {}

    with (
        patch(
            "nga_tools.backup.image_store.load_persistent_validation_cache",
            side_effect=slow_load,
        ) as load_mock,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(cache.preload, {image_path})
        assert load_entered.wait(timeout=2)
        second = executor.submit(cache.preload, {image_path})
        release_load.set()
        query_counts = [first.result(timeout=2), second.result(timeout=2)]

    assert load_mock.call_count == 1
    assert sorted(query_counts) == [0, 1]


def test_image_preparation_writes_subphases_and_metrics(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    image_path = output_dir / "images_unique" / "timed.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)
    first_url = _image_url("timed-first")
    second_url = _image_url("timed-second")
    timing_path = tmp_path / "timing.log"

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_store.upsert_image_mappings(
            [(first_url, image_path), (second_url, image_path)]
        )
        with use_timing_log(timing_path, task_name="image preparation"):
            download_images(
                123,
                None,
                [{"url": first_url}, {"url": second_url}],
            )

    timing_text = timing_path.read_text(encoding="utf-8")
    for stage_name in (
        "图片下载准备",
        "图片索引批量查询",
        "图片缓存文件校验",
    ):
        assert f"阶段：{stage_name}，开始时间：" in timing_text
        assert f"阶段：{stage_name}，结束时间：" in timing_text
    assert "指标：图片任务URL数，值：2\n" in timing_text
    assert "指标：图片索引命中URL数，值：2\n" in timing_text
    assert "指标：图片唯一物理路径数，值：1\n" in timing_text
    assert "指标：图片线程内路径去重数，值：1\n" in timing_text
    assert "指标：图片深度校验路径数，值：1\n" in timing_text
    assert "指标：图片待下载URL数，值：0\n" in timing_text
    assert "指标：图片持久化缓存命中路径数，值：0\n" in timing_text
    assert "指标：图片持久化缓存查询路径数，值：1\n" in timing_text
    assert "指标：图片内存缓存命中路径数，值：0\n" in timing_text
    assert "指标：图片校验文件缺失路径数，值：0\n" in timing_text


def test_image_pipeline_reports_one_detailed_final_failure(tmp_path: Path) -> None:
    url = _image_url("missing")
    stats = image_store.ImagePreparationStats(
        task_url_count=1,
        mapping_hit_url_count=0,
        unique_physical_path_count=0,
        intra_thread_path_dedup_count=0,
        memory_cache_hit_path_count=0,
        deep_validation_path_count=0,
        persistent_cache_hit_path_count=0,
        missing_validation_path_count=0,
        persistent_cache_query_path_count=0,
        invalid_mapping_count=0,
        pending_download_url_count=1,
    )
    timing_path = tmp_path / "timing.log"
    failure: image_store.utils.DownloadFileResult = {
        "url": url,
        "save_path": str(tmp_path / "missing.png"),
        "success": False,
        "error": f"404, message='HTTP 404', url='{url}'",
        "failure_kind": "http_4xx",
        "http_status": 404,
    }

    with (
        patch(
            "nga_tools.backup.image_pipeline.image_store.prepare_image_download_tasks",
            return_value=image_store.ImageDownloadPreparation(
                pending_tasks=[{"url": url}],
                stats=stats,
            ),
        ),
        patch(
            "nga_tools.backup.image_pipeline.image_store.download_image_tasks",
            return_value={"succeeded": [], "failed": [failure]},
        ),
        patch("nga_tools.backup.image_pipeline.report_warning") as warning_mock,
        use_timing_log(timing_path, task_name="image failure"),
    ):
        result = download_images(123, None, [{"url": url}])

    assert result["failed"] == [failure]
    warning_mock.assert_called_once()
    warning_text = warning_mock.call_args.args[0]
    assert warning_text.count(url) == 1
    assert "http_4xx" in warning_text
    assert "HTTP 404" in warning_text
    assert f"url='{url}'" not in warning_text
    assert "详情：404, message='HTTP 404'）" in warning_text
    assert (
        "指标：图片下载失败/http_4xx，值：1\n"
        in timing_path.read_text(encoding="utf-8")
    )


def _setup_output_dir(tmp_path: Path) -> Path:
    output_dir = tmp_path / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def test_persistent_cache_survives_new_cache_instance(tmp_path: Path) -> None:
    output_dir = _setup_output_dir(tmp_path)
    image_path = output_dir / "images_unique" / "persist.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        cache_a = ImageValidationCache()
        with patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            wraps=image_file_is_valid,
        ) as validation_mock:
            outcome_a = cache_a.validate(image_path)
            cache_a.flush_new_entries()

        assert outcome_a.source == "deep"
        assert validation_mock.call_count == 1

        cache_b = ImageValidationCache()
        cache_b.preload({image_path})
        with patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            wraps=image_file_is_valid,
        ) as validation_mock_b:
            outcome_b = cache_b.validate(image_path)

        assert outcome_b.source == "persistent"
        assert validation_mock_b.call_count == 0


def test_persistent_cache_invalidates_on_file_change(tmp_path: Path) -> None:
    output_dir = _setup_output_dir(tmp_path)
    image_path = output_dir / "images_unique" / "changed.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        cache_a = ImageValidationCache()
        cache_a.validate(image_path)
        cache_a.flush_new_entries()

        import os
        os.utime(image_path, ns=(image_path.stat().st_mtime_ns + 1, image_path.stat().st_mtime_ns + 1))

        cache_b = ImageValidationCache()
        cache_b.preload({image_path})
        with patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            wraps=image_file_is_valid,
        ) as validation_mock:
            outcome_b = cache_b.validate(image_path)

        assert outcome_b.source == "deep"
        assert validation_mock.call_count == 1


def test_persistent_cache_invalidates_on_invalidate_call(tmp_path: Path) -> None:
    output_dir = _setup_output_dir(tmp_path)
    image_path = output_dir / "images_unique" / "invalidated.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        cache_a = ImageValidationCache()
        cache_a.validate(image_path)
        cache_a.flush_new_entries()

        cache_a.invalidate(image_path)
        cache_a.flush_new_entries()

        cache_b = ImageValidationCache()
        cache_b.preload({image_path})
        with patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            wraps=image_file_is_valid,
        ) as validation_mock:
            outcome_b = cache_b.validate(image_path)

        assert outcome_b.source == "deep"
        assert validation_mock.call_count == 1


def test_persistent_cache_preload_skips_unknown_paths(tmp_path: Path) -> None:
    output_dir = _setup_output_dir(tmp_path)
    existing_path = output_dir / "images_unique" / "exists.png"
    existing_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(existing_path)
    unknown_path = output_dir / "images_unique" / "unknown.png"

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        cache_a = ImageValidationCache()
        cache_a.validate(existing_path)
        cache_a.flush_new_entries()

        cache_b = ImageValidationCache()
        cache_b.preload({existing_path, unknown_path})
        outcome_existing = cache_b.validate(existing_path)
        assert outcome_existing.source == "persistent"

        with patch(
            "nga_tools.backup.image_validation.image_file_is_valid",
            wraps=image_file_is_valid,
        ) as validation_mock:
            outcome_unknown = cache_b.validate(unknown_path)

        assert not outcome_unknown.valid
        assert outcome_unknown.source == "missing"
        assert validation_mock.call_count == 0


def test_persistent_cache_flush_writes_only_new_entries(tmp_path: Path) -> None:
    output_dir = _setup_output_dir(tmp_path)
    cached_path = output_dir / "images_unique" / "cached.png"
    new_path = output_dir / "images_unique" / "new.png"
    cached_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(cached_path)
    Image.new("RGB", (2, 2), color="black").save(new_path)

    with patch(
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        cache_a = ImageValidationCache()
        cache_a.validate(cached_path)
        cache_a.flush_new_entries()

        cache_b = ImageValidationCache()
        cache_b.preload({cached_path, new_path})
        cache_b.validate(cached_path)
        cache_b.validate(new_path)
        cache_b.flush_new_entries()

        import sqlite3
        from nga_tools.backup.image_store import image_index_path
        with closing(sqlite3.connect(image_index_path())) as conn:
            rows = conn.execute(
                "SELECT canonical_path FROM image_validation_cache ORDER BY canonical_path"
            ).fetchall()
        paths = [r[0] for r in rows]
        assert len(paths) == 2
        assert any("cached.png" in p for p in paths)
        assert any("new.png" in p for p in paths)

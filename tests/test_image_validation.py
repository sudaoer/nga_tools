from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
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
    assert sum(outcome.deep_validated for outcome in outcomes) == 1
    assert sum(outcome.cache_hit for outcome in outcomes) == 1


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
    assert second.deep_validated
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
        assert cache.validate(image_path).cache_hit
        cache.invalidate(image_path)
        after_invalidation = cache.validate(image_path)

    assert after_invalidation.deep_validated
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
    assert created.deep_validated
    assert validation_mock.call_count == 1


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

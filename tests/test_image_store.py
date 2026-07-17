from __future__ import annotations

import re
import io
import sqlite3
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from unittest.mock import patch

import imagecodecs
import numpy as np
import pytest
from PIL import Image

from nga_tools.core.nga_images import NGA_img_link_verify
from nga_tools.backup import image_index, image_store
from nga_tools.core import image_formats
from nga_tools.storage import UnsupportedStorageFormatError


def _write_avif_image(path: Path) -> None:
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    pixels[:, :] = [255, 255, 255]
    path.write_bytes(imagecodecs.avif_encode(pixels))


class NgaImageLinkVerifyTest:
    @pytest.mark.parametrize(
        "url",
        [
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-aygqK1nT3cSl9-sg.jpg.thumb.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-8fvtK7ToS5w-5y.jpg.thumb_s.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2t-bodqK8T8S3m-3m.jpg.thumb_ss.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ1a8-90mbZaT3cSsg-g0.jpg.medium.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-8o79K8ToS5k-5k.webp",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQktk-gl8gZgT3cSqo-wf.jpeg",
            "https://img.nga.178.com/attachments/mon_202506/06/-9lddQ0-f0a0Z1tT3cSdc-7i.gif.medium.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.avif",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.heic",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.jxl",
        ],
    )
    def test_accepts_current_nga_image_filename_formats(self, url: str) -> None:
        assert NGA_img_link_verify(url)

    @pytest.mark.parametrize(
        "url",
        [
            "http://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://example.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202513/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/31/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/06/nested/lsQkle.png",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ1ah-kopmZ2p.mp3",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png#frag",
        ],
    )
    def test_rejects_non_image_or_malformed_nga_links(self, url: str) -> None:
        assert not NGA_img_link_verify(url)


class ImageStoreTest:
    def test_ordinary_image_validation_does_not_full_read_for_modern_probe(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            image_path = Path(temp_dir_name) / "ordinary.png"
            Image.new("RGB", (1, 1), color="white").save(image_path)

            with patch(
                "nga_tools.core.image_formats._read_file",
                side_effect=AssertionError("unexpected full read"),
            ):
                extension = image_formats.image_extension_from_file(image_path)
                error = image_formats.image_file_error(image_path)

        assert extension == "png"
        assert error is None

    def test_placeholder_image_path_creates_valid_png_without_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                placeholder_path = image_store.placeholder_image_path()
                placeholder_src = image_store.placeholder_image_src_from_html_dir(
                    output_dir / "123_all" / "pdf",
                )

            with Image.open(placeholder_path) as image:
                image.verify()
            assert placeholder_path == output_dir / 'images_unique' / image_store.PLACEHOLDER_IMAGE_FILENAME
            assert placeholder_src == f'../../images_unique/{image_store.PLACEHOLDER_IMAGE_FILENAME}'
            assert not (output_dir / 'image_index.sqlite3').exists()

    def test_store_downloaded_image_uses_hash_name_and_sqlite_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            temp_image = Path(temp_dir_name) / "download.png"
            Image.new("RGB", (1, 1), color="white").save(temp_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_downloaded_image(
                    temp_image,
                    {"url": image_url},
                )
                mapping = image_index.ImageIndexStore(
                    output_dir
                ).mapping_for_url(image_url)

            unique_path = Path(result["unique_path"])
            assert unique_path.exists()
            assert unique_path.parent.samefile(output_dir / 'images_unique')
            assert re.search('^[0-9a-f]{64}\\.png$', unique_path.name) is not None
            assert mapping is not None
            assert mapping is not None
            assert mapping.unique_rel_path == f'images_unique/{unique_path.name}'
            assert not (output_dir / 'images').exists()
            assert (output_dir / 'image_index.sqlite3').exists()

    def test_store_existing_image_copies_without_removing_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            legacy_image = Path(temp_dir_name) / "legacy.png"
            Image.new("RGB", (1, 1), color="white").save(legacy_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_existing_image(legacy_image, image_url)
                mapping = image_index.ImageIndexStore(
                    output_dir
                ).mapping_for_url(image_url)

            unique_path = Path(result["unique_path"])
            assert legacy_image.exists()
            assert unique_path.exists()
            assert unique_path.parent.samefile(output_dir / 'images_unique')
            assert mapping is not None
            assert mapping.unique_rel_path == f'images_unique/{unique_path.name}'

    def test_pending_image_download_tasks_uses_batched_index_lookup(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            existing_path = unique_dir / "existing.png"
            Image.new("RGB", (1, 1), color="white").save(
                existing_path,
                format="PNG",
            )
            existing_url_with_comma = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-,552eXuT3cS10p-7f7.png"
            )
            existing_url = image_index.normalize_nga_image_url(
                existing_url_with_comma
            )
            missing_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQ2w-8o79K8ToS5k-5k.webp"
            )
            invalid_url = "https://example.com/not-nga.png"

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                index_store = image_index.ImageIndexStore(output_dir)
                index_store.upsert_mapping(existing_url, existing_path)
                mappings = index_store.mappings_for_urls(
                    [existing_url_with_comma, missing_url, invalid_url]
                )
                pending_tasks = image_store.pending_image_download_tasks(
                    [{"url": existing_url_with_comma}, {"url": missing_url}]
                )

            assert set(mappings) == {existing_url}
            assert pending_tasks == [{'url': missing_url}]

    def test_image_index_schema_initializes_once_before_readonly_queries(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        missing_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/missing.png"
        )

        with (
            patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ),
            patch(
                "nga_tools.backup.image_index.configure_connection",
                wraps=image_index.configure_connection,
            ) as writable_config_mock,
            patch(
                "nga_tools.backup.image_index.configure_readonly_connection",
                wraps=image_index.configure_readonly_connection,
            ) as readonly_config_mock,
        ):
            index_store = image_index.ImageIndexStore(output_dir)
            assert index_store.mappings_for_urls([missing_url]) == {}
            assert index_store.mappings_for_urls([missing_url]) == {}

        assert (output_dir / "image_index.sqlite3").is_file()
        assert writable_config_mock.call_count == 1
        assert readonly_config_mock.call_count == 2
        with sqlite3.connect(output_dir / "image_index.sqlite3") as connection:
            assert [
                row[1] for row in connection.execute(
                    "PRAGMA table_info(image_mappings)"
                )
            ] == ["url", "unique_rel_path"]

    def test_old_image_index_schema_is_rejected_without_mutation(
        self,
        tmp_path: Path,
    ) -> None:
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        image_index_path = output_dir / "image_index.sqlite3"
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
        )
        old_relative_path = "images_unique/old.png"
        with sqlite3.connect(image_index_path) as connection:
            image_index.ensure_storage_metadata(connection, role="image_index")
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
                    image_url,
                    old_relative_path,
                    "2026-07-01T00:00:00+00:00",
                    "2026-07-02T00:00:00+00:00",
                ),
            )

        with patch(
            "nga_tools.config.get_config",
            return_value=SimpleNamespace(output_dir=str(output_dir)),
        ):
            with pytest.raises(UnsupportedStorageFormatError):
                image_index.ImageIndexStore(output_dir).upsert_mapping(
                    image_url,
                    output_dir / "images_unique/new.png",
                )

        with sqlite3.connect(image_index_path) as connection:
            assert [
                row[1] for row in connection.execute(
                    "PRAGMA table_info(image_mappings)"
                )
            ] == ["url", "unique_rel_path", "created_at", "updated_at"]
            assert connection.execute(
                "SELECT url, unique_rel_path FROM image_mappings"
            ).fetchall() == [(image_url, old_relative_path)]

    def test_pending_image_download_tasks_retries_invalid_mapped_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            invalid_path = unique_dir / "broken.png"
            invalid_path.write_bytes(b"not an image")
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                image_index.ImageIndexStore(output_dir).upsert_mapping(
                    image_url, invalid_path
                )
                pending_tasks = image_store.pending_image_download_tasks(
                    [{"url": image_url}]
                )

            assert pending_tasks == [{"url": image_url}]

    def test_pending_image_download_tasks_reuses_avif_file_with_legacy_extension(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            avif_path = unique_dir / "legacy.png"
            _write_avif_image(avif_path)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/02/lsQ92-jkkjK3.png"
            )

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                image_index.ImageIndexStore(output_dir).upsert_mapping(
                    image_url, avif_path
                )
                pending_tasks = image_store.pending_image_download_tasks(
                    [{"url": image_url}]
                )

            assert pending_tasks == []

    def test_store_downloaded_image_uses_avif_extension_from_file_content(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            temp_image = Path(temp_dir_name) / "download.png"
            _write_avif_image(temp_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/02/lsQ92-jkkjK3.png"
            )

            with patch(
                "nga_tools.config.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_downloaded_image(
                    temp_image,
                    {"url": image_url},
                )
                pending_tasks = image_store.pending_image_download_tasks(
                    [{"url": image_url}]
                )

            unique_path = Path(result["unique_path"])
            assert unique_path.suffix == ".avif"
            assert unique_path.exists()
            assert pending_tasks == []

    def test_store_downloaded_image_replaces_invalid_target_without_collision(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            image_hash = "a" * 64
            target_path = unique_dir / f"{image_hash}.png"
            target_path.write_bytes(b"not an image")
            temp_image = Path(temp_dir_name) / "download.png"
            Image.new("RGB", (1, 1), color="white").save(temp_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with (
                patch(
                    "nga_tools.config.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.image_store.sha256", return_value=image_hash),
            ):
                result = image_store.store_downloaded_image(
                    temp_image,
                    {"url": image_url},
                )

            with Image.open(target_path) as image:
                image.verify()
            assert Path(result["unique_path"]) == target_path
            assert "collision" not in result

    def test_store_downloaded_image_preserves_hash_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            first_temp = Path(temp_dir_name) / "first.png"
            second_temp = Path(temp_dir_name) / "second.png"
            Image.new("RGB", (1, 1), color="white").save(first_temp)
            Image.new("RGB", (1, 1), color="black").save(second_temp)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with (
                patch(
                    "nga_tools.config.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.image_store.sha256", return_value="a" * 64),
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                first = image_store.store_downloaded_image(
                    first_temp,
                    {"url": image_url},
                )
                second = image_store.store_downloaded_image(
                    second_temp,
                    {"url": image_url},
                )

            assert Path(first['unique_path']).name == f"{'a' * 64}.png"
            assert Path(second['unique_path']).name == f"{'a' * 64}-collision-1.png"


def _image_url_for_store(name: str) -> str:
    return (
        "https://img.nga.178.com/attachments/"
        f"mon_202506/06/{name}.png"
    )


def _image_preparation(
    pending_tasks: list[image_store.ImageDownloadTask] | None = None,
) -> image_store.ImageDownloadPreparation:
    tasks = [] if pending_tasks is None else pending_tasks
    return image_store.ImageDownloadPreparation(
        pending_tasks=tasks,
        stats=image_store.ImagePreparationStats(
            task_url_count=len(tasks),
            mapping_hit_url_count=0,
            unique_physical_path_count=0,
            intra_thread_path_dedup_count=0,
            memory_cache_hit_path_count=0,
            deep_validation_path_count=0,
            persistent_cache_hit_path_count=0,
            missing_validation_path_count=0,
            persistent_cache_query_path_count=0,
            invalid_mapping_count=0,
            pending_download_url_count=len(tasks),
        ),
    )


def test_image_download_preparation_runs_one_caller_at_a_time() -> None:
    first_entered = Event()
    second_entered = Event()
    release_first = Event()
    active_count = 0
    max_active_count = 0
    call_count = 0
    state_lock = Lock()

    def prepare_side_effect(
        _tasks: list[image_store.ImageDownloadTask],
    ) -> image_store.ImageDownloadPreparation:
        nonlocal active_count, call_count, max_active_count
        with state_lock:
            call_count += 1
            active_count += 1
            max_active_count = max(max_active_count, active_count)
            current_call = call_count
        if current_call == 1:
            first_entered.set()
            assert release_first.wait(timeout=2)
        else:
            second_entered.set()
        with state_lock:
            active_count -= 1
        return _image_preparation()

    with (
        patch(
            "nga_tools.backup.image_store."
            "_prepare_image_download_tasks_uncoordinated",
            side_effect=prepare_side_effect,
        ),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(image_store.prepare_image_download_tasks, [])
        assert first_entered.wait(timeout=2)
        second = executor.submit(image_store.prepare_image_download_tasks, [])
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        first.result(timeout=2)
        second.result(timeout=2)

    assert max_active_count == 1
    assert second_entered.is_set()


def test_image_download_preparation_releases_slot_after_failure() -> None:
    with patch(
        "nga_tools.backup.image_store._prepare_image_download_tasks_uncoordinated",
        side_effect=(RuntimeError("boom"), _image_preparation()),
    ):
        with pytest.raises(RuntimeError, match="boom"):
            image_store.prepare_image_download_tasks([])
        assert image_store.prepare_image_download_tasks([]) == _image_preparation()


def test_image_download_execution_is_outside_preparation_slot() -> None:
    from nga_tools.backup import image_pipeline

    download_barrier = Barrier(2, timeout=2)

    def download_side_effect(
        _tasks: list[image_store.ImageDownloadTask],
        **_kwargs: object,
    ) -> image_store.CompactImageDownloadSummary:
        download_barrier.wait()
        return {"succeeded_count": 1, "failed": []}

    first_task: image_store.ImageDownloadTask = {
        "url": _image_url_for_store("preparation-slot-first")
    }
    second_task: image_store.ImageDownloadTask = {
        "url": _image_url_for_store("preparation-slot-second")
    }
    with (
        patch(
            "nga_tools.backup.image_store."
            "_prepare_image_download_tasks_uncoordinated",
            side_effect=lambda tasks: _image_preparation(tasks),
        ),
        patch(
            "nga_tools.backup.image_store.download_image_tasks_compact",
            side_effect=download_side_effect,
        ),
        patch("nga_tools.backup.image_pipeline.report_progress"),
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        first = executor.submit(
            image_pipeline._run_download_images,
            1,
            None,
            [first_task],
            collect_successes=False,
        )
        second = executor.submit(
            image_pipeline._run_download_images,
            2,
            None,
            [second_task],
            collect_successes=False,
        )
        first.result(timeout=3)
        second.result(timeout=3)


def test_prepare_image_download_tasks_uses_persistent_cache(tmp_path: Path) -> None:
    output_dir = tmp_path / "output"
    image_path = output_dir / "images_unique" / "persist_store.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)
    url = _image_url_for_store("persist-store")
    from nga_tools.backup.image_validation import (
        ImageValidationCache,
        use_image_validation_cache,
    )
    from nga_tools.core.image_formats import image_file_is_valid as real_validate

    with patch(
        "nga_tools.config.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_index.ImageIndexStore(output_dir).upsert_mappings(
            [(url, image_path)]
        )

        cache_a = ImageValidationCache()
        with use_image_validation_cache(cache_a):
            with patch(
                "nga_tools.backup.image_validation.image_file_is_valid",
                wraps=real_validate,
            ) as mock_a:
                prep_a = image_store.prepare_image_download_tasks([{"url": url}])

        assert mock_a.call_count == 1
        assert prep_a.stats.deep_validation_path_count == 1
        assert prep_a.stats.persistent_cache_hit_path_count == 0
        assert prep_a.stats.persistent_cache_query_path_count == 1
        cache_a.flush_new_entries()

        cache_b = ImageValidationCache()
        with use_image_validation_cache(cache_b):
            with patch(
                "nga_tools.backup.image_validation.image_file_is_valid",
                wraps=real_validate,
            ) as mock_b:
                prep_b = image_store.prepare_image_download_tasks([{"url": url}])

        assert mock_b.call_count == 0
        assert prep_b.stats.deep_validation_path_count == 0
        assert prep_b.stats.persistent_cache_hit_path_count == 1
        assert prep_b.stats.persistent_cache_query_path_count == 1
        assert prep_b.pending_tasks == []


def test_shared_validation_cache_reports_batch_local_persistent_hits(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "output"
    image_path = output_dir / "images_unique" / "shared-persist.png"
    image_path.parent.mkdir(parents=True)
    Image.new("RGB", (1, 1), color="white").save(image_path)
    url = _image_url_for_store("shared-persist")
    from nga_tools.backup.image_validation import (
        ImageValidationCache,
        use_image_validation_cache,
    )

    with patch(
        "nga_tools.config.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_index.ImageIndexStore(output_dir).upsert_mapping(url, image_path)
        seed_cache = ImageValidationCache()
        seed_cache.validate(image_path)
        seed_cache.flush_new_entries()

        shared_cache = ImageValidationCache()
        with use_image_validation_cache(shared_cache):
            first = image_store.prepare_image_download_tasks([{"url": url}])
            second = image_store.prepare_image_download_tasks([{"url": url}])

    assert first.stats.persistent_cache_query_path_count == 1
    assert first.stats.persistent_cache_hit_path_count == 1
    assert second.stats.persistent_cache_query_path_count == 0
    assert second.stats.persistent_cache_hit_path_count == 0
    assert second.stats.memory_cache_hit_path_count == 1

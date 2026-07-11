from __future__ import annotations

import re
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import imagecodecs
import numpy as np
import pytest
from PIL import Image

from nga_tools import utils
from nga_tools.backup import image_store
from nga_tools.core import image_formats


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
        assert utils.NGA_img_link_verify(url)

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
        assert not utils.NGA_img_link_verify(url)


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
                "nga_tools.backup.image_store.get_config",
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
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_downloaded_image(
                    temp_image,
                    {"url": image_url},
                )
                mapping = image_store.image_mapping_for_url(image_url)

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
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_existing_image(legacy_image, image_url)
                mapping = image_store.image_mapping_for_url(image_url)

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
            existing_url = image_store.normalize_nga_image_url(existing_url_with_comma)
            missing_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQ2w-8o79K8ToS5k-5k.webp"
            )
            invalid_url = "https://example.com/not-nga.png"

            with patch(
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                image_store.upsert_image_mapping(existing_url, existing_path)
                mappings = image_store.image_mappings_for_urls(
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
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ),
            patch(
                "nga_tools.backup.image_store.configure_connection",
                wraps=image_store.configure_connection,
            ) as writable_config_mock,
            patch(
                "nga_tools.backup.image_store.configure_readonly_connection",
                wraps=image_store.configure_readonly_connection,
            ) as readonly_config_mock,
        ):
            assert image_store.image_mappings_for_urls([missing_url]) == {}
            assert image_store.image_mappings_for_urls([missing_url]) == {}

        assert (output_dir / "image_index.sqlite3").is_file()
        assert writable_config_mock.call_count == 1
        assert readonly_config_mock.call_count == 2

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
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                image_store.upsert_image_mapping(image_url, invalid_path)
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
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                image_store.upsert_image_mapping(image_url, avif_path)
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
                "nga_tools.backup.image_store.get_config",
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
                    "nga_tools.backup.image_store.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.image_store.utils.sha256", return_value=image_hash),
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
                    "nga_tools.backup.image_store.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.image_store.utils.sha256", return_value="a" * 64),
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
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_store.upsert_image_mappings([(url, image_path)])

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
        "nga_tools.backup.image_store.get_config",
        return_value=SimpleNamespace(output_dir=str(output_dir)),
    ):
        image_store.upsert_image_mapping(url, image_path)
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

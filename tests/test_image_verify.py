from __future__ import annotations

import pytest
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

import imagecodecs
import numpy as np
from PIL import Image
from rich.console import Console

from nga_tools.backup import image_store
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.image_verify import (
    ImageVerifyResult,
    _image_verify_worker_count,
    _list_downloaded_image_folders,
    _list_thread_referenced_image_paths,
    _verify_images_in_folder,
    verify_all_downloaded_images,
)
from nga_tools.backup.post_overlay import make_post_overlay
from nga_tools.cli import args_parse
from nga_tools.console import (
    ConsoleReporter,
    WarningCategory,
    report_warning,
    use_reporter,
)
from nga_tools.commands.image import image_verify


def _write_avif_image(path: Path) -> None:
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    pixels[:, :] = [255, 255, 255]
    path.write_bytes(imagecodecs.avif_encode(pixels))


class ImageVerifyCliTest:
    def test_image_verify_parses_without_thread_target(self) -> None:
        args = args_parse(["image", "verify"])

        assert args['command'] == 'image'
        assert args['action'] == 'verify'
        assert args['name'] is None
        assert args['tid'] is None
        assert args['aid'] is None

    def test_image_verify_still_parses_single_thread_target(self) -> None:
        args = args_parse(["image", "verify", "--name", "帖子名"])

        assert args['command'] == 'image'
        assert args['action'] == 'verify'
        assert args['name'] == '帖子名'

    def test_image_migrate_is_rejected(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["image", "migrate"])

        assert context.value.code == 2

    def test_image_prune_links_is_rejected(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with pytest.raises(SystemExit) as context:
                args_parse(["image", "prune-links"])

        assert context.value.code == 2


class ImageVerifyHandlerTest:
    def test_without_thread_target_verifies_all_downloaded_images(self) -> None:
        with (
            patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock,
            patch("nga_tools.commands.image.resolve_command_thread_target") as resolve_mock,
            patch("nga_tools.commands.image.verify_downloaded_images") as verify_mock,
        ):
            image_verify({"name": None, "tid": None, "aid": None})

        all_mock.assert_called_once_with()
        resolve_mock.assert_not_called()
        verify_mock.assert_not_called()

    def test_with_thread_target_verifies_single_thread(self) -> None:
        args = {"name": "帖子名", "tid": None, "aid": None}
        with tempfile.TemporaryDirectory() as temp_dir_name:
            thread_dir = Path(temp_dir_name) / "101_201"

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
                *,
                create: bool = True,
            ) -> str:
                assert (tid, aid) == (101, 201)
                path = thread_dir
                if subfolder is not None:
                    path = path / subfolder
                if create:
                    path.mkdir(parents=True, exist_ok=True)
                return str(path)

            with (
                patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock,
                patch(
                    "nga_tools.commands.image.resolve_command_thread_target",
                    return_value=(101, 201),
                ) as resolve_mock,
                patch("nga_tools.commands.image.verify_downloaded_images") as verify_mock,
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.commands.image.load_timing_log_enabled",
                    return_value=True,
                ),
            ):
                image_verify(args)

        all_mock.assert_not_called()
        resolve_mock.assert_called_once_with(args)
        verify_mock.assert_called_once_with(101, 201)

    def test_single_thread_verify_writes_warning_log(self) -> None:
        args = {"name": "帖子名", "tid": None, "aid": None}
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )

        with tempfile.TemporaryDirectory() as temp_dir_name:
            base_dir = Path(temp_dir_name)
            thread_dir = base_dir / "101_201"
            thread_dir.mkdir()
            log_path = thread_dir / "warnings.log"
            log_path.write_text("旧日志\n", encoding="utf-8")
            timing_path = thread_dir / "timing.log"
            timing_path.write_text("旧耗时\n", encoding="utf-8")

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
                *,
                create: bool = True,
            ) -> str:
                assert (tid, aid) == (101, 201)
                path = thread_dir
                if subfolder is not None:
                    path = path / subfolder
                if create:
                    path.mkdir(parents=True, exist_ok=True)
                return str(path)

            def verify_side_effect(tid: int, aid: int | None) -> None:
                assert (tid, aid) == (101, 201)
                report_warning(
                    WarningCategory.IMAGE_PROCESSING,
                    "单帖图片告警",
                )

            with (
                patch(
                    "nga_tools.commands.image.resolve_command_thread_target",
                    return_value=(101, 201),
                ),
                patch(
                    "nga_tools.commands.image.verify_downloaded_images",
                    side_effect=verify_side_effect,
                ),
                patch(
                    "nga_tools.core.paths.get_folder",
                    side_effect=fake_get_folder,
                ),
                patch(
                    "nga_tools.commands.image.load_timing_log_enabled",
                    return_value=True,
                ),
                use_reporter(ConsoleReporter(console)),
            ):
                image_verify(args)

            assert log_path.read_text(encoding='utf-8') == '警告：单帖图片告警\n'
            timing_paths = list(thread_dir.glob("timing-*.log"))
            assert len(timing_paths) == 1
            timing_text = timing_paths[0].read_text(encoding="utf-8")
            assert timing_path.read_text(encoding="utf-8") == "旧耗时\n"
            assert "任务：image verify\n" in timing_text
            assert "目标：tid=101, aid=201\n" in timing_text
            assert "总耗时：" in timing_text
            assert "状态：完成" in timing_text
            assert "警告：单帖图片告警" not in output.getvalue()
            assert (
                "警告汇总：tid=101, aid=201：共1条；图片处理1条。"
                in output.getvalue()
            )

    def test_aid_without_thread_target_is_rejected(self) -> None:
        with patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock:
            with pytest.raises(ValueError):
                image_verify({"name": None, "tid": None, "aid": 201})

        all_mock.assert_not_called()


class ImageVerifyAllTest:
    def test_lists_global_unique_image_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            unique_images = output_dir / "images_unique"
            unique_images.mkdir(parents=True)
            (output_dir / "101_201" / "images").mkdir(parents=True)
            (output_dir / "103_301").mkdir()
            (output_dir / "101_201" / "pdf" / "long_image_slices").mkdir(parents=True)

            with patch(
                "nga_tools.backup.image_verify.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                folders = _list_downloaded_image_folders()

        assert folders == [str(unique_images)]

    def test_verify_all_reports_global_unique_image_directory(self) -> None:
        results = [
            ImageVerifyResult(folder="output/images_unique", total=2, removed=1),
        ]
        with (
            patch(
                "nga_tools.backup.image_verify._list_downloaded_image_folders",
                return_value=["output/images_unique"],
            ),
            patch(
                "nga_tools.backup.image_verify._verify_images_in_folder",
                side_effect=results,
            ) as verify_mock,
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            verify_all_downloaded_images()

        assert verify_mock.call_args_list == [call('output/images_unique')]

    def test_remote_image_without_mapping_does_not_resolve_legacy_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            link_dir.mkdir(parents=True)
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            Image.new("RGB", (1, 1), color="white").save(link_path)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            with patch(
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                resolved_path = image_store.link_path_for_image_src(image_url)

        assert resolved_path is None

    def test_thread_reference_listing_resolves_overlay_unique_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_image = unique_dir / "abc.png"
            Image.new("RGB", (1, 1), color="white").save(unique_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/abc.png"
            )
            ThreadArchiveStore(thread_dir).upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": "original without image",
                        }
                    ],
                },
            )
            ThreadArchiveStore(thread_dir).upsert_post_overlay(
                1,
                make_post_overlay(f"[img]{image_url}[/img]"),
            )
            config = SimpleNamespace(output_dir=str(output_dir))

            with patch(
                "nga_tools.backup.image_store.get_config",
                return_value=config,
            ):
                image_store.upsert_image_mapping(image_url, unique_image)
                unique_image.write_bytes(b"corrupted after overlay save")
                paths = _list_thread_referenced_image_paths(
                    101,
                    None,
                    thread_dir,
                )

        assert paths == [unique_image]

    def test_parallel_folder_verify_removes_broken_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_dir = Path(temp_dir)
            valid_image = image_dir / "valid.png"
            broken_image = image_dir / "broken.png"
            Image.new("RGB", (1, 1), color="white").save(valid_image)
            broken_image.write_bytes(b"not an image")

            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = _verify_images_in_folder(str(image_dir))

            assert result.total == 2
            assert result.removed == 1
            assert valid_image.exists()
            assert not broken_image.exists()

    def test_parallel_folder_verify_keeps_avif_file_with_legacy_extension(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_dir = Path(temp_dir)
            valid_image = image_dir / "valid.png"
            _write_avif_image(valid_image)

            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                result = _verify_images_in_folder(str(image_dir))

            assert result.total == 1
            assert result.removed == 0
            assert valid_image.exists()

    def test_worker_count_is_bounded(self) -> None:
        assert _image_verify_worker_count(0) == 1
        assert _image_verify_worker_count(1) == 1
        assert _image_verify_worker_count(10) == 10
        assert _image_verify_worker_count(100) == 32

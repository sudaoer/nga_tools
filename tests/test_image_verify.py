from __future__ import annotations

import pytest
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from PIL import Image
from rich.console import Console

from nga_tools.backup import image_store
from nga_tools.backup.images import (
    migrate_image_index,
    prune_legacy_image_links,
)
from nga_tools.backup.image_verify import (
    ImageVerifyResult,
    _image_verify_worker_count,
    _list_downloaded_image_folders,
    _list_thread_referenced_image_paths,
    _verify_images_in_folder,
    verify_all_downloaded_images,
)
from nga_tools.cli import args_parse
from nga_tools.console import ConsoleReporter, report_warning, use_reporter
from nga_tools.commands.image import image_verify


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

    def test_image_migrate_parses_without_arguments(self) -> None:
        args = args_parse(["image", "migrate"])

        assert args['command'] == 'image'
        assert args['action'] == 'migrate'

    def test_image_prune_links_parses_without_arguments(self) -> None:
        args = args_parse(["image", "prune-links"])

        assert args['command'] == 'image'
        assert args['action'] == 'prune-links'


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
                report_warning("单帖图片告警")

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
            timing_text = timing_path.read_text(encoding="utf-8")
            assert "旧耗时" not in timing_text
            assert "任务：image verify\n" in timing_text
            assert "目标：tid=101, aid=201\n" in timing_text
            assert "总耗时：" in timing_text
            assert "状态：完成" in timing_text
            assert '警告：单帖图片告警' in output.getvalue()

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

    def test_thread_reference_listing_resolves_legacy_image_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            html_dir = output_dir / "101_201" / "html_modified"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            html_dir.mkdir(parents=True)
            link_dir.mkdir(parents=True)
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            Image.new("RGB", (1, 1), color="white").save(link_path)
            (html_dir / "post_1.html").write_text(
                '<img src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"/>',
                encoding="utf-8",
            )

            paths = _list_thread_referenced_image_paths(html_dir)

        assert paths == [link_path]

    def test_thread_reference_listing_resolves_direct_unique_image_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            html_dir = output_dir / "101_201" / "html_modified"
            unique_dir = output_dir / "images_unique"
            html_dir.mkdir(parents=True)
            unique_dir.mkdir()
            unique_image = unique_dir / "abc.png"
            Image.new("RGB", (1, 1), color="white").save(unique_image)
            (html_dir / "post_1.html").write_text(
                '<img src="../../images_unique/abc.png"/>',
                encoding="utf-8",
            )

            paths = _list_thread_referenced_image_paths(html_dir)

        assert paths == [unique_image]

    def test_migrate_image_index_rewrites_legacy_html_and_preserves_source_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            html_dir = output_dir / "101_201" / "html_modified"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            html_dir.mkdir(parents=True)
            link_dir.mkdir(parents=True)
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            Image.new("RGB", (1, 1), color="white").save(link_path)
            (html_dir / "post_1.html").write_text(
                '<img src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"/>',
                encoding="utf-8",
            )
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )

            config = SimpleNamespace(output_dir=str(output_dir))
            with (
                patch("nga_tools.backup.images.get_config", return_value=config),
                patch("nga_tools.backup.image_store.get_config", return_value=config),
            ):
                result = migrate_image_index()
                mapping = image_store.image_mapping_for_url(image_url)

            migrated_html = (html_dir / "post_1.html").read_text(encoding="utf-8")
            legacy_file_survived = link_path.is_file()
            unique_file_exists = (
                (output_dir / mapping.unique_rel_path).exists()
                if mapping is not None
                else False
            )

        assert result.mappings == 1
        assert result.updated_image_refs == 1
        assert mapping is not None
        assert unique_file_exists
        assert 'src="../../images_unique/' in migrated_html
        assert legacy_file_survived

    def test_prune_legacy_image_links_removes_only_after_html_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir) / "output"
            html_dir = output_dir / "101_201" / "html_modified"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            unique_dir = output_dir / "images_unique"
            html_dir.mkdir(parents=True)
            link_dir.mkdir(parents=True)
            unique_dir.mkdir()
            unique_image = unique_dir / "abc.png"
            Image.new("RGB", (1, 1), color="white").save(unique_image)
            legacy_image = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            Image.new("RGB", (1, 1), color="white").save(legacy_image)
            (html_dir / "post_1.html").write_text(
                '<img src="../../images_unique/abc.png"/>',
                encoding="utf-8",
            )

            config = SimpleNamespace(output_dir=str(output_dir))
            with (
                patch("nga_tools.backup.images.get_config", return_value=config),
                patch("nga_tools.backup.image_store.get_config", return_value=config),
            ):
                result = prune_legacy_image_links()
            images_dir_exists = (output_dir / "images").exists()

        assert result.removed_links == 1
        assert not images_dir_exists

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

    def test_worker_count_is_bounded(self) -> None:
        assert _image_verify_worker_count(0) == 1
        assert _image_verify_worker_count(1) == 1
        assert _image_verify_worker_count(10) == 10
        assert _image_verify_worker_count(100) == 32

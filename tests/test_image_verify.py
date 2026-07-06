from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from PIL import Image
from rich.console import Console

from nga_tools.backup import image_store
from nga_tools.backup.images import (
    ImageVerifyResult,
    _image_verify_worker_count,
    _list_downloaded_image_folders,
    _list_thread_referenced_image_paths,
    _verify_images_in_folder,
    migrate_image_index,
    prune_legacy_image_links,
    verify_all_downloaded_images,
)
from nga_tools.cli import args_parse
from nga_tools.console import ConsoleReporter, report_warning, use_reporter
from nga_tools.commands.image import image_verify


class ImageVerifyCliTest(unittest.TestCase):
    def test_image_verify_parses_without_thread_target(self) -> None:
        args = args_parse(["image", "verify"])

        self.assertEqual(args["command"], "image")
        self.assertEqual(args["action"], "verify")
        self.assertIsNone(args["name"])
        self.assertIsNone(args["tid"])
        self.assertIsNone(args["aid"])

    def test_image_verify_still_parses_single_thread_target(self) -> None:
        args = args_parse(["image", "verify", "--name", "帖子名"])

        self.assertEqual(args["command"], "image")
        self.assertEqual(args["action"], "verify")
        self.assertEqual(args["name"], "帖子名")

    def test_image_migrate_parses_without_arguments(self) -> None:
        args = args_parse(["image", "migrate"])

        self.assertEqual(args["command"], "image")
        self.assertEqual(args["action"], "migrate")

    def test_image_prune_links_parses_without_arguments(self) -> None:
        args = args_parse(["image", "prune-links"])

        self.assertEqual(args["command"], "image")
        self.assertEqual(args["action"], "prune-links")


class ImageVerifyHandlerTest(unittest.TestCase):
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
        with (
            patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock,
            patch(
                "nga_tools.commands.image.resolve_command_thread_target",
                return_value=(101, 201),
            ) as resolve_mock,
            patch("nga_tools.commands.image.verify_downloaded_images") as verify_mock,
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

            def fake_get_folder(
                tid: int,
                aid: int | None,
                subfolder: str | None = None,
            ) -> str:
                self.assertEqual((tid, aid), (101, 201))
                path = thread_dir
                if subfolder is not None:
                    path = path / subfolder
                path.mkdir(parents=True, exist_ok=True)
                return str(path)

            def verify_side_effect(tid: int, aid: int | None) -> None:
                self.assertEqual((tid, aid), (101, 201))
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
                    "nga_tools.commands.warning_log.utils.get_folder",
                    side_effect=fake_get_folder,
                ),
                use_reporter(ConsoleReporter(console)),
            ):
                image_verify(args)

            self.assertEqual(
                log_path.read_text(encoding="utf-8"),
                "警告：单帖图片告警\n",
            )
            self.assertIn("警告：单帖图片告警", output.getvalue())

    def test_aid_without_thread_target_is_rejected(self) -> None:
        with patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock:
            with self.assertRaises(ValueError):
                image_verify({"name": None, "tid": None, "aid": 201})

        all_mock.assert_not_called()


class ImageVerifyAllTest(unittest.TestCase):
    def test_lists_global_unique_image_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            unique_images = output_dir / "images_unique"
            unique_images.mkdir(parents=True)
            (output_dir / "101_201" / "images").mkdir(parents=True)
            (output_dir / "103_301").mkdir()
            (output_dir / "101_201" / "pdf" / "long_image_slices").mkdir(parents=True)

            with patch(
                "nga_tools.backup.images.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                folders = _list_downloaded_image_folders()

        self.assertEqual(
            folders,
            [
                str(unique_images),
            ],
        )

    def test_verify_all_reports_global_unique_image_directory(self) -> None:
        results = [
            ImageVerifyResult(folder="output/images_unique", total=2, removed=1),
        ]
        with (
            patch(
                "nga_tools.backup.images._list_downloaded_image_folders",
                return_value=["output/images_unique"],
            ),
            patch(
                "nga_tools.backup.images._verify_images_in_folder",
                side_effect=results,
            ) as verify_mock,
            patch("builtins.print"),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            verify_all_downloaded_images()

        self.assertEqual(
            verify_mock.call_args_list,
            [
                call("output/images_unique"),
            ],
        )

    def test_thread_reference_listing_resolves_global_image_link(self) -> None:
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
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            link_path.symlink_to(Path("../../..") / "images_unique" / "abc.png")
            (html_dir / "post_1.html").write_text(
                '<img src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"/>',
                encoding="utf-8",
            )

            paths = _list_thread_referenced_image_paths(html_dir)

        self.assertEqual(paths, [unique_image])

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

        self.assertEqual(paths, [unique_image])

    def test_migrate_image_index_rewrites_legacy_html_and_preserves_links(self) -> None:
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
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            link_path.symlink_to(Path("../../..") / "images_unique" / "abc.png")
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
            legacy_link_survived = link_path.is_symlink()

        self.assertEqual(result.mappings, 1)
        self.assertEqual(result.updated_image_refs, 1)
        self.assertIsNotNone(mapping)
        self.assertIn('src="../../images_unique/abc.png"', migrated_html)
        self.assertTrue(legacy_link_survived)

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
            (link_dir / "lsQkle-552eXuT3cS10p-7f7.png").symlink_to(
                Path("../../..") / "images_unique" / "abc.png"
            )
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

        self.assertEqual(result.removed_links, 1)
        self.assertFalse(images_dir_exists)

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

            self.assertEqual(result.total, 2)
            self.assertEqual(result.removed, 1)
            self.assertTrue(valid_image.exists())
            self.assertFalse(broken_image.exists())

    def test_worker_count_is_bounded(self) -> None:
        self.assertEqual(_image_verify_worker_count(0), 1)
        self.assertEqual(_image_verify_worker_count(1), 1)
        self.assertEqual(_image_verify_worker_count(10), 10)
        self.assertEqual(_image_verify_worker_count(100), 32)


if __name__ == "__main__":
    unittest.main()

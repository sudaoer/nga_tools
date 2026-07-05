from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from PIL import Image

from nga_tools.backup.images import (
    ImageVerifyResult,
    _image_verify_worker_count,
    _list_downloaded_image_folders,
    _verify_images_in_folder,
    verify_all_downloaded_images,
)
from nga_tools.cli import args_parse
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

    def test_aid_without_thread_target_is_rejected(self) -> None:
        with patch("nga_tools.commands.image.verify_all_downloaded_images") as all_mock:
            with self.assertRaises(ValueError):
                image_verify({"name": None, "tid": None, "aid": 201})

        all_mock.assert_not_called()


class ImageVerifyAllTest(unittest.TestCase):
    def test_lists_only_direct_backup_image_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_dir = Path(temp_dir)
            first_images = output_dir / "101_201" / "images"
            second_images = output_dir / "102_all" / "images"
            first_images.mkdir(parents=True)
            second_images.mkdir(parents=True)
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
                str(first_images),
                str(second_images),
            ],
        )

    def test_verify_all_reports_each_image_directory(self) -> None:
        results = [
            ImageVerifyResult(folder="output/101_201/images", total=2, removed=1),
            ImageVerifyResult(folder="output/102_all/images", total=3, removed=0),
        ]
        with (
            patch(
                "nga_tools.backup.images._list_downloaded_image_folders",
                return_value=["output/101_201/images", "output/102_all/images"],
            ),
            patch(
                "nga_tools.backup.images._verify_images_in_folder",
                side_effect=results,
            ) as verify_mock,
            patch("builtins.print"),
        ):
            verify_all_downloaded_images()

        self.assertEqual(
            verify_mock.call_args_list,
            [
                call("output/101_201/images"),
                call("output/102_all/images"),
            ],
        )

    def test_parallel_folder_verify_removes_broken_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            image_dir = Path(temp_dir)
            valid_image = image_dir / "valid.png"
            broken_image = image_dir / "broken.png"
            Image.new("RGB", (1, 1), color="white").save(valid_image)
            broken_image.write_bytes(b"not an image")

            with patch("builtins.print"):
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

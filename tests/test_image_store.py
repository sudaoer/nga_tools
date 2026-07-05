from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from nga_tools import utils
from nga_tools.backup import image_store


class NgaImageLinkVerifyTest(unittest.TestCase):
    def test_accepts_current_nga_image_filename_formats(self) -> None:
        valid_urls = [
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-aygqK1nT3cSl9-sg.jpg.thumb.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-8fvtK7ToS5w-5y.jpg.thumb_s.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2t-bodqK8T8S3m-3m.jpg.thumb_ss.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ1a8-90mbZaT3cSsg-g0.jpg.medium.jpg",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ2w-8o79K8ToS5k-5k.webp",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQktk-gl8gZgT3cSqo-wf.jpeg",
            "https://img.nga.178.com/attachments/mon_202506/06/-9lddQ0-f0a0Z1tT3cSdc-7i.gif.medium.jpg",
        ]

        for url in valid_urls:
            with self.subTest(url=url):
                self.assertTrue(utils.NGA_img_link_verify(url))

    def test_rejects_non_image_or_malformed_nga_links(self) -> None:
        invalid_urls = [
            "http://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://example.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202513/06/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/31/lsQkle-552eXuT3cS10p-7f7.png",
            "https://img.nga.178.com/attachments/mon_202506/06/nested/lsQkle.png",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQ1ah-kopmZ2p.mp3",
            "https://img.nga.178.com/attachments/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png#frag",
        ]

        for url in invalid_urls:
            with self.subTest(url=url):
                self.assertFalse(utils.NGA_img_link_verify(url))


class ImageStoreTest(unittest.TestCase):
    def test_store_downloaded_image_uses_hash_name_and_relative_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir_name:
            output_dir = Path(temp_dir_name) / "output"
            temp_image = Path(temp_dir_name) / "download.png"
            Image.new("RGB", (1, 1), color="white").save(temp_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )
            link_path = output_dir / "images" / "mon_202506" / "06" / "image.png"

            with patch(
                "nga_tools.backup.image_store.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                result = image_store.store_downloaded_image(
                    temp_image,
                    {"url": image_url, "link_path": str(link_path)},
                )

            unique_path = Path(result["unique_path"])
            self.assertTrue(unique_path.exists())
            self.assertTrue(unique_path.parent.samefile(output_dir / "images_unique"))
            self.assertRegex(unique_path.name, r"^[0-9a-f]{64}\.png$")
            self.assertTrue(link_path.is_symlink())
            self.assertFalse(os.readlink(link_path).startswith("/"))
            self.assertTrue(link_path.exists())

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
            ):
                first = image_store.store_downloaded_image(
                    first_temp,
                    {
                        "url": image_url,
                        "link_path": str(
                            output_dir
                            / "images"
                            / "mon_202506"
                            / "06"
                            / "first.png"
                        ),
                    },
                )
                second = image_store.store_downloaded_image(
                    second_temp,
                    {
                        "url": image_url,
                        "link_path": str(
                            output_dir
                            / "images"
                            / "mon_202506"
                            / "06"
                            / "second.png"
                        ),
                    },
                )

            self.assertEqual(Path(first["unique_path"]).name, f"{'a' * 64}.png")
            self.assertEqual(
                Path(second["unique_path"]).name,
                f"{'a' * 64}-collision-1.png",
            )
            self.assertTrue(second.get("collision"))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from nga_tools.backup import html_manifest


class HtmlManifestTest(unittest.TestCase):
    def test_write_load_and_file_existence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            (html_dir / "post_1.html").write_text("html", encoding="utf-8")
            entries: dict[str, html_manifest.HtmlManifestEntry] = {
                "post_1.html": {
                    "source_hash": html_manifest.hash_text("source"),
                    "output_hash": html_manifest.hash_text("html"),
                }
            }

            html_manifest.write_manifest(html_dir, entries)
            loaded_entries = html_manifest.load_manifest(html_dir)
            files_exist = html_manifest.manifest_files_exist(html_dir, loaded_entries)
            (html_dir / "post_1.html").unlink()
            files_exist_after_delete = html_manifest.manifest_files_exist(
                html_dir,
                loaded_entries,
            )

        self.assertEqual(loaded_entries, entries)
        self.assertTrue(files_exist)
        self.assertFalse(files_exist_after_delete)

    def test_generation_version_mismatch_is_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            html_manifest.manifest_path(html_dir).write_text(
                json.dumps(
                    {
                        "version": html_manifest.HTML_MANIFEST_VERSION,
                        "html_generation_version": 999,
                        "algorithm": html_manifest.HTML_HASH_ALGORITHM,
                        "files": {
                            "post_1.html": {
                                "source_hash": "source",
                                "output_hash": "output",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )

            entries = html_manifest.load_manifest(html_dir)

        self.assertEqual(entries, {})


if __name__ == "__main__":
    unittest.main()

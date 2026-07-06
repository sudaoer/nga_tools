from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.pdf import (
    PDF_HASH_MANIFEST_FILENAME,
    PdfRenderPlan,
    _build_render_tasks,
    _read_pdf_html,
    _write_pdf_hashes,
)


class PdfHashCacheTest(unittest.TestCase):
    def _build_plan(
        self,
        folder_pdf: Path,
        html_content_by_lou: dict[int, str],
    ) -> PdfRenderPlan:
        app_config = SimpleNamespace(
            html_style="<style>body{}</style>",
            html_pre='<div class="bbcode_container">',
            html_post="</div>",
        )
        with patch("nga_tools.backup.pdf.get_config", return_value=app_config):
            return _build_render_tasks(
                html_content_by_lou,
                str(folder_pdf),
                2,
                FloorLabels.plain(),
            )

    def test_first_run_writes_html_and_schedules_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.skipped_count, 0)
            self.assertEqual(plan.cleaned_count, 0)
            self.assertEqual(len(plan.render_tasks), 1)
            self.assertEqual(
                plan.render_tasks[0].html_path,
                str(folder_pdf / "part_0_1.html"),
            )
            self.assertTrue((folder_pdf / "part_0_1.html").exists())
            self.assertIn(
                "zero",
                (folder_pdf / "part_0_1.html").read_text(encoding="utf-8"),
            )
            self.assertIn("part_0_1.html", plan.input_hashes)

    def test_matching_hash_and_existing_pdf_skips_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            _write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").write_bytes(b"%PDF-1.7\n")
            (folder_pdf / "part_0_1.html").unlink()

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.skipped_count, 1)
            self.assertEqual(plan.cleaned_count, 0)
            self.assertEqual(plan.render_tasks, [])
            self.assertTrue((folder_pdf / "part_0_1.html").exists())

    def test_changed_html_schedules_render_even_when_pdf_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            _write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>changed</p>"})

            self.assertEqual(plan.skipped_count, 0)
            self.assertEqual(plan.cleaned_count, 0)
            self.assertEqual(len(plan.render_tasks), 1)

    def test_missing_pdf_schedules_render_even_when_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            _write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").unlink(missing_ok=True)

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.skipped_count, 0)
            self.assertEqual(plan.cleaned_count, 0)
            self.assertEqual(len(plan.render_tasks), 1)

    def test_invalid_manifest_is_treated_as_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            (folder_pdf / PDF_HASH_MANIFEST_FILENAME).write_text(
                "{bad",
                encoding="utf-8",
            )
            (folder_pdf / "part_0_1.pdf").write_bytes(b"%PDF-1.7\n")

            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.skipped_count, 0)
            self.assertEqual(plan.cleaned_count, 0)
            self.assertEqual(len(plan.render_tasks), 1)

    def test_removes_stale_tail_pdf_and_html_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            (folder_pdf / "part_2_3.html").write_text("stale", encoding="utf-8")
            (folder_pdf / "part_2_3.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.cleaned_count, 2)
            self.assertFalse((folder_pdf / "part_2_3.html").exists())
            self.assertFalse((folder_pdf / "part_2_3.pdf").exists())
            self.assertTrue((folder_pdf / "part_0_1.html").exists())

    def test_removes_stale_parts_after_segment_range_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            (folder_pdf / "part_0_3.html").write_text("stale", encoding="utf-8")
            (folder_pdf / "part_0_3.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.cleaned_count, 2)
            self.assertFalse((folder_pdf / "part_0_3.html").exists())
            self.assertFalse((folder_pdf / "part_0_3.pdf").exists())

    def test_keeps_non_part_files_and_slice_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            slice_dir = folder_pdf / "long_image_slices"
            slice_dir.mkdir()
            (slice_dir / "part_2_3.pdf").write_bytes(b"slice")
            (folder_pdf / "manual.pdf").write_bytes(b"%PDF-1.7\n")
            (folder_pdf / "notes.html").write_text("notes", encoding="utf-8")
            (folder_pdf / "part_draft_2_3.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            self.assertEqual(plan.cleaned_count, 0)
            self.assertTrue((slice_dir / "part_2_3.pdf").exists())
            self.assertTrue((folder_pdf / "manual.pdf").exists())
            self.assertTrue((folder_pdf / "notes.html").exists())
            self.assertTrue((folder_pdf / "part_draft_2_3.pdf").exists())


class PdfImageSourceTest(unittest.TestCase):
    def test_read_pdf_html_resolves_global_image_link(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            html_dir = output_dir / "101_all" / "html_modified"
            unique_dir = output_dir / "images_unique"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            html_dir.mkdir(parents=True)
            unique_dir.mkdir(parents=True)
            link_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.png"
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            link_path.symlink_to(Path("../../..") / "images_unique" / "hash.png")
            (html_dir / "post_1.html").write_text(
                '<p><img src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"/></p>',
                encoding="utf-8",
            )

            with (
                patch(
                    "nga_tools.utils.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        self.assertIn(
            'src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"',
            html_content_by_lou[1],
        )


if __name__ == "__main__":
    unittest.main()

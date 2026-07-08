from __future__ import annotations

import io
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from rich.console import Console

from nga_tools.backup import image_store
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.pdf import (
    PdfRenderResult,
    _read_pdf_html,
    _report_weasyprint_output,
    _run_weasyprint,
    _split_weasyprint_output,
)
from nga_tools.backup.pdf_plan import (
    PDF_HASH_MANIFEST_FILENAME,
    PdfRenderPlan,
    PdfRenderTask,
    build_render_tasks,
    write_pdf_hashes,
)
from nga_tools.console import ConsoleReporter, use_reporter, use_warning_log


class PdfHashCacheTest:
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
        with patch("nga_tools.backup.pdf_plan.get_config", return_value=app_config):
            return build_render_tasks(
                html_content_by_lou,
                str(folder_pdf),
                2,
                FloorLabels.plain(),
            )

    def test_first_run_writes_html_and_schedules_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.skipped_count == 0
            assert plan.cleaned_count == 0
            assert len(plan.render_tasks) == 1
            assert plan.render_tasks[0].html_path == str(folder_pdf / 'part_0_1.html')
            assert (folder_pdf / 'part_0_1.html').exists()
            assert 'zero' in (folder_pdf / 'part_0_1.html').read_text(encoding='utf-8')
            assert 'part_0_1.html' in plan.input_hashes

    def test_matching_hash_and_existing_pdf_skips_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").write_bytes(b"%PDF-1.7\n")
            (folder_pdf / "part_0_1.html").unlink()

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.skipped_count == 1
            assert plan.cleaned_count == 0
            assert plan.render_tasks == []
            assert (folder_pdf / 'part_0_1.html').exists()

    def test_changed_html_schedules_render_even_when_pdf_exists(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>changed</p>"})

            assert plan.skipped_count == 0
            assert plan.cleaned_count == 0
            assert len(plan.render_tasks) == 1

    def test_missing_pdf_schedules_render_even_when_hash_matches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").unlink(missing_ok=True)

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.skipped_count == 0
            assert plan.cleaned_count == 0
            assert len(plan.render_tasks) == 1

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

            assert plan.skipped_count == 0
            assert plan.cleaned_count == 0
            assert len(plan.render_tasks) == 1

    def test_removes_stale_tail_pdf_and_html_parts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            (folder_pdf / "part_2_3.html").write_text("stale", encoding="utf-8")
            (folder_pdf / "part_2_3.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.cleaned_count == 2
            assert not (folder_pdf / 'part_2_3.html').exists()
            assert not (folder_pdf / 'part_2_3.pdf').exists()
            assert (folder_pdf / 'part_0_1.html').exists()

    def test_removes_stale_parts_after_segment_range_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            (folder_pdf / "part_0_3.html").write_text("stale", encoding="utf-8")
            (folder_pdf / "part_0_3.pdf").write_bytes(b"%PDF-1.7\n")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.cleaned_count == 2
            assert not (folder_pdf / 'part_0_3.html').exists()
            assert not (folder_pdf / 'part_0_3.pdf').exists()

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

            assert plan.cleaned_count == 0
            assert (slice_dir / 'part_2_3.pdf').exists()
            assert (folder_pdf / 'manual.pdf').exists()
            assert (folder_pdf / 'notes.html').exists()
            assert (folder_pdf / 'part_draft_2_3.pdf').exists()


class PdfWeasyPrintCaptureTest:
    def test_split_weasyprint_output_filters_blank_lines(self) -> None:
        assert _split_weasyprint_output(None) == ()
        assert _split_weasyprint_output(
            "\nWARNING: missing glyph\r\n  INFO: done  \n\n"
        ) == ("WARNING: missing glyph", "INFO: done")

    def test_run_weasyprint_captures_combined_output(self) -> None:
        task = PdfRenderTask("/tmp/part_0_1.html", "/tmp/part_0_1.pdf")

        with patch(
            "nga_tools.backup.pdf.subprocess.run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout="WARNING: missing glyph\n\n  INFO: done  \n",
            ),
        ) as run:
            result = _run_weasyprint(task)

        run.assert_called_once_with(
            ["weasyprint", task.html_path, task.output_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
        assert result == PdfRenderResult(
            task=task,
            returncode=0,
            output_lines=("WARNING: missing glyph", "INFO: done"),
        )

    def test_reports_weasyprint_output_through_warning_reporter(self) -> None:
        output = io.StringIO()
        console = Console(
            file=output,
            force_terminal=False,
            color_system=None,
            width=120,
        )
        result = PdfRenderResult(
            task=PdfRenderTask("/tmp/part_0_1.html", "/tmp/part_0_1.pdf"),
            returncode=0,
            output_lines=("WARNING: missing glyph",),
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            log_path = Path(tmp_dir) / "warnings.log"
            with (
                use_reporter(ConsoleReporter(console)),
                use_warning_log(log_path),
            ):
                _report_weasyprint_output([result])

            assert log_path.read_text(
                encoding="utf-8"
            ) == "警告：WeasyPrint part_0_1.pdf: WARNING: missing glyph\n"

        assert output.getvalue() == (
            "警告：WeasyPrint part_0_1.pdf: WARNING: missing glyph\n"
        )


class PdfImageSourceTest:
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
                    "nga_tools.core.paths.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert 'src="../../images/mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"' in html_content_by_lou[1]

    def test_read_pdf_html_keeps_hidden_about_blank_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            html_dir = output_dir / "101_all" / "html_modified"
            html_dir.mkdir(parents=True)
            (html_dir / "post_1.html").write_text(
                (
                    '<p>初始<span style="font-size: 50%;">5楼</span>'
                    '<img src="about:blank" style="display:none" /></p>'
                ),
                encoding="utf-8",
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert 'src="about:blank"' in html_content_by_lou[1]
        assert "display:none" in html_content_by_lou[1]

    def test_read_pdf_html_uses_lazy_about_blank_data_srcorg(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202604/24/lsQ2x-ji3jKnT3cS14u-c3.webp"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            html_dir = output_dir / "101_all" / "html_modified"
            unique_dir = output_dir / "images_unique"
            html_dir.mkdir(parents=True)
            unique_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.png"
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            (html_dir / "post_1.html").write_text(
                (
                    '<p><img src="about:blank" '
                    f'data-srcorg="{image_url}" '
                    'style="max-height: 1em; min-height: 130px;" /></p>'
                ),
                encoding="utf-8",
            )

            with (
                patch(
                    "nga_tools.core.paths.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=SimpleNamespace(output_dir=str(output_dir)),
                ),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                image_store.upsert_image_mapping(image_url, unique_image)
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert 'src="../../images_unique/hash.png"' in html_content_by_lou[1]
        assert f'data-srcorg="{image_url}"' in html_content_by_lou[1]

from __future__ import annotations

import io
import sqlite3
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import imagecodecs
import numpy as np
import pytest
from PIL import Image
from rich.console import Console

from nga_tools.backup import image_store
from nga_tools.backup import pdf_renderer
from nga_tools.backup.archive_store import ThreadArchiveStore
from nga_tools.backup.floor_map import FloorLabels
from nga_tools.backup.pdf import (
    PdfRenderPool,
    PdfRenderResult,
    _image_path_for_pdf,
    _read_pdf_html,
    _report_weasyprint_output,
    _run_pdf_renderer,
    _slice_image_file_is_valid,
    _split_weasyprint_output,
)
from nga_tools.backup.pdf_plan import (
    PDF_HASH_MANIFEST_FILENAME,
    PdfRenderPlan,
    PdfRenderTask,
    build_render_tasks,
    write_pdf_hashes,
)
from nga_tools.backup.post_overlay import make_post_overlay
from nga_tools.console import ConsoleReporter, use_reporter, use_warning_log
from nga_tools.core.hashing import hash_text


def _write_avif_image(path: Path) -> None:
    pixels = np.zeros((2, 3, 3), dtype=np.uint8)
    pixels[:, :] = [255, 255, 255]
    path.write_bytes(imagecodecs.avif_encode(pixels))


def _write_pdf_archive(thread_dir: Path, content: str) -> None:
    ThreadArchiveStore(thread_dir).upsert_page(
        1,
        {
            "totalPage": 1,
            "result": [{"lou": 1, "pid": 1001, "content": content}],
        },
        observed_at="2026-07-11T00:00:00+00:00",
    )


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

    def test_matching_hash_and_invalid_pdf_schedules_render(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            folder_pdf = Path(tmp_dir)
            original_plan = self._build_plan(
                folder_pdf,
                {0: "<p>zero</p>"},
            )
            write_pdf_hashes(str(folder_pdf), original_plan.input_hashes)
            (folder_pdf / "part_0_1.pdf").write_bytes(b"not a pdf")

            plan = self._build_plan(folder_pdf, {0: "<p>zero</p>"})

            assert plan.skipped_count == 0
            assert plan.cleaned_count == 0
            assert len(plan.render_tasks) == 1

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


class _FakeProcessPoolExecutor:
    instances: list["_FakeProcessPoolExecutor"] = []

    def __init__(self, max_workers: int | None = None) -> None:
        self.max_workers = max_workers
        self.map_calls: list[list[PdfRenderTask]] = []
        self.shutdown_count = 0
        type(self).instances.append(self)

    def map(
        self,
        func: object,
        tasks: list[PdfRenderTask],
    ) -> list[PdfRenderResult]:
        del func
        task_list = list(tasks)
        self.map_calls.append(task_list)
        return [
            PdfRenderResult(task=task, returncode=0, output_lines=())
            for task in task_list
        ]

    def shutdown(self) -> None:
        self.shutdown_count += 1


class PdfRenderPoolTest:
    def test_reuses_single_executor_for_multiple_render_calls(self) -> None:
        _FakeProcessPoolExecutor.instances = []
        first_task = PdfRenderTask("/tmp/part_0_1.html", "/tmp/part_0_1.pdf")
        second_task = PdfRenderTask("/tmp/part_2_3.html", "/tmp/part_2_3.pdf")

        with patch(
            "nga_tools.backup.pdf.concurrent.futures.ProcessPoolExecutor",
            _FakeProcessPoolExecutor,
        ):
            with PdfRenderPool(2) as renderer:
                first_result = renderer.render([first_task])
                second_result = renderer.render([second_task])

        executor = _FakeProcessPoolExecutor.instances[0]
        assert len(_FakeProcessPoolExecutor.instances) == 1
        assert executor.max_workers == 2
        assert executor.map_calls == [[first_task], [second_task]]
        assert first_result == [
            PdfRenderResult(task=first_task, returncode=0, output_lines=())
        ]
        assert second_result == [
            PdfRenderResult(task=second_task, returncode=0, output_lines=())
        ]
        assert executor.shutdown_count == 1


class PdfRendererCaptureTest:
    def test_split_weasyprint_output_filters_blank_lines(self) -> None:
        assert _split_weasyprint_output(None) == ()
        assert _split_weasyprint_output(
            "\nWARNING: missing glyph\r\n  INFO: done  \n\n"
        ) == ("WARNING: missing glyph", "INFO: done")

    def test_run_pdf_renderer_captures_combined_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            task = PdfRenderTask(
                str(Path(tmp_dir) / "part_0_1.html"),
                str(Path(tmp_dir) / "part_0_1.pdf"),
            )

            def fake_run(
                args: list[str],
                *,
                stdout: int,
                stderr: int,
                text: bool,
                check: bool,
            ) -> SimpleNamespace:
                del stdout, stderr, text, check
                Path(args[4]).write_bytes(b"%PDF-1.7\n")
                return SimpleNamespace(
                    returncode=0,
                    stdout="WARNING: missing glyph\n\n  INFO: done  \n",
                )

            with patch(
                "nga_tools.backup.pdf.subprocess.run",
                side_effect=fake_run,
            ) as run:
                result = _run_pdf_renderer(task)

            run_args = run.call_args.args[0]
            assert run_args[:3] == [
                sys.executable,
                "-m",
                "nga_tools.backup.pdf_renderer",
            ]
            assert run_args[3] == task.html_path
            assert run_args[4] != task.output_path
            run.assert_called_once_with(
                run_args,
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
            assert Path(task.output_path).read_bytes() == b"%PDF-1.7\n"

    def test_run_pdf_renderer_keeps_existing_pdf_when_output_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            task = PdfRenderTask(
                str(Path(tmp_dir) / "part_0_1.html"),
                str(Path(tmp_dir) / "part_0_1.pdf"),
            )
            Path(task.output_path).write_bytes(b"%PDF-old\n")

            def fake_run(
                args: list[str],
                *,
                stdout: int,
                stderr: int,
                text: bool,
                check: bool,
            ) -> SimpleNamespace:
                del stdout, stderr, text, check
                Path(args[4]).write_bytes(b"not a pdf")
                return SimpleNamespace(returncode=0, stdout="")

            with patch(
                "nga_tools.backup.pdf.subprocess.run",
                side_effect=fake_run,
            ):
                result = _run_pdf_renderer(task)

            assert result.task == task
            assert result.returncode == 1
            assert result.output_lines == (f"PDF输出无效：{task.output_path}",)
            assert Path(task.output_path).read_bytes() == b"%PDF-old\n"

    def test_run_pdf_renderer_reports_start_failure(self) -> None:
        task = PdfRenderTask("/tmp/part_0_1.html", "/tmp/part_0_1.pdf")

        with patch(
            "nga_tools.backup.pdf.subprocess.run",
            side_effect=FileNotFoundError("missing"),
        ):
            result = _run_pdf_renderer(task)

        assert result.task == task
        assert result.returncode == 1
        assert "无法启动PDF渲染子进程" in result.output_lines[0]

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


class PdfRendererEntrypointTest:
    def test_render_pdf_registers_openers_and_calls_weasyprint(self) -> None:
        with (
            patch(
                "nga_tools.backup.pdf_renderer.register_pillow_image_openers"
            ) as register,
            patch("nga_tools.backup.pdf_renderer.weasyprint.HTML") as html_class,
        ):
            pdf_renderer.render_pdf(Path("part.html"), Path("part.pdf"))

        register.assert_called_once_with()
        html_class.assert_called_once_with(filename="part.html")
        html_class.return_value.write_pdf.assert_called_once_with("part.pdf")


class PdfImageSourceTest:
    def test_slice_image_validation_treats_syntax_error_as_invalid(self) -> None:
        with patch("nga_tools.backup.pdf.Image.open", side_effect=SyntaxError("bad")):
            assert not _slice_image_file_is_valid("/tmp/broken.png")

    def test_image_path_for_pdf_allows_windows_drive_path(self) -> None:
        assert _image_path_for_pdf("C:/nga/image.png", Path("html")) == Path(
            "C:/nga/image.png"
        )

    def test_read_pdf_html_uses_selected_archive_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            store = ThreadArchiveStore(thread_dir)
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "before edit"}
                    ],
                },
                observed_at="2026-07-11T00:00:00+00:00",
            )
            store.upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {"lou": 1, "pid": 1001, "content": "after edit"}
                    ],
                },
                observed_at="2026-07-11T01:00:00+00:00",
            )
            with closing(sqlite3.connect(store.db_path)) as connection:
                version_id = connection.execute(
                    """
                    SELECT id
                    FROM post_versions
                    WHERE source_hash = ?
                    """,
                    (hash_text("before edit"),),
                ).fetchone()[0]
            store.upsert_post_version_selection(1, version_id)

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert "before edit" in html_content_by_lou[1]
        assert "after edit" not in html_content_by_lou[1]

    def test_read_pdf_html_does_not_recover_attachment_url(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            relative_src = "./mon_202607/11/attachment.png"
            ThreadArchiveStore(thread_dir).upsert_page(
                1,
                {
                    "totalPage": 1,
                    "result": [
                        {
                            "lou": 1,
                            "pid": 1001,
                            "content": f"[img]{relative_src}[/img]",
                            "attches": [
                                {
                                    "type": "img",
                                    "attachurl": "mon_202607/11/attachment.png",
                                }
                            ],
                        }
                    ],
                },
                observed_at="2026-07-11T00:00:00+00:00",
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert f"[img]{relative_src}[/img]" in html_content_by_lou[1]
        assert "<img" not in html_content_by_lou[1]

    def test_read_pdf_html_converts_avif_image_for_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.avif"
            _write_avif_image(unique_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/hash.avif"
            )
            _write_pdf_archive(thread_dir, f"[img]{image_url}[/img]")
            config = SimpleNamespace(output_dir=str(output_dir))

            with (
                patch(
                    "nga_tools.core.paths.get_config",
                    return_value=config,
                ),
                patch("nga_tools.backup.image_store.get_config", return_value=config),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                image_store.upsert_image_mapping(image_url, unique_image)
                html_content_by_lou, folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

            converted_paths = list(
                (Path(folder_pdf) / "converted_images").iterdir()
            )

            assert len(converted_paths) == 1
            assert converted_paths[0].suffix == ".jpg"
            with Image.open(converted_paths[0]) as image:
                image.verify()
            assert f'src="converted_images/{converted_paths[0].name}"' in (
                html_content_by_lou[1]
            )

    def test_read_pdf_html_keeps_png_image_without_conversion(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.png"
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/11/hash.png"
            )
            _write_pdf_archive(thread_dir, f"[img]{image_url}[/img]")
            config = SimpleNamespace(output_dir=str(output_dir))

            with (
                patch(
                    "nga_tools.core.paths.get_config",
                    return_value=config,
                ),
                patch("nga_tools.backup.image_store.get_config", return_value=config),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                image_store.upsert_image_mapping(image_url, unique_image)
                html_content_by_lou, folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

            assert not (Path(folder_pdf) / "converted_images").exists()
            assert 'src="../../images_unique/hash.png"' in html_content_by_lou[1]

    def test_read_pdf_html_uses_placeholder_instead_of_legacy_link_without_mapping(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_dir = output_dir / "images_unique"
            link_dir = output_dir / "images" / "mon_202506" / "06"
            unique_dir.mkdir(parents=True)
            link_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.png"
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            link_path = link_dir / "lsQkle-552eXuT3cS10p-7f7.png"
            link_path.symlink_to(Path("../../..") / "images_unique" / "hash.png")
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png"
            )
            _write_pdf_archive(thread_dir, f"[img]{image_url}[/img]")
            config = SimpleNamespace(output_dir=str(output_dir))

            with (
                patch(
                    "nga_tools.core.paths.get_config",
                    return_value=config,
                ),
                patch("nga_tools.backup.image_store.get_config", return_value=config),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch("nga_tools.backup.pdf._is_speaker_portrait", return_value=False),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert "download_failed_placeholder.png" in html_content_by_lou[1]
        assert "mon_202506/06/lsQkle-552eXuT3cS10p-7f7.png" not in (
            html_content_by_lou[1]
        )

    def test_read_pdf_html_keeps_hidden_about_blank_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            _write_pdf_archive(
                thread_dir,
                (
                    '<p>初始<span style="font-size: 50%;">5楼</span>'
                    '<img src="about:blank" style="display:none" /></p>'
                ),
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

    def test_read_pdf_html_uses_bbcode_overlay_and_ignores_legacy_html(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            html_dir = thread_dir / "html_modified"
            legacy_overlay_dir = thread_dir / "overlay"
            _write_pdf_archive(thread_dir, "original body")
            html_dir.mkdir(parents=True)
            legacy_overlay_dir.mkdir(parents=True)
            (html_dir / "post_1.html").write_text(
                '<p class="old-html">old materialized html</p>',
                encoding="utf-8",
            )
            (legacy_overlay_dir / "post_1.html").write_text(
                '<p class="legacy-overlay">legacy overlay</p>',
                encoding="utf-8",
            )
            ThreadArchiveStore(thread_dir).upsert_post_overlay(
                1,
                make_post_overlay("bbcode overlay"),
            )

            with patch(
                "nga_tools.core.paths.get_config",
                return_value=SimpleNamespace(output_dir=str(output_dir)),
            ):
                html_content_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert "bbcode overlay" in html_content_by_lou[1]
        assert "legacy overlay" not in html_content_by_lou[1]
        assert "old materialized html" not in html_content_by_lou[1]
        assert "original body" not in html_content_by_lou[1]

    def test_read_pdf_html_uses_empty_and_existing_image_overlays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_image = output_dir / "images_unique" / "overlay.png"
            unique_image.parent.mkdir(parents=True)
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            image_url = (
                "https://img.nga.178.com/attachments/"
                "mon_202607/12/overlay.png"
            )
            _write_pdf_archive(thread_dir, "original body")
            config = SimpleNamespace(output_dir=str(output_dir))

            with (
                patch("nga_tools.core.paths.get_config", return_value=config),
                patch(
                    "nga_tools.backup.image_store.get_config",
                    return_value=config,
                ),
                patch("nga_tools.backup.pdf._is_long_image", return_value=False),
                patch(
                    "nga_tools.backup.pdf._is_speaker_portrait",
                    return_value=False,
                ),
            ):
                image_store.upsert_image_mapping(image_url, unique_image)
                store = ThreadArchiveStore(thread_dir)
                store.upsert_post_overlay(
                    1,
                    make_post_overlay(f"[img]{image_url}[/img]"),
                )
                image_html_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )
                store.upsert_post_overlay(1, make_post_overlay(""))
                empty_html_by_lou, _folder_pdf, _floor_labels = _read_pdf_html(
                    101,
                    None,
                )

        assert 'src="../../images_unique/overlay.png"' in image_html_by_lou[1]
        assert empty_html_by_lou[1] == ""

    def test_read_pdf_html_uses_lazy_about_blank_data_srcorg(self) -> None:
        image_url = (
            "https://img.nga.178.com/attachments/"
            "mon_202604/24/lsQ2x-ji3jKnT3cS14u-c3.webp"
        )
        with tempfile.TemporaryDirectory() as tmp_dir:
            output_dir = Path(tmp_dir) / "output"
            thread_dir = output_dir / "101_all"
            unique_dir = output_dir / "images_unique"
            unique_dir.mkdir(parents=True)
            unique_image = unique_dir / "hash.png"
            Image.new("RGB", (2, 2), color="white").save(unique_image)
            _write_pdf_archive(
                thread_dir,
                (
                    '<p><img src="about:blank" '
                    f'data-srcorg="{image_url}" '
                    'style="max-height: 1em; min-height: 130px;" /></p>'
                ),
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

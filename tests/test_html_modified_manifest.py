from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from nga_tools.backup import html_modified_manifest


class HtmlModifiedManifestTest:
    def test_completed_post_lous_require_matching_source_hash_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            output_path = html_dir / "post_1.html"
            output_path.write_text("output", encoding="utf-8")
            source_hash = html_modified_manifest.hash_text("source")
            output_hash = html_modified_manifest.hash_text("output")

            html_modified_manifest.write_updated_manifest(
                html_dir,
                previous_entries={},
                source_hash_by_lou={1: source_hash},
                skipped_lous=set(),
                completed_lous={1},
                output_hash_by_lou={1: output_hash},
            )

            entries = html_modified_manifest.load_manifest(html_dir)
            completed_lous = html_modified_manifest.completed_post_lous(
                html_dir,
                {1: source_hash, 2: source_hash},
                entries,
            )
            output_path.unlink()
            completed_lous_after_missing_file = (
                html_modified_manifest.completed_post_lous(
                    html_dir,
                    {1: source_hash},
                    entries,
                )
            )

        assert completed_lous == {1}
        assert completed_lous_after_missing_file == set()

    def test_completed_post_lous_rejects_changed_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            output_path = html_dir / "post_1.html"
            output_path.write_text("output", encoding="utf-8")
            source_hash = html_modified_manifest.hash_text("source")
            output_hash = html_modified_manifest.hash_text("output")

            html_modified_manifest.write_updated_manifest(
                html_dir,
                previous_entries={},
                source_hash_by_lou={1: source_hash},
                skipped_lous=set(),
                completed_lous={1},
                output_hash_by_lou={1: output_hash},
            )
            entries = html_modified_manifest.load_manifest(html_dir)
            output_path.write_text("truncated", encoding="utf-8")

            completed_lous = html_modified_manifest.completed_post_lous(
                html_dir,
                {1: source_hash},
                entries,
            )
            files_exist = html_modified_manifest.manifest_files_exist(
                html_dir,
                entries,
            )

        assert completed_lous == set()
        assert not files_exist

    def test_completed_post_lous_uses_file_stat_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            output_path = html_dir / "post_1.html"
            output_path.write_text("output", encoding="utf-8")
            source_hash = html_modified_manifest.hash_text("source")
            output_hash = html_modified_manifest.hash_text("output")
            html_modified_manifest.write_updated_manifest(
                html_dir,
                previous_entries={},
                source_hash_by_lou={1: source_hash},
                skipped_lous=set(),
                completed_lous={1},
                output_hash_by_lou={1: output_hash},
            )
            entries = html_modified_manifest.load_manifest(html_dir)

            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("stat hit should not read HTML"),
            ):
                completed_lous = html_modified_manifest.completed_post_lous(
                    html_dir,
                    {1: source_hash},
                    entries,
                )

        assert completed_lous == {1}

    def test_completed_post_lous_refreshes_old_entry_stat_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            output_path = html_dir / "post_1.html"
            output_path.write_text("output", encoding="utf-8")
            source_hash = html_modified_manifest.hash_text("source")
            output_hash = html_modified_manifest.hash_text("output")
            html_modified_manifest.manifest_path(html_dir).write_text(
                json.dumps(
                    {
                        "version": html_modified_manifest.HTML_MODIFIED_MANIFEST_VERSION,
                        "modified_generation_version": (
                            html_modified_manifest.HTML_MODIFIED_GENERATION_VERSION
                        ),
                        "algorithm": (
                            html_modified_manifest.HTML_MODIFIED_HASH_ALGORITHM
                        ),
                        "files": {
                            "post_1.html": {
                                "source_hash": source_hash,
                                "output_hash": output_hash,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            entries = html_modified_manifest.load_manifest(html_dir)

            completed_lous = html_modified_manifest.completed_post_lous(
                html_dir,
                {1: source_hash},
                entries,
            )

        entry = entries["post_1.html"]
        assert completed_lous == {1}
        assert entry["output_size"] == len("output")
        assert type(entry["output_mtime_ns"]) is int

    def test_generation_version_mismatch_is_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            manifest_path = html_modified_manifest.manifest_path(html_dir)
            manifest_path.write_text(
                json.dumps(
                    {
                        "version": html_modified_manifest.HTML_MODIFIED_MANIFEST_VERSION,
                        "modified_generation_version": 999,
                        "algorithm": html_modified_manifest.HTML_MODIFIED_HASH_ALGORITHM,
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

            entries = html_modified_manifest.load_manifest(html_dir)

        assert entries == {}

    def test_invalid_manifest_is_empty_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            html_dir = Path(tmp_dir)
            html_modified_manifest.manifest_path(html_dir).write_text(
                "{bad",
                encoding="utf-8",
            )

            with (
                patch("builtins.print"),
                patch("sys.stdout", new_callable=io.StringIO),
            ):
                entries = html_modified_manifest.load_manifest(html_dir)

        assert entries == {}

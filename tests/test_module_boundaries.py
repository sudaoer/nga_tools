from __future__ import annotations

import ast
from pathlib import Path


def _imported_modules(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _class_method_names(module_path: Path, class_name: str) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                child.name
                for child in node.body
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    raise AssertionError(f"未找到类：{class_name}")


def _module_function_names(module_path: Path) -> set[str]:
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    return {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def test_download_runtime_depends_on_types_not_download_coordinator() -> None:
    imports = _imported_modules(
        Path("nga_tools/core/image_download_runtime.py")
    )

    assert "nga_tools.core.download_types" in imports
    assert "nga_tools.core.downloads" not in imports
    assert "nga_tools.network_limits" not in imports


def test_image_validation_does_not_import_image_store() -> None:
    validation_imports = _imported_modules(
        Path("nga_tools/backup/image_validation.py")
    )
    persistence_imports = _imported_modules(
        Path("nga_tools/backup/image_validation_store.py")
    )

    assert "nga_tools.backup.image_store" not in validation_imports
    assert "nga_tools.backup.image_store" not in persistence_imports


def test_removed_compatibility_facades_are_not_reintroduced() -> None:
    removed_paths = (
        Path("nga_tools/utils.py"),
        Path("nga_tools/backup/files.py"),
        Path("nga_tools/web/render.py"),
        Path("nga_tools/web/html_sanitize.py"),
        Path("nga_tools/web/data.py"),
    )

    assert all(not path.exists() for path in removed_paths)


def test_thread_archive_store_does_not_own_state_or_cache_sql() -> None:
    archive_store_path = Path("nga_tools/backup/archive_store.py")
    source = archive_store_path.read_text(encoding="utf-8")
    method_names = _class_method_names(archive_store_path, "ThreadArchiveStore")

    assert "read_backup_processing_snapshot" not in method_names
    assert "read_post_image_reference_cache" not in method_names
    assert "backup_pending_images" not in source
    assert "image_reference_manifest_entries" not in source
    assert "post_image_reference_cache" not in source


def test_thread_archive_store_delegates_main_database_domains() -> None:
    archive_store_path = Path("nga_tools/backup/archive_store.py")
    method_names = _class_method_names(archive_store_path, "ThreadArchiveStore")
    moved_methods = {
        "read_post_overlays",
        "replace_floor_map",
        "upsert_pages",
        "upsert_recovered_posts",
        "read_effective_post_rows",
        "upsert_post_version_selection",
        "read_page_numbers",
    }

    assert method_names.isdisjoint(moved_methods)

    source = archive_store_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(archive_store_path))
    store_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ThreadArchiveStore"
    )
    allowed_sql_methods = {
        "_create_archive_pages_table",
        "_create_post_versions_table",
        "_create_post_latest_metadata_table",
        "_create_post_version_selections_table",
        "_create_floor_map_tables",
        "_create_post_overlays_table",
        "_create_archive_change_state_table",
        "_ensure_schema",
        "_read_archive_change_state",
        "increment_archive_revision",
        "increment_floor_map_revision",
    }
    for node in store_class.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in allowed_sql_methods:
            continue
        method_source = ast.get_source_segment(source, node) or ""
        assert not any(
            keyword in method_source
            for keyword in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        ), node.name


def test_archive_entry_flow_does_not_reabsorb_derived_processing() -> None:
    archive_path = Path("nga_tools/backup/archive.py")
    function_names = _module_function_names(archive_path)
    source = archive_path.read_text(encoding="utf-8")

    assert "_try_incremental_image_reference_update" not in function_names
    assert "_try_processing_state_reuse" not in function_names
    assert "run_full_processing" not in function_names
    assert "archive_processing" in source
    assert "archive_image_processing" in source
    assert "nga_tools.backup.image_pipeline" not in source

    processing_source = Path(
        "nga_tools/backup/archive_processing.py"
    ).read_text(encoding="utf-8")
    assert "archive_image_processing" in processing_source
    assert "nga_tools.backup.image_pipeline" not in processing_source


def test_web_app_assembly_does_not_own_routes_or_data_queries() -> None:
    server_path = Path("nga_tools/web/server.py")
    assert _module_function_names(server_path) == {"create_app", "serve_app"}

    server_source = server_path.read_text(encoding="utf-8")
    assert "nga_tools.web.routes" in server_source
    assert "SELECT " not in server_source

    thread_data_source = Path("nga_tools/web/thread_data.py").read_text(
        encoding="utf-8"
    )
    post_data_source = Path("nga_tools/web/post_data.py").read_text(
        encoding="utf-8"
    )
    assert "BeautifulSoup" not in thread_data_source
    assert "scan_thread_summaries" not in post_data_source


def test_web_tests_are_grouped_by_surface() -> None:
    assert not Path("tests/test_web_viewer.py").exists()
    assert all(
        path.is_file()
        for path in (
            Path("tests/test_web_data.py"),
            Path("tests/test_web_server.py"),
            Path("tests/test_web_database.py"),
            Path("tests/test_web_image_usage.py"),
            Path("tests/test_web_cli.py"),
        )
    )

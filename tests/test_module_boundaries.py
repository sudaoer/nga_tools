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
    )

    assert all(not path.exists() for path in removed_paths)

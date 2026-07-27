from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

from nga_tools.core.sqlite import configure_readonly_connection


def open_readonly_connection(db_path: Path) -> sqlite3.Connection:
    resolved_path = db_path.resolve()
    uri = f"file:{quote(str(resolved_path), safe='/:')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    configure_readonly_connection(connection)
    return connection

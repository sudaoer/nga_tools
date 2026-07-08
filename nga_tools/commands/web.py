from __future__ import annotations

from pathlib import Path

from nga_tools.commands.types import CommandArgs, optional_int, optional_str
from nga_tools.web import DEFAULT_WEB_HOST, DEFAULT_WEB_PORT, DEFAULT_WEB_STATIC_DIR
from nga_tools.web.server import serve_app


def web_serve(args: CommandArgs) -> None:
    host = optional_str(args, "host") or DEFAULT_WEB_HOST
    port = optional_int(args, "port") or DEFAULT_WEB_PORT
    static_dir = Path(optional_str(args, "static_dir") or DEFAULT_WEB_STATIC_DIR)
    serve_app(host=host, port=port, static_dir=static_dir)

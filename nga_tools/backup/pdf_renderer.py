from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any, Sequence, cast

import weasyprint  # pyright: ignore[reportMissingTypeStubs]

from nga_tools.core.image_formats import register_pillow_image_openers


def render_pdf(html_path: Path, output_path: Path) -> None:
    register_pillow_image_openers()
    html = weasyprint.HTML(filename=str(html_path))
    cast(Any, html).write_pdf(str(output_path))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render one NGA backup PDF segment.")
    parser.add_argument("html_path")
    parser.add_argument("output_path")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        render_pdf(Path(args.html_path), Path(args.output_path))
    except Exception as error:
        print(f"ERROR: PDF render failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

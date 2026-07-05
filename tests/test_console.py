from __future__ import annotations

import io
import unittest

from nga_tools.console import InlineProgress


class InlineProgressTest(unittest.TestCase):
    def test_updates_reuse_current_line_and_finish_once(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.update("abcdef")
        progress.update("xy")
        progress.finish()
        progress.finish()

        self.assertEqual(output.getvalue(), "\rabcdef\rxy    \rxy\n")

    def test_finish_without_update_is_noop(self) -> None:
        output = io.StringIO()
        progress = InlineProgress(output)

        progress.finish()

        self.assertEqual(output.getvalue(), "")


if __name__ == "__main__":
    unittest.main()

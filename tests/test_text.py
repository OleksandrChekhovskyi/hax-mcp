from __future__ import annotations

import io
import os
import unittest
from unittest import mock

import support  # noqa: F401
from hax_mcp import text


class TextTests(unittest.TestCase):
    def test_terminal_controls_are_escaped_in_json(self) -> None:
        self.assertEqual(text.dump_json("a\x1b\x9bb"), '"a\\u001b\\u009bb"')

    def test_hax_output_splits_long_lines_except_raw(self) -> None:
        value = "word " * 300
        for raw, split in ((False, True), (True, False)):
            stdout = io.StringIO()
            with mock.patch.dict(os.environ, {"AI_AGENT": "hax"}), mock.patch("sys.stdout", stdout):
                text.write_output(value, raw)
            lines = stdout.getvalue().splitlines()
            self.assertEqual(all(len(x) <= text.AGENT_LINE_BYTES for x in lines), split)

    def test_output_strips_terminal_controls(self) -> None:
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("sys.stdout", stdout):
            text.write_output("a\x1b[2Jb")
        self.assertEqual(stdout.getvalue(), "a[2Jb\n")


if __name__ == "__main__":
    unittest.main()

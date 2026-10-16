from __future__ import annotations

import io
import json
import pathlib
import subprocess
import tempfile
import unittest

from support import ROOT, TOOLS, FakeServer, McpTestCase
from hax_mcp import VERSION


class CommandTests(McpTestCase):
    def test_servers_lists_auth_state(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t")
        servers = {
            "remote": {"url": f"{fake.base}/mcp"},
            "gh": {"url": "https://example.com/mcp", "headers": {"Authorization": "x"}},
            "local": {"command": "npx"},
        }
        self.configure(servers)
        self.assertEqual(
            self.run_cli("servers")[1].splitlines(),
            [
                f"remote  {fake.base}/mcp  [logged in]",
                "gh  https://example.com/mcp  [token]",
                "local  (unsupported: not a remote server)",
            ],
        )

    def test_call_prints_readable_output(self) -> None:
        self.serve()
        code, stdout, _ = self.run_cli("call", "remote", "echo", '{"text": "hi"}')
        self.assertEqual((code, stdout), (0, '{\n  "text": "hi"\n}\n'))

    def test_arguments_can_come_from_stdin(self) -> None:
        self.serve()
        code, stdout, _ = self.run_cli("call", "remote", "echo", "-", stdin=io.StringIO('{"n": 1}'))
        self.assertEqual((code, stdout), (0, '{\n  "n": 1\n}\n'))

    def test_arguments_must_be_an_object(self) -> None:
        self.serve()
        code, _, stderr = self.run_cli("call", "remote", "echo", "[1]")
        self.assertEqual((code, stderr), (1, "arguments must be a JSON object\n"))

    def test_tool_errors_go_to_stderr_with_failure_status(self) -> None:
        self.serve()
        self.assertEqual(self.run_cli("call", "remote", "fail"), (1, "", "boom\n"))
        code, stdout, _ = self.run_cli("call", "remote", "fail", "--json")
        self.assertEqual((code, json.loads(stdout)["isError"]), (1, True))

    def test_show_json_writes_one_document_per_tool(self) -> None:
        self.serve()
        stdout = self.run_cli("show", "remote", "echo", "fail", "--json")[1]
        decoder = json.JSONDecoder()
        first, end = decoder.raw_decode(stdout)
        second, _ = decoder.raw_decode(stdout[end:].lstrip())
        self.assertEqual((first["name"], second["name"]), ("echo", "fail"))


class EditTests(McpTestCase):
    def test_add_reports_tool_count(self) -> None:
        fake = FakeServer()
        self.addCleanup(fake.close)
        code, stdout, _ = self.run_cli("add", "remote", f"{fake.base}/mcp", "-H", "X-Key: k")
        self.assertEqual((code, stdout), (0, f"added remote ({len(TOOLS)} tools)\n"))
        self.assertEqual(self.server().headers, {"X-Key": "k"})

    def test_add_points_at_login_when_required(self) -> None:
        fake = FakeServer(token="unset")
        self.addCleanup(fake.close)
        code, stdout, _ = self.run_cli("add", "remote", f"{fake.base}/mcp")
        self.assertEqual((code, stdout), (0, "added remote; log in with: hax-mcp login remote\n"))

    def test_add_keeps_server_that_fails_its_check(self) -> None:
        stdout = self.run_cli("add", "remote", "http://127.0.0.1:9/mcp")[1]
        self.assertTrue(stdout.startswith("added remote, but listing its tools failed"))
        self.assertEqual(self.server().url, "http://127.0.0.1:9/mcp")

    def test_malformed_header_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as raised:
            self.run_cli("add", "remote", "https://example.com/mcp", "-H", "no colon")
        self.assertEqual(raised.exception.code, 2)

    def test_logout_needs_no_connection_settings(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t")
        self.configure({"remote": {"url": "https://example.com", "headers": {"A": "${UNSET_X}"}}})
        self.assertEqual(self.run_cli("logout", "remote")[:2], (0, "logged out of remote\n"))

    def test_remove_also_removes_login(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t")
        self.assertEqual(self.run_cli("remove", "remote")[:2], (0, "removed remote\n"))
        self.assertEqual(self.credentials(), {})


class LauncherTests(unittest.TestCase):
    def test_launcher_works_through_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            link = pathlib.Path(directory) / "hax-mcp"
            link.symlink_to(ROOT / "hax-mcp")
            result = subprocess.run(
                [str(link), "--version"],
                check=False,
                text=True,
                capture_output=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), f"hax-mcp {VERSION}")

    def test_launcher_ignores_modules_in_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pathlib.Path(directory, "json.py").write_text("raise SystemExit('shadowed')\n")
            result = subprocess.run(
                [str(ROOT / "hax-mcp"), "--version"],
                check=False,
                text=True,
                capture_output=True,
                cwd=directory,
            )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

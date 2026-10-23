from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import shutil
import unittest
from unittest import mock

from support import Json, McpTestCase
from hax_mcp import McpError, config


class ConfigTests(McpTestCase):
    def read(self) -> Json:
        return json.loads(self.config_file().read_text())

    def test_header_values_expand_environment_variables(self) -> None:
        servers = {"s": {"url": "https://example.com/mcp", "headers": {"A": "Bearer ${TOKEN}"}}}
        self.configure(servers)
        with mock.patch.dict(os.environ, {"TOKEN": "secret"}):
            self.assertEqual(self.server("s").headers, {"A": "Bearer secret"})

    def test_missing_variable_is_named(self) -> None:
        self.configure({"s": {"url": "https://example.com/mcp", "headers": {"A": "${UNSET_X}"}}})
        with self.assertRaisesRegex(McpError, "UNSET_X is not set"):
            config.get_server("s")

    def test_local_server_expands_variables_and_finds_the_command(self) -> None:
        entry = {"command": "sh", "args": ["-c", "${ARG}"], "env": {"KEY": "${SECRET}"}}
        self.configure({"s": entry})
        with mock.patch.dict(os.environ, {"ARG": "true", "SECRET": "k"}):
            server = self.local_server("s")
        self.assertEqual(server.command, shutil.which("sh"))
        self.assertEqual((server.args, server.env), (["-c", "true"], {"KEY": "k"}))
        self.assertEqual(server.protocol, "auto")

    def executable(self, path: pathlib.Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    def test_command_is_found_through_the_configured_path(self) -> None:
        tools = self.root / "tools"
        self.executable(tools / "sh")
        self.executable(tools / "only-here")
        env = {"PATH": str(tools)}
        self.configure(
            {"s": {"command": "sh", "env": env}, "t": {"command": "only-here", "env": env}}
        )
        self.assertEqual(self.local_server("s").command, str(tools / "sh"))
        self.assertEqual(self.local_server("t").command, str(tools / "only-here"))

    def test_relative_command_names_a_file_in_the_server_directory(self) -> None:
        project = self.root / "project"
        self.executable(project / ".venv" / "bin" / "python")
        (project / ".git").mkdir()
        (project / "src").mkdir()
        command = "./.venv/bin/python"
        self.configure({"s": {"command": command}, "t": {"command": command, "cwd": str(project)}})
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(project / "src")
        expected = str((project / ".venv" / "bin" / "python").resolve())
        self.assertEqual(self.local_server("s").command, expected)
        self.assertEqual(self.local_server("t").command, expected)
        os.chdir(self.root)
        self.assertEqual(self.local_server("t").command, expected)

    def test_relative_path_entries_give_an_absolute_command(self) -> None:
        self.executable(self.root / "bin" / "tool")
        self.configure({"s": {"command": "tool"}})
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)
        with mock.patch.dict(os.environ, {"PATH": "bin"}):
            command = self.local_server("s").command
        self.assertEqual(command, str((self.root / "bin" / "tool").resolve()))

    def test_local_server_runs_in_the_project_directory(self) -> None:
        project = self.root / "project"
        (project / ".git").mkdir(parents=True)
        (project / "sub").mkdir()
        self.configure({"s": {"command": "sh"}, "t": {"command": "sh", "cwd": str(self.root)}})
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(project / "sub")
        self.assertEqual(self.local_server("s").cwd, str(project.resolve()))
        self.assertEqual(self.local_server("t").cwd, str(self.root.resolve()))

    def test_same_entry_compares_the_entry_as_written(self) -> None:
        entry: Json = {"command": "python"}
        first = config.LocalServer("s", "/a/.venv/bin/python", [], {}, "/a", "auto", entry)
        second = dataclasses.replace(first, command="/b/.venv/bin/python", cwd="/b")
        self.assertTrue(first.same_entry(second))
        edited = dataclasses.replace(second, entry={**entry, "cwd": "/b"})
        self.assertFalse(first.same_entry(edited))

    def test_project_directory_falls_back_to_the_start(self) -> None:
        self.assertEqual(config.project_dir(str(self.root)), str(self.root.resolve()))

    def test_local_server_fields_are_validated(self) -> None:
        cases = {
            "not found in PATH": {"command": "hax-mcp-no-such-command"},
            '"args" must be a list': {"command": "sh", "args": "-c"},
            '"protocol" must be one of': {"command": "sh", "protocol": "2024-01-01"},
            "not both": {"command": "sh", "url": "https://example.com/mcp"},
            "not an executable": {"command": str(self.root)},
        }
        for message, entry in cases.items():
            self.configure({"s": entry})
            with self.subTest(message), self.assertRaisesRegex(McpError, message):
                config.get_server("s")

    def test_runtime_directory_must_be_private(self) -> None:
        self.assertEqual(os.stat(config.runtime_dir()).st_mode & 0o777, 0o700)
        os.chmod(config.runtime_dir(), 0o755)
        with self.assertRaisesRegex(McpError, "only you can access"):
            config.runtime_dir()

    def test_server_urls_must_be_secure(self) -> None:
        self.configure({"s": {"url": "http://example.com/mcp"}})
        with self.assertRaisesRegex(McpError, "non-HTTPS"):
            config.get_server("s")

    def test_file_without_servers_is_empty(self) -> None:
        self.configure({})
        self.config_file().write_text('{"other": 1}')
        self.assertEqual(config.load_config(), {})

    def test_add_stores_entry_and_keeps_other_settings(self) -> None:
        self.configure({})
        self.config_file().write_text(json.dumps({"mcpServers": {}, "comment": "kept"}))
        headers = {"Authorization": "Bearer ${TOKEN}"}
        with mock.patch.dict(os.environ, {"TOKEN": "t"}):
            config.add_server("s", "https://example.com/mcp", headers)
        entry = {"url": "https://example.com/mcp", "headers": headers}
        self.assertEqual(self.read(), {"mcpServers": {"s": entry}, "comment": "kept"})
        self.assertEqual(os.stat(self.config_file()).st_mode & 0o777, 0o600)

    def test_add_rejects_invalid_input(self) -> None:
        self.configure({"s": {"url": "https://example.com/mcp"}})
        with self.assertRaisesRegex(McpError, "already configured"):
            config.add_server("s", "https://example.com/mcp", {})
        with self.assertRaisesRegex(McpError, "server names"):
            config.add_server("a/b", "https://example.com/mcp", {})
        with self.assertRaisesRegex(McpError, "non-HTTPS"):
            config.add_server("t", "http://example.com/mcp", {})

    def test_add_local_stores_command_and_environment(self) -> None:
        config.add_local_server("s", ["npx", "-y", "pkg"], {"TOKEN": "${TOKEN}"})
        entry = {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": "${TOKEN}"}}
        self.assertEqual(self.read(), {"mcpServers": {"s": entry}})

    def test_remove_deletes_only_the_entry(self) -> None:
        self.configure({"s": {"url": "https://a.example"}, "t": {"url": "https://b.example"}})
        config.remove_server("s")
        self.assertEqual(self.read(), {"mcpServers": {"t": {"url": "https://b.example"}}})
        with self.assertRaisesRegex(McpError, "unknown server"):
            config.remove_server("s")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
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
            self.assertEqual(config.get_server("s").headers, {"A": "Bearer secret"})

    def test_missing_variable_is_named(self) -> None:
        self.configure({"s": {"url": "https://example.com/mcp", "headers": {"A": "${UNSET_X}"}}})
        with self.assertRaisesRegex(McpError, "UNSET_X is not set"):
            config.get_server("s")

    def test_stdio_servers_are_rejected(self) -> None:
        self.configure({"s": {"command": "npx"}})
        with self.assertRaisesRegex(McpError, "only remote servers"):
            config.get_server("s")

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

    def test_remove_deletes_only_the_entry(self) -> None:
        self.configure({"s": {"url": "https://a.example"}, "t": {"url": "https://b.example"}})
        config.remove_server("s")
        self.assertEqual(self.read(), {"mcpServers": {"t": {"url": "https://b.example"}}})
        with self.assertRaisesRegex(McpError, "unknown server"):
            config.remove_server("s")


if __name__ == "__main__":
    unittest.main()

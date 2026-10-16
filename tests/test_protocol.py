from __future__ import annotations

import unittest
from unittest import mock

from support import TOOLS, Json, McpTestCase
from hax_mcp import LoginRequired, McpError, protocol


class SessionTests(McpTestCase):
    def session(self) -> protocol.Session:
        session = protocol.Session(self.server())
        self.addCleanup(session.__exit__)
        return session

    def test_modern_server_gets_one_request_with_routing_headers(self) -> None:
        fake = self.serve()
        result = protocol.call_tool(self.session(), "echo", {"text": "hi"})
        self.assertEqual(result["content"], [{"type": "text", "text": '{"text": "hi"}'}])
        self.assertEqual(fake.methods(), ["tools/call"])
        headers, body = fake.log[0][1], fake.log[0][2]
        self.assertEqual((headers["mcp-method"], headers["mcp-name"]), ("tools/call", "echo"))
        meta = body["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28")

    def test_legacy_server_gets_a_session(self) -> None:
        fake = self.serve(legacy=True)
        with protocol.Session(self.server()) as session:
            self.assertEqual(len(protocol.list_tools(session)), len(TOOLS))
            protocol.list_tools(session)
        self.assertEqual(
            fake.methods(),
            [
                "tools/list",
                "initialize",
                "notifications/initialized",
                "tools/list",
                "tools/list",
                "DELETE",
            ],
        )
        self.assertEqual(fake.log[3][1]["mcp-protocol-version"], "2025-11-25")
        self.assertEqual(fake.log[5][1]["mcp-session-id"], "session-1")

    def test_unsupported_version_listing_only_legacy_ones_gets_a_session(self) -> None:
        fake = self.serve(legacy=True)
        error = {"code": -32022, "message": "Unsupported", "data": {"supported": ["2025-11-25"]}}
        fake.legacy_rejection = {"jsonrpc": "2.0", "id": 1, "error": error}
        self.assertEqual(len(protocol.list_tools(self.session())), len(TOOLS))
        self.assertIn("initialize", fake.methods())

    def test_unsupported_version_listing_modern_ones_is_an_error(self) -> None:
        fake = self.serve(legacy=True)
        error = {"code": -32022, "message": "Unsupported", "data": {"supported": ["2026-07-28"]}}
        fake.legacy_rejection = {"jsonrpc": "2.0", "id": 1, "error": error}
        with self.assertRaisesRegex(McpError, "Unsupported"):
            protocol.list_tools(self.session())
        self.assertNotIn("initialize", fake.methods())

    def test_tool_list_reports_the_page_limit(self) -> None:
        fake = self.serve()
        fake.page_size = 1
        with mock.patch.object(protocol, "MAX_TOOL_PAGES", 2):
            with self.assertRaisesRegex(McpError, "more than 2 pages"):
                protocol.list_tools(self.session())

    def test_errors_after_modern_replies_do_not_downgrade(self) -> None:
        fake = self.serve()
        session = self.session()
        protocol.list_tools(session)
        with self.assertRaisesRegex(McpError, r"Invalid params \(code -32602\)"):
            protocol.call_tool(session, "invalid", {})
        self.assertEqual(fake.methods(), ["tools/list", "tools/call"])

    def test_event_stream_replies_skip_notifications(self) -> None:
        self.serve(sse=True)
        result = protocol.call_tool(self.session(), "echo", {})
        self.assertEqual(result["content"], [{"type": "text", "text": "{}"}])

    def test_tool_list_follows_cursors(self) -> None:
        fake = self.serve()
        fake.page_size = 3
        tools = protocol.list_tools(self.session())
        self.assertEqual([tool["name"] for tool in tools], [tool["name"] for tool in TOOLS])
        self.assertEqual(fake.methods(), ["tools/list", "tools/list"])

    def test_header_mismatch_retries_with_param_headers(self) -> None:
        fake = self.serve()
        result = protocol.call_tool(self.session(), "regional", {"region": "eu"})
        self.assertEqual(result["content"], [{"type": "text", "text": "ok"}])
        self.assertEqual(fake.methods(), ["tools/call", "tools/list", "tools/call"])

    def test_input_requests_are_reported(self) -> None:
        self.serve()
        with self.assertRaisesRegex(McpError, "cannot provide: Confirm deletion"):
            protocol.call_tool(self.session(), "needs_input", {})

    def test_missing_login_names_the_login_command(self) -> None:
        self.serve(token="unset")
        with self.assertRaisesRegex(LoginRequired, "must run `hax-mcp login remote`"):
            protocol.list_tools(self.session())

    def test_errors_carry_server_message(self) -> None:
        self.serve()
        with self.assertRaisesRegex(McpError, r"remote: Tool nope not found \(code -32602\)"):
            protocol.call_tool(self.session(), "nope", {})


class ToolTests(unittest.TestCase):
    def test_unknown_tool_points_at_tool_list(self) -> None:
        tools: list[Json] = [{"name": "a"}]
        with self.assertRaisesRegex(McpError, "see hax-mcp tools s"):
            protocol.find_tool(tools, "b", "s")

    def test_header_values_use_base64_when_unsafe(self) -> None:
        self.assertEqual(protocol.header_value("us-west1"), "us-west1")
        self.assertEqual(protocol.header_value("Hello, 世界"), "=?base64?SGVsbG8sIOS4lueVjA==?=")
        self.assertEqual(protocol.header_value(" padded "), "=?base64?IHBhZGRlZCA=?=")
        self.assertEqual(protocol.header_value("=?base64?x?="), "=?base64?PT9iYXNlNjQ/eD89?=")

    def test_param_headers_follow_property_paths(self) -> None:
        schema: Json = {
            "properties": {
                "region": {"type": "string", "x-mcp-header": "Region"},
                "options": {"properties": {"dry": {"type": "boolean", "x-mcp-header": "Dry"}}},
                "absent": {"type": "string", "x-mcp-header": "Absent"},
            }
        }
        arguments = {"region": "eu", "options": {"dry": True}}
        self.assertEqual(
            protocol.param_headers(schema, arguments),
            {"Mcp-Param-Region": "eu", "Mcp-Param-Dry": "true"},
        )

    def test_invalid_header_annotation_is_an_error(self) -> None:
        schema: Json = {"properties": {"a": {"type": "string", "x-mcp-header": "A\r\nB"}}}
        with self.assertRaisesRegex(McpError, "invalid x-mcp-header"):
            protocol.param_headers(schema, {"a": "x"})


if __name__ == "__main__":
    unittest.main()

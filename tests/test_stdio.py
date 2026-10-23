from __future__ import annotations

import concurrent.futures
import json
import os
import time
import unittest
from unittest import mock

from support import Json, McpTestCase, process_gone
from hax_mcp import McpError, protocol, stdio


class StdioTests(McpTestCase):
    def launch(self, mode: str = "legacy", **fields: Json) -> stdio.ServerProcess:
        self.configure({"local": self.local_entry(mode, **fields)})
        process = stdio.launch(self.local_server(), dict(os.environ))
        self.addCleanup(process.close)
        return process

    def call_text(self, process: stdio.ServerProcess, tool: str, arguments: Json = None) -> str:
        result: Json = protocol.call_tool(process, tool, arguments or {})
        return result["content"][0]["text"]

    def test_legacy_server_refusing_the_probe_gets_initialize(self) -> None:
        process = self.launch("legacy")
        self.assertEqual(self.call_text(process, "count"), "1")
        self.assertEqual(
            self.methods_received(),
            ["server/discover", "initialize", "notifications/initialized", "tools/call"],
        )
        self.assertEqual(process.version, "2025-11-25")

    def test_modern_server_gets_requests_without_a_handshake(self) -> None:
        process = self.launch("modern")
        self.assertEqual(self.call_text(process, "count"), "1")
        self.assertEqual(self.methods_received(), ["server/discover", "tools/call"])
        meta = self.received()[1]["params"]["_meta"]
        self.assertEqual(meta["io.modelcontextprotocol/protocolVersion"], "2026-07-28")

    def test_server_exiting_on_the_probe_is_started_again_without_it(self) -> None:
        process = self.launch("exit-on-discover")
        self.assertEqual(self.call_text(process, "count"), "1")
        self.assertEqual(
            self.methods_received(),
            ["server/discover", "initialize", "notifications/initialized", "tools/call"],
        )

    def test_legacy_protocol_setting_skips_the_probe(self) -> None:
        protocol.list_tools(self.launch("legacy", protocol="legacy"))
        self.assertEqual(
            self.methods_received(), ["initialize", "notifications/initialized", "tools/list"]
        )

    def test_pinned_version_rejects_a_legacy_server(self) -> None:
        with self.assertRaisesRegex(McpError, "Invalid request parameters"):
            self.launch("legacy", protocol="2026-07-28")

    def test_silent_server_times_out_at_startup(self) -> None:
        with mock.patch.object(stdio, "STARTUP_TIMEOUT_S", 0.2):
            with self.assertRaisesRegex(McpError, "no response to server/discover within 0.2 s"):
                self.launch("silent")

    def test_concurrent_requests_get_their_own_replies(self) -> None:
        process = self.launch()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            counts = list(pool.map(lambda _: self.call_text(process, "count"), range(32)))
        self.assertEqual(sorted(counts, key=int), [str(n) for n in range(1, 33)])

    def test_server_requests_are_answered(self) -> None:
        replies = json.loads(self.call_text(self.launch(), "ping_client"))
        self.assertEqual(replies[0], {"jsonrpc": "2.0", "id": "s1", "result": {}})
        self.assertEqual(replies[1]["error"]["code"], -32601)

    def test_exit_is_reported_with_status_and_stderr(self) -> None:
        process = self.launch()
        message = "server exited with status 3; stderr: crashing on purpose"
        with self.assertRaisesRegex(stdio.ServerExited, message):
            protocol.call_tool(process, "crash", {})
        with self.assertRaisesRegex(stdio.ServerExited, message):
            protocol.list_tools(process)

    def test_reply_written_just_before_exit_is_delivered(self) -> None:
        process = self.launch()
        receive = stdio.ServerProcess.receive

        def slow_receive(server: stdio.ServerProcess, line: bytes) -> None:
            time.sleep(0.2)  # the server exits while its reply is still being read
            receive(server, line)

        with mock.patch.object(stdio.ServerProcess, "receive", slow_receive):
            self.assertEqual(self.call_text(process, "reply_and_exit"), "bye")

    def test_request_abandoned_in_flight_is_cancelled(self) -> None:
        process = self.launch()
        checks = iter([False])
        with self.assertRaises(stdio.Cancelled):
            process.send("tools/call", {"name": "hang"}, cancelled=lambda: next(checks, True))
        self.call_text(process, "count")
        hang, cancel = self.received()[3:5]
        self.assertEqual(cancel["method"], "notifications/cancelled")
        self.assertEqual(cancel["params"]["requestId"], hang["id"])

    def test_request_abandoned_before_sending_is_not_sent(self) -> None:
        process = self.launch()
        with self.assertRaises(stdio.Cancelled):
            process.send("tools/call", {"name": "count"}, cancelled=lambda: True)
        self.assertEqual(self.call_text(process, "count"), "1")

    def test_server_gets_its_directory_and_environment(self) -> None:
        process = self.launch(cwd=str(self.root))
        self.assertEqual(self.call_text(process, "cwd"), str(self.root.resolve()))
        log = self.call_text(process, "env", {"name": "FAKE_MCP_LOG"})
        self.assertEqual(log, str(self.root / "messages.jsonl"))

    def test_missing_executable_is_reported(self) -> None:
        self.configure({"local": self.local_entry()})
        server = self.local_server()
        server.command = str(self.root / "missing")
        with self.assertRaisesRegex(McpError, "cannot run .*missing"):
            stdio.launch(server, dict(os.environ))

    def test_close_stops_helpers_that_outlive_the_server(self) -> None:
        process = self.launch()
        helper = int(self.call_text(process, "spawn_helper"))
        with mock.patch.object(stdio, "STOP_GRACE_S", 0.2):
            process.close()
        self.assertTrue(process_gone(helper))

    def test_exit_stops_helpers_left_behind(self) -> None:
        process = self.launch()
        helper = int(self.call_text(process, "spawn_helper"))
        with mock.patch.object(stdio, "STOP_GRACE_S", 0.2):
            with self.assertRaises(stdio.ServerExited):
                protocol.call_tool(process, "crash", {})
            self.assertTrue(process_gone(helper))

    def test_close_stops_the_process(self) -> None:
        process = self.launch()
        process.close()
        self.assertIsNotNone(process.process.returncode)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import dataclasses
import os
import pathlib
import sys
import threading
import time
import unittest
from unittest import mock

from support import Json, McpTestCase, process_gone
from hax_mcp import McpError, broker, local, protocol


class BrokerTests(McpTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.configure_local()
        self.addCleanup(os.chdir, os.getcwd())
        os.chdir(self.root)

    def call(self, tool: str) -> tuple[int, str, str]:
        return self.run_cli("call", "local", tool)

    def test_server_keeps_its_state_between_commands(self) -> None:
        self.assertEqual(self.call("count"), (0, "1\n", ""))
        self.assertEqual(self.call("count"), (0, "2\n", ""))
        self.assertEqual(self.methods_received().count("initialize"), 1)
        status = self.run_cli("status")[1].splitlines()
        self.assertEqual(status[0].split(), ["SERVER", "DIRECTORY", "PID", "MEMORY", "UP", "IDLE"])
        self.assertEqual(status[1].split()[:2], ["local", str(self.root.resolve())])
        self.assertEqual(len(status), 2)

    def test_each_project_gets_its_own_instance(self) -> None:
        first, second = self.root / "a", self.root / "b"
        for directory in (first, second):
            (directory / ".git").mkdir(parents=True)
            os.chdir(directory)
            self.assertEqual(self.call("count")[1], "1\n")
        self.assertEqual(len(local.running()), 2)
        code, stdout, _ = self.run_cli("stop", "local", "-C", str(first))
        self.assertEqual((code, stdout), (0, f"stopped local in {first.resolve()}\n"))
        self.assertEqual([item["cwd"] for item in local.running()], [str(second.resolve())])
        stdout = self.run_cli("stop", "local", "-C", str(first))[1]
        self.assertEqual(stdout, f"local is not running in {first.resolve()}\n")

    def test_restart_after_an_exit_is_reported(self) -> None:
        self.call("count")
        code, _, stderr = self.call("crash")
        self.assertEqual(code, 1)
        self.assertIn("server exited with status 3", stderr)
        code, stdout, stderr = self.call("count")
        self.assertEqual((code, stdout), (0, "1\n"))
        self.assertIn("restarted the server, which exited with status 3", stderr)

    def test_changed_configuration_restarts_the_server(self) -> None:
        self.call("count")
        entry = self.local_entry()
        entry["env"]["EXTRA"] = "1"
        self.configure({"local": entry})
        code, stdout, stderr = self.call("count")
        self.assertEqual((code, stdout), (0, "1\n"))
        self.assertIn("restarted the server with its changed configuration", stderr)
        self.assertEqual(len(local.running()), 1)

    def test_rotated_variable_restarts_the_server(self) -> None:
        entry = self.local_entry()
        entry["env"]["API_TOKEN"] = "${TOKEN}"
        self.configure({"local": entry})
        arguments = '{"name": "API_TOKEN"}'
        with mock.patch.dict(os.environ, {"TOKEN": "old"}):
            self.assertEqual(self.run_cli("call", "local", "env", arguments), (0, "old\n", ""))
        with mock.patch.dict(os.environ, {"TOKEN": "new"}):
            code, stdout, stderr = self.run_cli("call", "local", "env", arguments)
        self.assertEqual((code, stdout), (0, "new\n"))
        self.assertIn("restarted the server with its changed environment", stderr)

    def test_projects_finding_the_command_differently_keep_their_servers(self) -> None:
        entry = self.local_entry()
        entry["command"] = "fake-python"
        self.configure({"local": entry})
        for name in ("a", "b"):
            (self.root / name / ".git").mkdir(parents=True)
            shim = self.root / name / "bin" / "fake-python"
            shim.parent.mkdir()
            shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
            shim.chmod(0o755)

        def count_in(name: str) -> tuple[int, str, str]:
            os.chdir(self.root / name)
            path = f"{self.root / name / 'bin'}{os.pathsep}{os.environ['PATH']}"
            with mock.patch.dict(os.environ, {"PATH": path}):
                return self.call("count")

        results = [count_in(name) for name in ("a", "b", "a")]
        self.assertEqual(results, [(0, "1\n", ""), (0, "1\n", ""), (0, "2\n", "")])

    def test_switching_to_a_shared_directory_stops_project_instances(self) -> None:
        self.call("count")
        shared = self.root / "shared"
        shared.mkdir()
        self.configure({"local": self.local_entry(cwd=str(shared))})
        code, stdout, stderr = self.call("count")
        self.assertEqual((code, stdout), (0, "1\n"))
        self.assertIn("restarted the server with its changed configuration", stderr)
        self.assertEqual([item["cwd"] for item in local.running()], [str(shared.resolve())])

    def test_stop_without_arguments_ends_the_broker(self) -> None:
        self.call("count")
        self.assertEqual(self.run_cli("stop"), (0, f"stopped local in {self.root.resolve()}\n", ""))
        self.assertFalse(os.path.exists(broker.socket_path()))
        self.assertEqual(self.run_cli("status")[1], "no local servers are running\n")
        self.assertEqual(self.run_cli("stop")[1], "no local servers are running\n")

    def test_broker_exits_once_its_socket_is_gone(self) -> None:
        self.call("count")
        link = local.open_broker()
        pid: Json = link.call({"op": "hello"})["pid"]
        link.close()
        os.unlink(broker.socket_path())
        self.assertTrue(process_gone(pid))

    def test_status_does_not_start_a_broker(self) -> None:
        self.assertEqual(self.run_cli("status"), (0, "no local servers are running\n", ""))
        self.assertFalse(os.path.exists(broker.socket_path()))

    def test_broker_running_other_code_is_replaced(self) -> None:
        self.call("count")
        with mock.patch("hax_mcp.local.build_id", return_value="other"):
            code, stdout, stderr = self.call("count")
        self.assertEqual((code, stdout), (0, "1\n"))
        self.assertIn("restarted local servers after an update: local", stderr)

    def test_client_hanging_up_cancels_its_request(self) -> None:
        link = local.open_broker()
        server = dataclasses.asdict(self.local_server())
        request: Json = {
            "op": "request",
            "server": server,
            "environ": dict(os.environ),
            "method": "tools/call",
            "params": {"name": "hang"},
        }
        broker.write_message(link.writer, request)
        deadline = time.monotonic() + 5
        while "tools/call" not in self.methods_received() and time.monotonic() < deadline:
            time.sleep(0.02)
        link.close()
        while "notifications/cancelled" not in self.methods_received():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)

    def start_slowly(self, calls: int = 1) -> tuple[list[threading.Thread], list[McpError], int]:
        """Begin calls to a server that never finishes starting; return once it runs and the
        other calls have had time to queue behind the first."""
        self.configure_local("silent")
        server, errors = self.local_server(), []

        def call() -> None:
            try:
                with local.LocalSession(server) as session:
                    protocol.list_tools(session)
            except McpError as error:
                errors.append(error)

        threads = [threading.Thread(target=call) for _ in range(calls)]
        for thread in threads:
            thread.start()
            # Bounded, so a failing test does not wait out a launch before cleanup stops it.
            self.addCleanup(thread.join, 5)
        deadline = time.monotonic() + 5
        while not self.received():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        time.sleep(0.2)
        return threads, errors, self.received()[0]["pid"]

    def test_stop_ends_a_server_still_starting(self) -> None:
        threads, errors, pid = self.start_slowly()
        self.assertEqual(local.stop("local"), [{"name": "local", "cwd": str(self.root.resolve())}])
        threads[0].join(5)
        self.assertEqual([str(error) for error in errors], ["local: stopped while starting"])
        self.assertTrue(process_gone(pid))
        self.assertEqual(local.running(), [])

    def test_stop_fails_requests_queued_behind_a_start(self) -> None:
        threads, errors, _ = self.start_slowly(calls=3)
        started = time.monotonic()
        local.stop("local")
        for thread in threads:
            thread.join(5)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(
            sorted(str(error) for error in errors),
            ["local: stopped while starting"] + ["local: the server was stopped"] * 2,
        )
        self.assertEqual(self.methods_received().count("(started)"), 1)

    def test_requests_abandoned_during_startup_are_not_sent(self) -> None:
        gate = self.root / "gate"
        entry = self.local_entry("gated")
        entry["env"]["FAKE_MCP_GATE"] = str(gate)
        self.configure({"local": entry})
        server = dataclasses.asdict(self.local_server())
        request: Json = {
            "op": "request",
            "server": server,
            "environ": dict(os.environ),
            "method": "tools/call",
            "params": {"name": "count"},
        }
        # The first starts the server; the second queues behind that launch.
        links = [local.open_broker() for _ in range(2)]
        for link in links:
            broker.write_message(link.writer, request)
        deadline = time.monotonic() + 5
        while not self.received():
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        for link in links:
            link.close()
        time.sleep(0.3)
        gate.touch()
        self.assertEqual(self.call("count"), (0, "1\n", ""))
        self.assertEqual(self.methods_received().count("tools/call"), 1)

    def test_stopping_everything_ends_servers_still_starting(self) -> None:
        threads, _, pid = self.start_slowly()
        self.assertEqual(len(local.stop()), 1)
        threads[0].join(5)
        self.assertTrue(process_gone(pid))
        self.assertFalse(os.path.exists(broker.socket_path()))

    def test_broker_runs_as_a_named_module(self) -> None:
        self.call("count")
        link = local.open_broker()
        self.addCleanup(link.close)
        pid = link.call({"op": "hello"})["pid"]
        command = pathlib.Path(f"/proc/{pid}/cmdline")
        if not command.exists():
            self.skipTest("needs /proc")
        arguments = command.read_bytes().split(b"\0")[1:-1]
        self.assertEqual(arguments, [b"-E", b"-s", b"-m", b"hax_mcp.broker"])

    def test_remove_stops_the_server(self) -> None:
        self.call("count")
        self.run_cli("remove", "local")
        self.assertEqual(local.running(), [])

    def test_login_is_refused(self) -> None:
        code, _, stderr = self.run_cli("login", "local")
        self.assertEqual((code, stderr), (1, "local is a local server, which needs no login\n"))


class AddTests(McpTestCase):
    def test_add_checks_the_server_without_leaving_it_running(self) -> None:
        entry = self.local_entry()
        command = [entry["command"], *entry["args"]]
        log = f"FAKE_MCP_LOG={entry['env']['FAKE_MCP_LOG']}"
        code, stdout, _ = self.run_cli("add", "-e", log, "local", "--", *command)
        self.assertEqual((code, stdout), (0, "added local (8 tools)\n"))
        self.assertEqual(self.local_server().env, entry["env"])
        self.assertFalse(os.path.exists(broker.socket_path()))
        self.assertEqual(self.run_cli("servers")[1], f"local  {' '.join(command)}\n")

    def test_add_rejects_options_of_the_other_kind(self) -> None:
        code, _, stderr = self.run_cli("add", "-H", "A: b", "local", "sh")
        self.assertEqual((code, stderr), (1, "-H applies only to remote servers\n"))
        code, _, stderr = self.run_cli("add", "remote", "https://example.com/mcp", "extra")
        self.assertEqual((code, stderr), (1, "a remote server takes a URL alone\n"))
        self.assertFalse(pathlib.Path(self.config_file()).exists())


if __name__ == "__main__":
    unittest.main()

"""JSON-RPC with one MCP server running as a child process on its stdin and stdout."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Final, Optional

from . import PROTOCOL_VERSION, JsonObject, JsonValue, McpError
from .config import LocalServer
from .protocol import CLIENT_INFO, LEGACY_VERSION, message_result, modern_request
from .text import one_line
from .transport import MAX_RESPONSE_BYTES

METHOD_NOT_FOUND: Final = -32601
POLL_S: Final = 0.1
STARTUP_TIMEOUT_S: Final = 60
STDERR_TAIL_BYTES: Final = 8192
STDERR_TAIL_CHARS: Final = 300
STOP_GRACE_S: Final = 2


class ServerExited(McpError):
    pass


class DiscoveryExited(ServerExited):
    pass


class Cancelled(Exception):
    """The caller stopped waiting, and the server was asked to cancel the request."""


class ServerProcess:
    """One running server, which several threads may send requests to at once.

    The process leads its own process group, so stopping it also stops helpers it started, such
    as the node process behind npx.
    """

    def __init__(self, server: LocalServer, environ: dict[str, str]) -> None:
        self.server = server
        try:
            self.process = subprocess.Popen(
                [server.command, *server.args],
                cwd=server.cwd,
                env={**environ, **server.env},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as error:
            reason = error.strerror or str(error)
            raise McpError(f"{server.name}: cannot run {server.command}: {reason}") from error
        self.modern = False
        self.version: Optional[str] = None
        self.started = time.time()
        self.last_used = self.started
        self.exited: Optional[str] = None
        self.finishing = False
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.pending: dict[int, queue.Queue[Optional[JsonObject]]] = {}
        self.last_id = 0
        self.stderr_tail = bytearray()
        self.stderr_reader = threading.Thread(target=self.read_stderr, daemon=True)
        self.stderr_reader.start()
        self.stdout_reader = threading.Thread(target=self.read_stdout, daemon=True)
        self.stdout_reader.start()
        threading.Thread(target=self.watch, daemon=True).start()

    @property
    def name(self) -> str:
        return self.server.name

    def take_id(self) -> int:
        with self.lock:
            self.last_id += 1
            return self.last_id

    def start(self, discover: bool, cancelled: Optional[Callable[[], bool]] = None) -> None:
        """Negotiate the protocol version: 2026-07-28 when the server answers server/discover
        with it, otherwise the initialize handshake. Legacy servers answer the unfamiliar method
        with differing errors, so any error counts as a refusal."""
        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        if discover:
            message, _ = modern_request(self.take_id(), "server/discover", {})
            try:
                reply = self.exchange(message, deadline, cancelled)
            except ServerExited as error:
                raise DiscoveryExited(str(error)) from error
            result = reply.get("result")
            versions = result.get("supportedVersions") if isinstance(result, dict) else None
            if isinstance(versions, list) and PROTOCOL_VERSION in versions:
                self.modern = True
                self.version = PROTOCOL_VERSION
                return
            if self.server.protocol == PROTOCOL_VERSION:
                message_result(reply, self.name)
                raise McpError(f"{self.name}: server does not support MCP {PROTOCOL_VERSION}")
        params: JsonObject = {
            "protocolVersion": LEGACY_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        message = {"jsonrpc": "2.0", "id": self.take_id(), "method": "initialize", "params": params}
        result = message_result(self.exchange(message, deadline, cancelled), self.name)
        version = result.get("protocolVersion")
        self.version = version if isinstance(version, str) else LEGACY_VERSION
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def send(
        self,
        method: str,
        params: JsonObject,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> JsonObject:
        """Send a request in the negotiated form and return the raw response message."""
        request_id = self.take_id()
        if self.modern:
            message, _ = modern_request(request_id, method, params)
        else:
            message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        self.last_used = time.time()
        try:
            return self.exchange(message, cancelled=cancelled)
        finally:
            self.last_used = time.time()

    def request(
        self, method: str, params: JsonObject, headers: Optional[dict[str, str]] = None
    ) -> JsonObject:
        return message_result(self.send(method, params), self.name)

    def exchange(
        self,
        message: JsonObject,
        deadline: Optional[float] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> JsonObject:
        request_id = message["id"]
        assert isinstance(request_id, int)
        if cancelled is not None and cancelled():
            raise Cancelled
        slot: queue.Queue[Optional[JsonObject]] = queue.Queue(1)
        with self.lock:
            if self.exited is not None:
                raise ServerExited(self.exit_text())
            self.pending[request_id] = slot
        try:
            self.write(message)
            while True:
                try:
                    reply = slot.get(timeout=POLL_S)
                except queue.Empty:
                    if cancelled is not None and cancelled():
                        self.cancel(request_id, "the client went away")
                        raise Cancelled from None
                    if deadline is not None and time.monotonic() >= deadline:
                        self.cancel(request_id, "timed out")
                        method = one_line(message.get("method"))
                        raise McpError(
                            f"{self.name}: no response to {method} within {STARTUP_TIMEOUT_S} s"
                        ) from None
                    continue
                if reply is None:
                    raise ServerExited(self.exit_text())
                return reply
        finally:
            with self.lock:
                self.pending.pop(request_id, None)

    def cancel(self, request_id: int, reason: str) -> None:
        params: JsonObject = {"requestId": request_id, "reason": reason}
        with contextlib.suppress(ServerExited):
            self.write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": params})

    def write(self, message: JsonObject) -> None:
        data = json.dumps(message).encode() + b"\n"
        stdin = self.process.stdin
        assert stdin is not None
        try:
            with self.write_lock:
                stdin.write(data)
                stdin.flush()
        except (OSError, ValueError) as error:
            raise ServerExited(self.exit_text()) from error

    def read_stdout(self) -> None:
        stdout = self.process.stdout
        assert stdout is not None
        problem = None
        with contextlib.suppress(OSError, ValueError):
            while True:
                line = stdout.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    break
                if len(line) > MAX_RESPONSE_BYTES:
                    problem = "sent a message over 16 MiB"
                    self.signal(signal.SIGKILL)
                    break
                self.receive(line)
        stdout.close()
        self.finish(problem)

    def receive(self, line: bytes) -> None:
        try:
            message: JsonValue = json.loads(line)
        except ValueError:
            # Some servers log to stdout; such lines are not messages.
            return
        if not isinstance(message, dict):
            return
        method = message.get("method")
        if isinstance(method, str):
            if "id" in message:
                self.answer(message["id"], method)
            return
        request_id = message.get("id")
        if not isinstance(request_id, int):
            return
        with self.lock:
            slot = self.pending.pop(request_id, None)
        if slot is not None:
            slot.put(message)

    def answer(self, request_id: JsonValue, method: str) -> None:
        """Reply to a request from the server, none of which needs input hax-mcp could give."""
        reply: JsonObject = {"jsonrpc": "2.0", "id": request_id}
        if method == "ping":
            reply["result"] = {}
        else:
            text = f"hax-mcp does not support {method}"
            reply["error"] = {"code": METHOD_NOT_FOUND, "message": text}
        with contextlib.suppress(ServerExited):
            self.write(reply)

    def read_stderr(self) -> None:
        stderr = self.process.stderr
        assert stderr is not None
        with contextlib.suppress(OSError, ValueError), stderr:
            while True:
                chunk = os.read(stderr.fileno(), 4096)
                if not chunk:
                    return
                with self.lock:
                    self.stderr_tail += chunk
                    del self.stderr_tail[:-STDERR_TAIL_BYTES]

    def watch(self) -> None:
        """Notice the server exiting even while helpers keep its output pipes open."""
        self.process.wait()
        # Replies written just before the exit may still be in the pipe; read them first.
        self.stdout_reader.join(STOP_GRACE_S)
        self.finish(None)

    def finish(self, problem: Optional[str]) -> None:
        """Fail pending requests once the server exits or its output ends, then stop whatever is
        left of its process group."""
        with self.lock:
            if self.finishing:
                return
            self.finishing = True
        status = None
        with contextlib.suppress(subprocess.TimeoutExpired):
            status = self.process.wait(STOP_GRACE_S)
        # Final stderr arrives at once unless a helper holds the pipe, so wait only briefly.
        self.stderr_reader.join(POLL_S)
        if problem is None:
            if status is None:
                problem = "closed its output"
            elif status < 0:
                problem = f"was killed by {signal_name(-status)}"
            else:
                problem = f"exited with status {status}"
        with self.lock:
            text = one_line(self.stderr_tail.decode("utf-8", "replace"))
            if len(text) > STDERR_TAIL_CHARS:
                text = "…" + text[-STDERR_TAIL_CHARS + 1 :]
            self.exited = problem + (f"; stderr: {text}" if text else "")
            pending = list(self.pending.values())
            self.pending.clear()
        for slot in pending:
            slot.put(None)
        self.close()

    def exit_text(self) -> str:
        return f"{self.name}: server {self.exited or 'exited'}"

    def signal(self, number: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.process.pid, number)

    def group_alive(self) -> bool:
        try:
            os.killpg(self.process.pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        return True

    def wait_stopped(self, timeout: float) -> bool:
        """Wait until the server and every process left in its group have exited."""
        deadline = time.monotonic() + timeout
        while self.process.poll() is None or self.group_alive():
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL_S / 2)
        return True

    def close(self) -> None:
        """Stop the server as the MCP specification describes: close its input, then signal its
        process group. Helpers may outlive the server itself, so its exit alone is not enough."""
        # A writer blocked on a server that stopped reading holds the lock; signals still work.
        if self.write_lock.acquire(timeout=STOP_GRACE_S):
            try:
                with contextlib.suppress(OSError, ValueError):
                    assert self.process.stdin is not None
                    self.process.stdin.close()
            finally:
                self.write_lock.release()
        for number in (None, signal.SIGTERM, signal.SIGKILL):
            if number is not None:
                self.signal(number)
            if self.wait_stopped(STOP_GRACE_S):
                return


def signal_name(number: int) -> str:
    try:
        return signal.Signals(number).name
    except ValueError:
        return f"signal {number}"


def launch(
    server: LocalServer,
    environ: dict[str, str],
    cancelled: Optional[Callable[[], bool]] = None,
) -> ServerProcess:
    """Start a server and negotiate its protocol version, starting it again with the initialize
    handshake alone if it exits on the server/discover probe. Raises Cancelled, with the server
    stopped, once `cancelled` returns true."""
    discover = server.protocol != "legacy"
    while True:
        process = ServerProcess(server, environ)
        try:
            process.start(discover, cancelled)
            return process
        except DiscoveryExited:
            process.close()
            if server.protocol != "auto":
                raise
            discover = False
        except BaseException:
            process.close()
            raise

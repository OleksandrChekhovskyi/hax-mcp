"""Use local servers through the broker, which keeps them running between commands."""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from typing import Final, Optional, cast

from . import JsonObject, JsonValue, McpError
from .broker import build_id, lock_path, log_path, read_message, socket_path, write_message
from .config import LocalServer, project_dir, state_dir
from .protocol import message_result
from .text import one_line

SOURCE_DIR: Final = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
START_TIMEOUT_S: Final = 10


class BrokerLink:
    def __init__(self, connection: socket.socket) -> None:
        self.connection = connection
        self.reader = connection.makefile("rb")
        self.writer = connection.makefile("wb")

    def call(self, message: JsonObject) -> JsonObject:
        try:
            write_message(self.writer, message)
            reply = read_message(self.reader)
        except (OSError, ValueError) as error:
            raise McpError(f"lost the connection to the broker: {error}") from error
        if reply is None:
            raise McpError("the broker closed the connection")
        error = reply.get("error")
        if isinstance(error, str):
            raise McpError(error)
        return reply

    def close(self) -> None:
        for stream in (self.reader, self.writer, self.connection):
            with contextlib.suppress(OSError):
                stream.close()


def try_connect() -> Optional[tuple[BrokerLink, str]]:
    """A link to the running broker and its build, or None when none answers."""
    path = socket_path()
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        connection.close()
        return None
    except OSError as error:
        connection.close()
        raise McpError(f"cannot connect to the broker at {path}: {error}") from error
    link = BrokerLink(connection)
    try:
        build = link.call({"op": "hello"}).get("build")
    except McpError:
        # A broker exiting after its idle time closes connections it has not accepted yet.
        link.close()
        return None
    return link, str(build)


@contextlib.contextmanager
def start_lock() -> Iterator[None]:
    with open(lock_path(), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def start_broker() -> None:
    os.makedirs(state_dir(), mode=0o700, exist_ok=True)
    with open(log_path(), "wb") as log:
        try:
            # `-m` imports from the working directory, which -I would leave out of the path;
            # -E and -s keep the rest of its isolation.
            status = subprocess.call(
                [sys.executable, "-E", "-s", "-m", "hax_mcp.broker"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=log,
                cwd=SOURCE_DIR,
                start_new_session=True,
                timeout=START_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            status = None
    if status != 0:
        with open(log_path(), "rb") as log:
            lines = log.read().decode("utf-8", "replace").strip().splitlines()
        detail = f": {one_line(lines[-1])}" if lines else ""
        raise McpError(f"the broker failed to start{detail} (log: {log_path()})")


def connect_broker() -> Optional[BrokerLink]:
    """A link to the running broker, without starting one."""
    found = try_connect()
    return found[0] if found else None


def open_broker() -> BrokerLink:
    """A link to the broker, started if none is running. A broker that runs other code is
    replaced, which stops the servers it was running."""
    found = try_connect()
    if found is not None:
        link, build = found
        if build == build_id():
            return link
        stopped = cast(list[JsonObject], link.call({"op": "stop"}).get("stopped") or [])
        link.close()
        if stopped:
            names = ", ".join(sorted({str(entry["name"]) for entry in stopped}))
            print(f"hax-mcp: restarted local servers after an update: {names}", file=sys.stderr)
    with start_lock():
        found = try_connect()
        if found is None:
            start_broker()
            found = try_connect()
        if found is None:
            raise McpError(f"the broker started but does not accept connections at {socket_path()}")
    return found[0]


class LocalSession:
    """Requests to a local server, which the broker starts on first use and keeps running."""

    def __init__(self, server: LocalServer) -> None:
        self.server = server
        self.link: Optional[BrokerLink] = None

    @property
    def name(self) -> str:
        return self.server.name

    def __enter__(self) -> LocalSession:
        self.link = open_broker()
        return self

    def __exit__(self, *_: object) -> None:
        if self.link is not None:
            self.link.close()

    def request(
        self, method: str, params: JsonObject, headers: Optional[dict[str, str]] = None
    ) -> JsonObject:
        assert self.link is not None
        reply = self.link.call(
            {
                "op": "request",
                "server": cast(JsonObject, dataclasses.asdict(self.server)),
                "environ": cast(JsonObject, dict(os.environ)),
                "method": method,
                "params": params,
            }
        )
        notice = reply.get("notice")
        if isinstance(notice, str):
            print(f"hax-mcp: {notice}", file=sys.stderr)
        message = reply.get("message")
        if not isinstance(message, dict):
            raise McpError(f"{self.name}: malformed broker reply")
        return message_result(message, self.name)


def running() -> list[JsonObject]:
    link = connect_broker()
    if link is None:
        return []
    try:
        return cast(list[JsonObject], link.call({"op": "status"}).get("instances") or [])
    finally:
        link.close()


def stop(name: Optional[str] = None, directory: Optional[str] = None) -> list[JsonObject]:
    """Stop running instances of `name`, or of every server; with `directory`, only the instance
    working in it or in the project containing it. Stopping everything also ends the broker."""
    link = connect_broker()
    if link is None:
        return []
    message: JsonObject = {"op": "stop", "name": name}
    if directory is not None:
        dirs = {os.path.realpath(directory), project_dir(directory)}
        message["dirs"] = cast(list[JsonValue], sorted(dirs))
    try:
        return cast(list[JsonObject], link.call(message).get("stopped") or [])
    finally:
        link.close()


def display_path(path: str) -> str:
    home = os.path.expanduser("~")
    if path == home or path.startswith(home + os.sep):
        return "~" + path[len(home) :]
    return path


def duration(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


def group_memory_kib(groups: set[int]) -> dict[int, int]:
    """Resident memory of each process group, which includes helpers a server started."""
    try:
        output = subprocess.run(
            ["ps", "-A", "-o", "pgid=,rss="],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    totals: dict[int, int] = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[0].isdigit() and fields[1].isdigit():
            group = int(fields[0])
            if group in groups:
                totals[group] = totals.get(group, 0) + int(fields[1])
    return totals


def format_status(instances: Sequence[JsonObject]) -> str:
    if not instances:
        return "no local servers are running"
    now = time.time()
    pids = {cast(int, item["pid"]) for item in instances}
    memory = group_memory_kib(pids)
    rows = [("SERVER", "DIRECTORY", "PID", "MEMORY", "UP", "IDLE")]
    for item in sorted(instances, key=lambda item: (str(item["name"]), str(item["cwd"]))):
        pid = cast(int, item["pid"])
        rows.append(
            (
                str(item["name"]),
                display_path(str(item["cwd"])),
                str(pid),
                f"{round(memory[pid] / 1024)} MiB" if pid in memory else "?",
                duration(now - cast(float, item["started"])),
                duration(now - cast(float, item["last_used"])),
            )
        )
    widths = [max(len(row[column]) for row in rows) for column in range(len(rows[0]))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip() for row in rows
    )

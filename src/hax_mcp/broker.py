"""The background process that keeps local servers running between hax-mcp commands.

Clients connect to a Unix socket and exchange newline-delimited JSON objects, one reply per
request. Each server runs once per working directory, started by the first request for it and
stopped by a `stop` request, by an exit signal, or when its configuration changes.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import select
import signal
import socket
import threading
import time
import traceback
from collections.abc import Callable
from types import FrameType
from typing import IO, Final, Optional, cast

from . import JsonObject, JsonValue, McpError
from .config import LocalServer, runtime_dir, state_dir
from .stdio import POLL_S, Cancelled, ServerProcess, launch

ACCEPT_POLL_S: Final = 1
DRAIN_S: Final = 5
IDLE_EXIT_S: Final = 300
MAX_MESSAGE_BYTES: Final = 64 * 1024 * 1024


def socket_path() -> str:
    return os.path.join(runtime_dir(), "default.sock")


def lock_path() -> str:
    return os.path.join(runtime_dir(), "default.lock")


def log_path() -> str:
    return os.path.join(state_dir(), "broker.log")


def build_id() -> str:
    """Identifies the code this process runs, so clients can replace a broker that runs older
    code. The version number alone misses changes made between releases."""
    digest = hashlib.sha256()
    package = os.path.dirname(os.path.abspath(__file__))
    for name in sorted(os.listdir(package)):
        if name.endswith(".py"):
            with open(os.path.join(package, name), "rb") as file:
                digest.update(name.encode() + b"\0" + file.read())
    return digest.hexdigest()[:16]


def read_message(reader: IO[bytes]) -> Optional[JsonObject]:
    line = reader.readline(MAX_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_MESSAGE_BYTES:
        raise McpError("broker message exceeded 64 MiB")
    message: JsonValue = json.loads(line)
    if not isinstance(message, dict):
        raise McpError("malformed broker message")
    return message


def write_message(writer: IO[bytes], message: JsonObject) -> None:
    writer.write(json.dumps(message).encode() + b"\n")
    writer.flush()


def peer_closed(connection: socket.socket) -> bool:
    """Whether the client hung up. A client sends nothing while it waits for a reply."""
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


Key = tuple[str, str]


class Broker:
    def __init__(self, listener: socket.socket, path: str) -> None:
        self.listener = listener
        self.path = path
        self.inode = os.stat(path).st_ino
        self.build = build_id()
        self.lock = threading.Lock()
        self.instances: dict[Key, ServerProcess] = {}
        self.starting: dict[Key, threading.Lock] = {}
        # Set to abandon a launch in progress, which is not in `instances` until it completes.
        self.launches: dict[Key, threading.Event] = {}
        # Counts stops of each server, so requests queued before a stop fail instead of starting
        # the server again, however the lock happens to pass between them and the stop.
        self.generations: dict[Key, int] = {}
        self.clients = 0
        self.idle_since = time.monotonic()
        self.closing = False

    def serve(self) -> None:
        self.listener.settimeout(ACCEPT_POLL_S)
        try:
            while not self.closing:
                try:
                    connection, _ = self.listener.accept()
                except socket.timeout:
                    if self.idle() or not self.reachable():
                        return
                    continue
                with self.lock:
                    self.clients += 1
                threading.Thread(target=self.serve_client, args=(connection,), daemon=True).start()
        finally:
            self.shutdown()

    def idle(self) -> bool:
        with self.lock:
            running = any(process.exited is None for process in self.instances.values())
            idle_s = time.monotonic() - self.idle_since
            return not running and not self.clients and idle_s >= IDLE_EXIT_S

    def reachable(self) -> bool:
        """Whether clients can still find this broker; a deleted runtime directory ends it."""
        try:
            return os.stat(self.path).st_ino == self.inode
        except OSError:
            return False

    def unlink(self) -> None:
        """Remove the socket so new clients start a new broker; never one a successor made."""
        with contextlib.suppress(OSError):
            if os.stat(self.path).st_ino == self.inode:
                os.unlink(self.path)

    def shutdown(self) -> None:
        with self.lock:
            self.closing = True
            for abort in self.launches.values():
                abort.set()
        self.unlink()
        self.listener.close()
        deadline = time.monotonic() + DRAIN_S
        while self.clients and time.monotonic() < deadline:
            time.sleep(0.05)
        with self.lock:
            keys = set(self.instances) | set(self.launches)
        self.stop_instances(sorted(keys))

    def serve_client(self, connection: socket.socket) -> None:
        try:
            with connection, connection.makefile("rb") as reader:
                with connection.makefile("wb") as writer:
                    while True:
                        message = read_message(reader)
                        if message is None:
                            return
                        reply = self.handle(message, connection)
                        if reply is None:
                            return
                        write_message(writer, reply)
        except (OSError, ValueError, McpError):
            pass
        finally:
            with self.lock:
                self.clients -= 1
                self.idle_since = time.monotonic()

    def handle(self, message: JsonObject, connection: socket.socket) -> Optional[JsonObject]:
        """The reply to one client message, or None when the client is gone."""
        operation = message.get("op")
        try:
            if operation == "hello":
                return {"build": self.build, "pid": os.getpid()}
            if operation == "request":
                return self.request(message, connection)
            if operation == "status":
                return {"instances": self.status()}
            if operation == "stop":
                return {"stopped": self.stop(message.get("name"), message.get("dirs"))}
            return {"error": f"unknown broker operation {operation!r}"}
        except Cancelled:
            return None
        except McpError as error:
            return {"error": str(error)}
        except Exception:
            traceback.print_exc()
            return {"error": f"internal broker error; see {log_path()}"}

    def request(self, message: JsonObject, connection: socket.socket) -> JsonObject:
        server = LocalServer.from_json(cast(JsonObject, message["server"]))
        environ = cast(dict[str, str], message["environ"])

        def gone() -> bool:
            return peer_closed(connection)

        process, notice = self.instance(server, environ, gone)
        method = str(message["method"])
        params = cast(JsonObject, message.get("params") or {})
        reply = process.send(method, params, cancelled=gone)
        return {"message": reply, **({"notice": notice} if notice else {})}

    def instance(
        self, server: LocalServer, environ: dict[str, str], gone: Callable[[], bool]
    ) -> tuple[ServerProcess, Optional[str]]:
        """The running instance for the server and directory, started if needed, plus a notice
        when starting it discarded an earlier instance's state.

        Raises Cancelled when the client hangs up while queued behind another launch. A launch
        of its own runs to completion, leaving the server ready for the next request.
        """
        key = (server.name, server.cwd)
        with self.lock:
            starting = self.starting.setdefault(key, threading.Lock())
            generation = self.generations.get(key, 0)
        while not starting.acquire(timeout=POLL_S):
            if gone():
                raise Cancelled
        try:
            return self.start_instance(key, server, environ, generation)
        finally:
            starting.release()

    def start_instance(
        self, key: Key, server: LocalServer, environ: dict[str, str], generation: int
    ) -> tuple[ServerProcess, Optional[str]]:
        """instance() with the server's starting lock held; `generation` is the number of stops
        of the server when the request arrived."""
        with self.lock:
            if self.generations.get(key, 0) != generation:
                raise McpError(f"{server.name}: the server was stopped")
        notice = None
        if self.stop_outdated(server):
            notice = f"{server.name}: restarted the server with its changed configuration"
        with self.lock:
            current = self.instances.get(key)
        if current is not None and current.exited is not None:
            notice = f"{server.name}: restarted the server, which {current.exited}"
        elif current is not None and current.server != server:
            # The entry is unchanged but resolves differently here, as after rotating a ${VAR}.
            current.close()
            notice = f"{server.name}: restarted the server with its changed environment"
        elif current is not None:
            return current, None
        abort = threading.Event()
        with self.lock:
            if self.closing:
                raise McpError("the broker is shutting down; try again")
            self.launches[key] = abort
        process: Optional[ServerProcess] = None
        try:
            process = launch(server, environ, abort.is_set)
            return process, notice
        except Cancelled:
            raise McpError(f"{server.name}: stopped while starting") from None
        finally:
            # One step, so a stop sees the server either launching or running.
            with self.lock:
                del self.launches[key]
                if process is None:
                    self.instances.pop(key, None)
                else:
                    self.instances[key] = process

    def stop_outdated(self, server: LocalServer) -> bool:
        """Stop running instances of the server started from an earlier version of its entry, in
        any directory, since no request will use them again; returns whether there were any.

        Their starting locks are not taken: requests for two projects under the new entry would
        each hold their own while waiting for the other's.
        """
        with self.lock:
            outdated = [
                key
                for key, process in self.instances.items()
                if key[0] == server.name
                and process.exited is None
                and not process.server.same_entry(server)
            ]
            processes = [self.instances.pop(key) for key in outdated]
        for process in processes:
            process.close()
        return bool(processes)

    def status(self) -> list[JsonValue]:
        with self.lock:
            processes = [p for p in self.instances.values() if p.exited is None]
        return [
            {
                "name": process.name,
                "cwd": process.server.cwd,
                "pid": process.process.pid,
                "version": process.version,
                "started": process.started,
                "last_used": process.last_used,
            }
            for process in processes
        ]

    def stop(self, name: JsonValue, dirs: JsonValue) -> list[JsonValue]:
        """Stop the matching instances, including ones still starting; with no filter, stop
        everything and exit."""
        everything = name is None and dirs is None

        def matches(key: Key) -> bool:
            in_dirs = not isinstance(dirs, list) or key[1] in dirs
            return (name is None or key[0] == name) and in_dirs

        with self.lock:
            self.closing = self.closing or everything
            aborted = [key for key in self.launches if matches(key)]
            for key in aborted:
                self.launches[key].set()
            keys = {key for key in self.instances if matches(key)} | set(aborted)
            for key in keys:
                self.generations[key] = self.generations.get(key, 0) + 1
        if everything:
            self.unlink()
        stopped = self.stop_instances(sorted(keys))
        reported = {(entry["name"], entry["cwd"]) for entry in stopped}
        stopped += [{"name": k[0], "cwd": k[1]} for k in aborted if k not in reported]
        return cast(list[JsonValue], stopped)

    def stop_instances(self, keys: list[Key]) -> list[JsonObject]:
        """Stop the instances, first waiting out launches of them, which end promptly once
        aborted."""
        stopped: list[JsonObject] = []
        threads = []
        for key in keys:
            with self.lock:
                starting = self.starting.setdefault(key, threading.Lock())
            with starting, self.lock:
                process = self.instances.pop(key, None)
            if process is None:
                continue
            if process.exited is None:
                stopped.append({"name": key[0], "cwd": key[1]})
            thread = threading.Thread(target=process.close)
            thread.start()
            threads.append(thread)
        for thread in threads:
            thread.join()
        return stopped


def exit_on_signal(number: int, frame: Optional[FrameType]) -> None:
    raise SystemExit(0)


def main() -> None:
    """Run the broker, detached by the client that starts it while holding the start lock.

    The first process forks and exits once the socket accepts connections, so the client learns
    the broker is ready, or that it failed, from that exit status.
    """
    ready_read, ready_write = os.pipe()
    if os.fork() > 0:
        os.close(ready_write)
        ready = os.read(ready_read, 1) == b"1"
        os._exit(0 if ready else 1)
    os.close(ready_read)
    os.chdir("/")
    os.umask(0o077)
    path = socket_path()
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(16)
    broker = Broker(listener, path)
    for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(number, exit_on_signal)
    os.write(ready_write, b"1")
    os.close(ready_write)
    broker.serve()


if __name__ == "__main__":
    main()

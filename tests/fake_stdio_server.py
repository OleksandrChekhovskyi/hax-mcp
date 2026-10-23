"""An MCP server on stdin and stdout for the tests, in the era its first argument names.

`legacy` answers server/discover with an error, as FastMCP does; `modern` speaks only 2026-07-28;
`exit-on-discover` is a legacy server that exits on the probe; `silent` logs its pid and never
answers; `gated` is a legacy server that logs its start and reads nothing until the file named by
FAKE_MCP_GATE exists. Every message received is appended as a JSON line to the file named by
FAKE_MCP_LOG.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any

Json = Any
MODE = sys.argv[1]
count = 0


def log(message: Json) -> None:
    with open(os.environ["FAKE_MCP_LOG"], "a") as file:
        file.write(json.dumps(message) + "\n")


def send(message: Json) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def receive() -> Json:
    line = sys.stdin.readline()
    if not line:
        raise SystemExit(0)
    message = json.loads(line)
    log(message)
    return message


def text(value: object) -> Json:
    return {"content": [{"type": "text", "text": str(value)}]}


def call(name: str, arguments: Json) -> Json:
    global count
    if name == "count":
        count += 1
        return text(count)
    if name == "cwd":
        return text(os.getcwd())
    if name == "env":
        return text(os.environ.get(arguments["name"], ""))
    if name == "ping_client":
        send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
        send({"jsonrpc": "2.0", "id": "s1", "method": "ping"})
        send({"jsonrpc": "2.0", "id": "s2", "method": "roots/list"})
        replies = [receive(), receive()]
        return text(json.dumps(replies))
    if name == "spawn_helper":
        # A helper that ignores the server's stdin, as a child of npx may.
        helper = [sys.executable, "-c", "import time; time.sleep(60)"]
        return text(subprocess.Popen(helper, stdin=subprocess.DEVNULL).pid)
    if name == "crash":
        sys.stderr.write("crashing on purpose\n")
        sys.stderr.flush()
        raise SystemExit(3)
    return {"content": [], "isError": True}


def main() -> None:
    if MODE in ("silent", "gated"):
        log({"method": "(started)", "pid": os.getpid()})
    while MODE == "gated" and not os.path.exists(os.environ["FAKE_MCP_GATE"]):
        time.sleep(0.02)
    initialized = False
    while True:
        message = receive()
        method, request_id = message.get("method"), message.get("id")
        if request_id is None:
            initialized = initialized or method == "notifications/initialized"
            continue
        if MODE == "silent":
            continue
        params = message.get("params", {})
        reply: Json = {"jsonrpc": "2.0", "id": request_id}
        if method == "server/discover":
            if MODE == "exit-on-discover":
                sys.stderr.write("unknown method server/discover\n")
                raise SystemExit(1)
            if MODE == "modern":
                reply["result"] = {"resultType": "complete", "supportedVersions": ["2026-07-28"]}
            else:
                reply["error"] = {"code": -32602, "message": "Invalid request parameters"}
        elif method == "initialize":
            if MODE == "modern":
                reply["error"] = {"code": -32601, "message": "initialize is not supported"}
            else:
                reply["result"] = {"protocolVersion": "2025-11-25", "capabilities": {}}
        elif MODE != "modern" and not initialized:
            reply["error"] = {"code": -32600, "message": "not initialized"}
        elif method == "tools/list":
            tools = [
                "count",
                "cwd",
                "env",
                "ping_client",
                "spawn_helper",
                "crash",
                "hang",
                "reply_and_exit",
            ]
            reply["result"] = {"tools": [{"name": name, "inputSchema": {}} for name in tools]}
        elif method == "tools/call":
            if params["name"] == "hang":
                continue
            if params["name"] == "reply_and_exit":
                send({**reply, "result": text("bye")})
                raise SystemExit(0)
            reply["result"] = call(params["name"], params.get("arguments", {}))
        else:
            reply["error"] = {"code": -32601, "message": "method not found"}
        send(reply)


main()

"""Fixtures shared by the tests. Import this before hax_mcp: it puts src/ on the path."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Final, Optional
from unittest import mock

ROOT: Final = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from hax_mcp import McpError, auth, cli, config, login  # noqa: E402

Json = Any

TOOLS: Final[list[Json]] = [
    {
        "name": "echo",
        "description": "Echo the arguments back. Useful for tests.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "default": 1},
                "text": {"type": "string", "description": "Text to echo"},
            },
            "required": ["text"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {"name": "fail", "inputSchema": {"type": "object"}},
    {"name": "needs_input", "inputSchema": {"type": "object"}},
    {
        "name": "regional",
        "inputSchema": {
            "type": "object",
            "properties": {"region": {"type": "string", "x-mcp-header": "Region"}},
        },
    },
]


class FakeServer:
    """An MCP endpoint at /mcp plus an OAuth authorization server on the same origin.

    `legacy` serves the 2025-11-25 session handshake instead of 2026-07-28 requests; `token`, when
    set, is the bearer token /mcp requires, and token grants rotate it. `cimd` advertises client
    ID metadata documents, which then identify the client instead of a registration.
    """

    def __init__(
        self,
        legacy: bool = False,
        token: Optional[str] = None,
        sse: bool = False,
        cimd: bool = False,
    ):
        self.legacy = legacy
        self.legacy_rejection = rpc_error("server-error", -32600, "Unsupported protocol version")
        self.token = token
        self.sse = sse
        self.client_id = login.CLIENT_ID_URL if cimd else "client-1"
        self.auth_methods = ["none"]
        self.issuer_param: Optional[str] = None
        self.page_size: Optional[int] = None
        self.issued = 1
        self.refresh_token = "refresh-1"
        self.log: list[tuple[str, dict[str, str], Json]] = []
        self.token_forms: list[dict[str, str]] = []
        self.authorize_query: dict[str, str] = {}
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self))
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        serve = {"poll_interval": 0.01}
        threading.Thread(target=self.httpd.serve_forever, kwargs=serve, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def methods(self) -> list[str]:
        return [method for method, _, _ in self.log]

    def metadata(self) -> Json:
        return {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "registration_endpoint": f"{self.base}/register",
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": self.auth_methods,
            "authorization_response_iss_parameter_supported": True,
            "client_id_metadata_document_supported": self.client_id == login.CLIENT_ID_URL,
        }

    def dispatch(self, body: Json, headers: dict[str, str]) -> tuple[int, Json, dict[str, str]]:
        method, request_id = body["method"], body["id"]
        params = body.get("params", {})
        if self.legacy:
            if method == "initialize":
                result = {"protocolVersion": "2025-11-25", "capabilities": {}}
                return 200, rpc_result(request_id, result), {"Mcp-Session-Id": "session-1"}
            if headers.get("mcp-session-id") != "session-1":
                return 400, self.legacy_rejection, {}
        elif headers.get("mcp-protocol-version") != "2026-07-28" or (
            headers.get("mcp-method") != method
        ):
            return 400, rpc_error(request_id, -32020, "header mismatch"), {}

        if method == "tools/list":
            start = int(params.get("cursor") or 0)
            end = len(TOOLS) if self.page_size is None else start + self.page_size
            result: Json = {"tools": TOOLS[start:end]}
            if end < len(TOOLS):
                result["nextCursor"] = str(end)
            return 200, rpc_result(request_id, result), {}
        if method == "tools/call":
            return self.call(request_id, params, headers)
        return 404, rpc_error(request_id, -32601, "method not found"), {}

    def call(
        self, request_id: Json, params: Json, headers: dict[str, str]
    ) -> tuple[int, Json, dict[str, str]]:
        name, arguments = params["name"], params.get("arguments", {})
        if name == "echo":
            result = {"content": [{"type": "text", "text": json.dumps(arguments)}]}
        elif name == "fail":
            result = {"content": [{"type": "text", "text": "boom"}], "isError": True}
        elif name == "needs_input":
            request = {"method": "elicitation/create", "params": {"message": "Confirm deletion"}}
            result = {"resultType": "input_required", "inputRequests": {"confirm": request}}
        elif name == "invalid":
            return 400, rpc_error(request_id, -32602, "Invalid params"), {}
        elif name == "regional":
            if not self.legacy and headers.get("mcp-param-region") != arguments.get("region"):
                return 400, rpc_error(request_id, -32020, "missing Mcp-Param-Region"), {}
            result = {"content": [{"type": "text", "text": "ok"}]}
        else:
            return 200, rpc_error(request_id, -32602, f"Tool {name} not found"), {}
        return 200, rpc_result(request_id, result), {}

    def grant(self, form: dict[str, str]) -> tuple[int, Json]:
        self.token_forms.append(form)
        if form.get("resource") != f"{self.base}/mcp" or form.get("client_id") != self.client_id:
            return 400, {"error": "invalid_request"}
        if form["grant_type"] == "authorization_code":
            verifier = form.get("code_verifier", "").encode()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier).digest()).rstrip(b"=")
            if (
                form.get("code") != "code-1"
                or challenge.decode() != self.authorize_query.get("code_challenge")
                or form.get("redirect_uri") != self.authorize_query.get("redirect_uri")
            ):
                return 400, {"error": "invalid_grant"}
        elif form.get("refresh_token") != self.refresh_token:
            return 400, {"error": "invalid_grant", "error_description": "refresh token revoked"}
        self.issued += 1
        self.token = f"token-{self.issued}"
        self.refresh_token = f"refresh-{self.issued}"
        tokens = {"access_token": self.token, "token_type": "Bearer", "expires_in": 3600}
        return 200, {**tokens, "refresh_token": self.refresh_token}


def rpc_result(request_id: Json, result: Json) -> Json:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def rpc_error(request_id: Json, code: int, message: str) -> Json:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def make_handler(fake: FakeServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def reply(self, status: int, data: Json, headers: Optional[dict[str, str]] = None) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def reply_sse(self, data: Json, headers: dict[str, str], status: int = 200) -> None:
            progress = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            events = f": keep-alive\n\nevent: message\ndata: {json.dumps(progress)}\n\n"
            events += f"event: message\ndata: {json.dumps(data)}\n\n"
            self.wfile.write(events.encode())

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            if url.path == "/.well-known/oauth-protected-resource/mcp":
                resource = {"resource": f"{fake.base}/mcp", "authorization_servers": [fake.base]}
                self.reply(200, {**resource, "scopes_supported": ["read"]})
            elif url.path == "/.well-known/oauth-authorization-server":
                self.reply(200, fake.metadata())
            elif url.path == "/authorize":
                fake.authorize_query = dict(urllib.parse.parse_qsl(url.query))
                redirect = fake.authorize_query["redirect_uri"]
                issuer = fake.issuer_param or fake.base
                query = {"code": "code-1", "state": fake.authorize_query["state"], "iss": issuer}
                self.send_response(302)
                self.send_header("Location", f"{redirect}?{urllib.parse.urlencode(query)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.reply(404, {})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/register":
                registration = json.loads(body)
                fake.log.append(("register", {}, registration))
                method = registration["token_endpoint_auth_method"]
                client = {"client_id": "client-1", "token_endpoint_auth_method": method}
                self.reply(201, client if method == "none" else {**client, "client_secret": "s"})
                return
            if self.path == "/stream-error":
                error = {"jsonrpc": "2.0", "error": {"code": -32020, "message": "mismatch"}}
                self.reply_sse(error, {}, 400)
                return
            if self.path == "/moved":
                self.send_response(307)
                self.send_header("Location", f"{fake.base}/mcp")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/token":
                self.reply(*fake.grant(dict(urllib.parse.parse_qsl(body.decode()))))
                return

            message = json.loads(body)
            headers = {name.lower(): value for name, value in self.headers.items()}
            fake.log.append((message["method"], headers, message))
            if fake.token is not None and headers.get("authorization") != f"Bearer {fake.token}":
                metadata = f"{fake.base}/.well-known/oauth-protected-resource/mcp"
                challenge = {"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'}
                self.reply(401, {"error": "invalid_token"}, challenge)
                return
            if "id" not in message:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status, data, extra = fake.dispatch(message, headers)
            if fake.sse and status == 200:
                self.reply_sse(data, extra)
            else:
                self.reply(status, data, extra)

        def do_DELETE(self) -> None:
            headers = {name.lower(): value for name, value in self.headers.items()}
            fake.log.append(("DELETE", headers, None))
            self.reply(200, {})

    return Handler


class FakeTty:
    """Readable end of a pipe that claims to be a terminal, for the interactive login."""

    def __init__(self) -> None:
        read_fd, self.write_fd = os.pipe()
        self.file = os.fdopen(read_fd)

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        return self.file.fileno()

    def readline(self) -> str:
        return self.file.readline()

    def type(self, text: str) -> None:
        os.write(self.write_fd, text.encode())

    def close(self) -> None:
        self.file.close()
        os.close(self.write_fd)


class McpTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        environment = {
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_STATE_HOME": str(self.root / "state"),
        }
        patch = mock.patch.dict(os.environ, environment)
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("AI_AGENT", None)

    def serve(self, **options: Any) -> FakeServer:
        fake = FakeServer(**options)
        self.addCleanup(fake.close)
        self.configure({"remote": {"url": f"{fake.base}/mcp"}})
        return fake

    def config_file(self) -> pathlib.Path:
        return self.root / "config" / "hax-mcp" / "servers.json"

    def configure(self, servers: Json) -> None:
        path = self.config_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"mcpServers": servers}))

    def store_login(self, fake: FakeServer, **fields: Json) -> None:
        entry = {
            "url": f"{fake.base}/mcp",
            "issuer": fake.base,
            "token_endpoint": f"{fake.base}/token",
            "resource": f"{fake.base}/mcp",
            "client_id": "client-1",
            "auth_method": "none",
            **fields,
        }
        with auth.locked_credentials() as credentials:
            credentials["remote"] = entry

    def run_cli(self, *args: str, stdin: Optional[Any] = None) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdout", stdout), mock.patch("sys.stderr", stderr):
            with mock.patch("sys.stdin", stdin or io.StringIO()):
                try:
                    code = cli.main(list(args))
                except McpError as error:
                    code = 1
                    print(error, file=stderr)
        return code, stdout.getvalue(), stderr.getvalue()

    def server(self) -> config.Server:
        return config.get_server("remote")

    def credentials(self) -> Json:
        return json.loads(pathlib.Path(auth.credentials_path()).read_text())

from __future__ import annotations

import base64
import contextlib
from collections.abc import Sequence
from typing import Final, Optional, Protocol

from . import PROTOCOL_VERSION, VERSION, JsonObject, JsonValue, LoginRequired, McpError
from .auth import load_login, login_required, refresh_login, token_expiring
from .config import Server
from .text import one_line
from .transport import HEADER_NAME, RpcReply, http_request, post_rpc, www_authenticate

CLIENT_INFO: Final[JsonObject] = {"name": "hax-mcp", "version": VERSION}
LEGACY_VERSION: Final = "2025-11-25"
MAX_TOOL_PAGES: Final = 50
HEADER_MISMATCH: Final = -32020
UNSUPPORTED_VERSION: Final = -32022
MODERN_ERRORS: Final = {HEADER_MISMATCH, -32021, UNSUPPORTED_VERSION}
METHOD_NOT_FOUND: Final = -32601


class HeaderMismatch(McpError):
    pass


def header_value(value: str) -> str:
    plain = all(char == "\t" or 0x20 <= ord(char) <= 0x7E for char in value)
    sentinel = value.startswith("=?base64?") and value.endswith("?=")
    if plain and value == value.strip() and not sentinel:
        return value
    return "=?base64?" + base64.b64encode(value.encode()).decode() + "?="


def param_headers(schema: JsonValue, arguments: JsonValue) -> dict[str, str]:
    """Mirror arguments whose schema property carries `x-mcp-header` into Mcp-Param headers."""
    headers: dict[str, str] = {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if not isinstance(properties, dict) or not isinstance(arguments, dict):
        return headers
    for name, child in properties.items():
        if name not in arguments or not isinstance(child, dict):
            continue
        value = arguments[name]
        header = child.get("x-mcp-header")
        if isinstance(header, str):
            if not HEADER_NAME.fullmatch(header):
                raise McpError(f"tool declares an invalid x-mcp-header {header!r}")
            if isinstance(value, bool):
                headers[f"Mcp-Param-{header}"] = "true" if value else "false"
            elif isinstance(value, (int, str)):
                headers[f"Mcp-Param-{header}"] = header_value(str(value))
        headers.update(param_headers(child, value))
    return headers


def modern_request(
    request_id: int, method: str, params: JsonObject
) -> tuple[JsonObject, dict[str, str]]:
    meta: JsonObject = {
        "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
        "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
        "io.modelcontextprotocol/clientCapabilities": {},
    }
    message: JsonObject = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": {**params, "_meta": meta},
    }
    headers = {"MCP-Protocol-Version": PROTOCOL_VERSION, "Mcp-Method": method}
    name = params.get("name")
    if method == "tools/call" and isinstance(name, str):
        headers["Mcp-Name"] = header_value(name)
    return message, headers


def is_legacy_rejection(reply: RpcReply) -> bool:
    """Whether a modern request failed because the server only speaks versions before 2026-07-28.

    Modern servers also answer 400 and 404, but always with an MCP-defined error code. A server
    that knows that code but supports only older versions lists them with UnsupportedVersion.
    """
    if reply.status not in (400, 404, 405):
        return False
    error = reply.message.get("error") if reply.message else None
    if not isinstance(error, dict):
        return True
    code = error.get("code")
    if code == UNSUPPORTED_VERSION:
        data = error.get("data")
        supported = data.get("supported") if isinstance(data, dict) else None
        return isinstance(supported, list) and bool(supported) and PROTOCOL_VERSION not in supported
    return code not in MODERN_ERRORS and not (reply.status == 404 and code == METHOD_NOT_FOUND)


def input_required_text(result: JsonObject) -> str:
    requests = result.get("inputRequests")
    details = []
    for request in requests.values() if isinstance(requests, dict) else []:
        if not isinstance(request, dict):
            continue
        method = one_line(request.get("method"))
        params = request.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "elicitation/create":
            url = one_line(params.get("url"))
            details.append(" ".join(filter(None, [one_line(params.get("message")), url])))
        else:
            details.append(f"unsupported {method} request")
    text = "; ".join(filter(None, details))
    return "server needs input hax-mcp cannot provide" + (f": {text}" if text else "")


def rpc_result(reply: RpcReply, server: str) -> JsonObject:
    if reply.status == 403:
        detail = www_authenticate(reply.headers).get("error_description")
        raise McpError(f"{server}: access denied" + (f": {one_line(detail)}" if detail else ""))
    if reply.message is None:
        raise McpError(f"{server}: unexpected HTTP {reply.status} response")
    return message_result(reply.message, server)


def message_result(message: JsonObject, server: str) -> JsonObject:
    """The result of a JSON-RPC response, or McpError for errors and incomplete results."""
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        text = one_line(error.get("message")) or "error"
        exception = HeaderMismatch if code == HEADER_MISMATCH else McpError
        raise exception(f"{server}: {text} (code {code})")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError(f"{server}: malformed response")
    kind = result.get("resultType", "complete")
    if kind == "input_required":
        raise McpError(f"{server}: {input_required_text(result)}")
    if kind != "complete":
        raise McpError(f"{server}: unsupported result type {one_line(kind)!r}")
    return result


class Connection(Protocol):
    """Requests to one server, whichever transport carries them."""

    @property
    def name(self) -> str: ...

    def request(
        self, method: str, params: JsonObject, headers: Optional[dict[str, str]] = None
    ) -> JsonObject: ...


class Session:
    """Requests to one remote server for the life of one command.

    Requests go out in the 2026-07-28 form; a server that rejects it as a legacy server gets the
    initialize handshake, and the rest of the session follows its negotiated version. The era is a
    property of the server, so once a modern reply arrives, later errors never trigger the fallback.
    """

    def __init__(self, server: Server) -> None:
        self.server = server
        self.login = load_login(server)
        self.refreshed = False
        self.modern = False
        self.legacy_version: Optional[str] = None
        self.session_id: Optional[str] = None
        self.last_id = 0

    @property
    def name(self) -> str:
        return self.server.name

    def __enter__(self) -> Session:
        return self

    def __exit__(self, *_: object) -> None:
        if self.session_id is None:
            return
        with contextlib.suppress(McpError):
            http_request(self.server.url, None, self.headers(self.legacy_headers()), "DELETE")

    def take_id(self) -> int:
        self.last_id += 1
        return self.last_id

    def headers(self, extra: dict[str, str]) -> dict[str, str]:
        headers = dict(self.server.headers)
        if self.login is not None:
            headers["Authorization"] = f"Bearer {self.login['access_token']}"
        return {**headers, **extra}

    def legacy_headers(self) -> dict[str, str]:
        headers = {"MCP-Protocol-Version": self.legacy_version or LEGACY_VERSION}
        if self.session_id is not None:
            headers["Mcp-Session-Id"] = self.session_id
        return headers

    def refresh(self, force: bool) -> None:
        assert self.login is not None
        self.refreshed = True
        self.login = refresh_login(self.server, self.login, force)

    def send(self, message: JsonObject, headers: dict[str, str]) -> RpcReply:
        if self.login is not None and not self.refreshed and token_expiring(self.login):
            self.refresh(force=False)
        reply = post_rpc(self.server.url, message, self.headers(headers))
        if reply.status == 401 and self.login is not None and not self.refreshed:
            self.refresh(force=True)
            reply = post_rpc(self.server.url, message, self.headers(headers))
        if reply.status == 401:
            if self.server.has_authorization():
                raise McpError(f"{self.server.name}: server rejected the configured credentials")
            raise LoginRequired(login_required(self.server.name))
        return reply

    def initialize(self) -> None:
        params: JsonObject = {
            "protocolVersion": LEGACY_VERSION,
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        }
        message: JsonObject = {
            "jsonrpc": "2.0",
            "id": self.take_id(),
            "method": "initialize",
            "params": params,
        }
        reply = self.send(message, {})
        version = rpc_result(reply, self.server.name).get("protocolVersion")
        self.legacy_version = version if isinstance(version, str) else LEGACY_VERSION
        self.session_id = reply.headers.get("Mcp-Session-Id")
        notification: JsonObject = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        status = self.send(notification, self.legacy_headers()).status
        if status >= 400:
            raise McpError(f"{self.server.name}: initialization failed (HTTP {status})")

    def request(
        self, method: str, params: JsonObject, headers: Optional[dict[str, str]] = None
    ) -> JsonObject:
        if self.legacy_version is None:
            message, modern_headers = modern_request(self.take_id(), method, params)
            reply = self.send(message, {**modern_headers, **(headers or {})})
            if self.modern or not is_legacy_rejection(reply):
                self.modern = True
                return rpc_result(reply, self.server.name)
            self.initialize()
        message = {"jsonrpc": "2.0", "id": self.take_id(), "method": method, "params": params}
        return rpc_result(self.send(message, self.legacy_headers()), self.server.name)


def list_tools(session: Connection) -> list[JsonObject]:
    tools: list[JsonObject] = []
    cursor: JsonValue = None
    for _ in range(MAX_TOOL_PAGES):
        result = session.request("tools/list", {"cursor": cursor} if cursor else {})
        page = result.get("tools")
        if not isinstance(page, list):
            raise McpError(f"{session.name}: malformed tool list")
        tools.extend(t for t in page if isinstance(t, dict) and isinstance(t.get("name"), str))
        cursor = result.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            return tools
    raise McpError(f"{session.name}: tool list has more than {MAX_TOOL_PAGES} pages")


def find_tool(tools: Sequence[JsonObject], name: str, server: str) -> JsonObject:
    for tool in tools:
        if tool.get("name") == name:
            return tool
    raise McpError(f"{server}: no tool named {name!r}; see hax-mcp tools {server}")


def call_tool(session: Connection, name: str, arguments: JsonObject) -> JsonObject:
    params: JsonObject = {"name": name, "arguments": arguments}
    try:
        return session.request("tools/call", params)
    except HeaderMismatch:
        # Only tools that declare x-mcp-header parameters need their schema before a call.
        tool = find_tool(list_tools(session), name, session.name)
        headers = param_headers(tool.get("inputSchema"), arguments)
        return session.request("tools/call", params, headers)

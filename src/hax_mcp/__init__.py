"""Use tools of MCP servers from the shell."""

from __future__ import annotations

from typing import Final, Union

JsonPrimitive = Union[None, bool, int, float, str]
JsonValue = Union[JsonPrimitive, list["JsonValue"], dict[str, "JsonValue"]]
JsonObject = dict[str, JsonValue]

VERSION: Final = "0.1.0"
PROTOCOL_VERSION: Final = "2026-07-28"


class McpError(Exception):
    pass


class LoginRequired(McpError):
    pass

from __future__ import annotations

import contextlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from typing import Final, cast

from . import JsonObject, JsonValue, McpError
from .transport import require_secure

SERVER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass
class Server:
    name: str
    url: str
    headers: dict[str, str]

    def has_authorization(self) -> bool:
        return any(name.lower() == "authorization" for name in self.headers)


def xdg_dir(variable: str, fallback: str) -> str:
    base = os.environ.get(variable) or os.path.join(os.path.expanduser("~"), fallback)
    return os.path.join(base, "hax-mcp")


def config_path() -> str:
    return os.path.join(xdg_dir("XDG_CONFIG_HOME", ".config"), "servers.json")


def state_dir() -> str:
    return xdg_dir("XDG_STATE_HOME", os.path.join(".local", "state"))


def write_json(path: str, data: JsonValue) -> None:
    """Replace `path` atomically with a file only the owner can read."""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(dir=directory, prefix=".hax-mcp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_path)
        raise


def read_config() -> JsonObject:
    """The whole configuration file, whose other keys `add` and `remove` preserve."""
    path = config_path()
    try:
        with open(path, encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise McpError(f"cannot read {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("mcpServers", {}), dict):
        raise McpError(f'{path}: expected an object with "mcpServers"')
    return data


def load_config() -> dict[str, JsonObject]:
    servers = cast(JsonObject, read_config().get("mcpServers", {}))
    return {name: entry for name, entry in servers.items() if isinstance(entry, dict)}


def expand_env(value: str, server: str) -> str:
    def replace(match: re.Match[str]) -> str:
        variable = match.group(1)
        if variable not in os.environ:
            raise McpError(f"{server}: environment variable {variable} is not set")
        return os.environ[variable]

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def get_server(name: str) -> Server:
    entry = load_config().get(name)
    if entry is None:
        raise McpError(f"unknown server {name!r}; see hax-mcp servers")
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise McpError(f'{name}: only remote servers with a "url" are supported')
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise McpError(f'{name}: "headers" must map header names to strings')
    expanded = {key: expand_env(cast(str, value), name) for key, value in headers.items()}
    return Server(name, require_secure(expand_env(url, name)), expanded)


def add_server(name: str, url: str, headers: dict[str, str]) -> None:
    if not SERVER_NAME.fullmatch(name):
        raise McpError("server names may contain letters, digits, '.', '_', and '-'")
    require_secure(expand_env(url, name))
    data = read_config()
    servers = cast(JsonObject, data.setdefault("mcpServers", {}))
    if name in servers:
        raise McpError(f"{name} is already configured; remove it first")
    entry: JsonObject = {"url": url}
    if headers:
        entry["headers"] = cast(JsonObject, headers)
    servers[name] = entry
    write_json(config_path(), data)


def remove_server(name: str) -> None:
    data = read_config()
    servers = cast(JsonObject, data.get("mcpServers", {}))
    if servers.pop(name, None) is None:
        raise McpError(f"unknown server {name!r}; see hax-mcp servers")
    write_json(config_path(), data)

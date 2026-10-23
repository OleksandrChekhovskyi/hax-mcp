from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from typing import Final, Optional, Union, cast

from . import PROTOCOL_VERSION, JsonObject, JsonValue, McpError
from .transport import require_secure

PROTOCOLS: Final = ("auto", "legacy", PROTOCOL_VERSION)
SERVER_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass
class Server:
    name: str
    url: str
    headers: dict[str, str]

    def has_authorization(self) -> bool:
        return any(name.lower() == "authorization" for name in self.headers)


@dataclass
class LocalServer:
    """A server run as a child process speaking MCP on its stdin and stdout.

    `command` is resolved to a path, and `env` holds only the configured variables. `protocol` is
    "auto", "legacy" for the initialize handshake alone, or a pinned version. `entry` is the
    configuration as written, before anything was expanded or resolved.
    """

    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    protocol: str
    entry: JsonObject

    def same_entry(self, other: LocalServer) -> bool:
        """Whether both come from the same configuration. What it resolves to may still differ
        between projects, such as the python a project's virtual environment puts first in PATH."""
        return self.name == other.name and self.entry == other.entry

    @classmethod
    def from_json(cls, data: JsonObject) -> LocalServer:
        """Rebuild a server that another hax-mcp process resolved and serialized."""
        return cls(
            str(data["name"]),
            str(data["command"]),
            cast(list[str], data["args"]),
            cast(dict[str, str], data["env"]),
            str(data["cwd"]),
            str(data["protocol"]),
            cast(JsonObject, data["entry"]),
        )


def xdg_dir(variable: str, fallback: str) -> str:
    base = os.environ.get(variable) or os.path.join(os.path.expanduser("~"), fallback)
    return os.path.join(base, "hax-mcp")


def config_path() -> str:
    return os.path.join(xdg_dir("XDG_CONFIG_HOME", ".config"), "servers.json")


def state_dir() -> str:
    return xdg_dir("XDG_STATE_HOME", os.path.join(".local", "state"))


def runtime_dir() -> str:
    """A directory for sockets that only the current user can enter, created on demand."""
    base = os.environ.get("XDG_RUNTIME_DIR")
    path = (
        os.path.join(base, "hax-mcp")
        if base
        else os.path.join(tempfile.gettempdir(), f"hax-mcp-{os.getuid()}")
    )
    with contextlib.suppress(FileExistsError):
        os.makedirs(path, mode=0o700)
    # Under a shared /tmp another user may have created the directory first.
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise McpError(f"{path} must be a directory only you can access")
    return path


def project_dir(start: str) -> str:
    """The nearest directory at or above `start` that contains .git, or `start` itself."""
    path = os.path.realpath(start)
    candidate = path
    while not os.path.exists(os.path.join(candidate, ".git")):
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return path
        candidate = parent
    return candidate


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


def string_list(value: JsonValue) -> Optional[list[str]]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return cast(list[str], value)


def string_map(value: JsonValue) -> Optional[dict[str, str]]:
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in value.values()):
        return None
    return cast(dict[str, str], value)


def resolve_command(command: str, directory: str, search_path: Optional[str], server: str) -> str:
    """An absolute path to the command: a relative path names a file in the server's `directory`,
    and a bare name is found through `search_path` the way the caller's shell would find it."""
    if os.sep in command:
        path = os.path.normpath(os.path.join(directory, os.path.expanduser(command)))
        if not os.access(path, os.X_OK) or os.path.isdir(path):
            raise McpError(f"{server}: {path} is not an executable file")
        return path
    found = shutil.which(command, path=search_path)
    if found is None:
        raise McpError(f"{server}: command {command!r} not found in PATH")
    return os.path.abspath(found)


def get_local_server(name: str, entry: JsonObject) -> LocalServer:
    """Expand ${VAR}, find the command, and pick the working directory: the configured "cwd", or
    the project containing the current directory, so each project gets its own instance."""
    command = entry.get("command")
    if not isinstance(command, str) or not command:
        raise McpError(f'{name}: "command" must be a non-empty string')
    args = string_list(entry.get("args", []))
    if args is None:
        raise McpError(f'{name}: "args" must be a list of strings')
    env = string_map(entry.get("env", {}))
    if env is None:
        raise McpError(f'{name}: "env" must map variable names to strings')
    cwd = entry.get("cwd")
    if cwd is not None and not isinstance(cwd, str):
        raise McpError(f'{name}: "cwd" must be a string')
    protocol = entry.get("protocol", "auto")
    if protocol not in PROTOCOLS:
        raise McpError(f'{name}: "protocol" must be one of {", ".join(PROTOCOLS)}')
    if cwd is None:
        directory = project_dir(os.getcwd())
    else:
        directory = os.path.realpath(os.path.expanduser(expand_env(cwd, name)))
        if not os.path.isdir(directory):
            raise McpError(f"{name}: {directory} is not a directory")
    expanded = {key: expand_env(value, name) for key, value in env.items()}
    search_path = expanded.get("PATH", os.environ.get("PATH"))
    return LocalServer(
        name,
        resolve_command(expand_env(command, name), directory, search_path, name),
        [expand_env(arg, name) for arg in args],
        expanded,
        directory,
        cast(str, protocol),
        entry,
    )


def get_server(name: str) -> Union[Server, LocalServer]:
    entry = load_config().get(name)
    if entry is None:
        raise McpError(f"unknown server {name!r}; see hax-mcp servers")
    if "url" in entry and "command" in entry:
        raise McpError(f'{name}: set either "url" or "command", not both')
    if "command" in entry:
        return get_local_server(name, entry)
    url = entry.get("url")
    if not isinstance(url, str) or not url:
        raise McpError(f'{name}: set "url" for a remote server or "command" for a local one')
    headers = entry.get("headers", {})
    if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
        raise McpError(f'{name}: "headers" must map header names to strings')
    expanded = {key: expand_env(cast(str, value), name) for key, value in headers.items()}
    return Server(name, require_secure(expand_env(url, name)), expanded)


def add_entry(name: str, entry: JsonObject) -> None:
    if not SERVER_NAME.fullmatch(name):
        raise McpError("server names may contain letters, digits, '.', '_', and '-'")
    data = read_config()
    servers = cast(JsonObject, data.setdefault("mcpServers", {}))
    if name in servers:
        raise McpError(f"{name} is already configured; remove it first")
    servers[name] = entry
    write_json(config_path(), data)


def add_server(name: str, url: str, headers: dict[str, str]) -> None:
    require_secure(expand_env(url, name))
    entry: JsonObject = {"url": url}
    if headers:
        entry["headers"] = cast(JsonObject, headers)
    add_entry(name, entry)


def add_local_server(name: str, command: list[str], env: dict[str, str]) -> None:
    entry: JsonObject = {"command": command[0]}
    if command[1:]:
        entry["args"] = cast(list[JsonValue], command[1:])
    if env:
        entry["env"] = cast(JsonObject, env)
    add_entry(name, entry)


def remove_server(name: str) -> None:
    data = read_config()
    servers = cast(JsonObject, data.get("mcpServers", {}))
    if servers.pop(name, None) is None:
        raise McpError(f"unknown server {name!r}; see hax-mcp servers")
    write_json(config_path(), data)

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from collections.abc import Sequence
from typing import Optional, Union

from . import VERSION, JsonObject, LoginRequired, McpError
from .auth import logout, read_credentials
from .config import (
    LocalServer,
    Server,
    add_local_server,
    add_server,
    config_path,
    get_server,
    load_config,
    project_dir,
    remove_server,
)
from .local import LocalSession, display_path, format_status, running, stop
from .login import login
from .protocol import Session, call_tool, find_tool, list_tools
from .render import format_result, format_tool, format_tools
from .stdio import launch
from .text import dump_json, sanitize_text, write_output
from .transport import HEADER_NAME


def parse_header(value: str) -> tuple[str, str]:
    name, separator, content = value.partition(":")
    if not separator or not HEADER_NAME.fullmatch(name.strip()):
        raise argparse.ArgumentTypeError(f"expected 'Name: value', got {value!r}")
    return name.strip(), content.strip()


def parse_env(value: str) -> tuple[str, str]:
    name, separator, content = value.partition("=")
    if not separator or not name:
        raise argparse.ArgumentTypeError(f"expected 'NAME=value', got {value!r}")
    return name, content


def open_session(server: Union[Server, LocalServer]) -> Union[Session, LocalSession]:
    return LocalSession(server) if isinstance(server, LocalServer) else Session(server)


def count_tools(server: Union[Server, LocalServer]) -> int:
    """List the server's tools once, without leaving a local server running in the background."""
    if isinstance(server, LocalServer):
        process = launch(server, dict(os.environ))
        try:
            return len(list_tools(process))
        finally:
            process.close()
    with Session(server) as session:
        return len(list_tools(session))


def check_server(name: str) -> str:
    """Describe whether a newly added server is usable, without failing the addition."""
    try:
        count = count_tools(get_server(name))
    except LoginRequired:
        return f"added {name}; log in with: hax-mcp login {name}"
    except McpError as error:
        return f"added {name}, but listing its tools failed: {error}"
    return f"added {name} ({count} tools)"


def format_servers() -> str:
    config = load_config()
    if not config:
        return f"no servers configured in {config_path()}"
    credentials = read_credentials()
    lines = []
    for name, entry in config.items():
        command, args = entry.get("command"), entry.get("args", [])
        if isinstance(command, str):
            words = [command, *args] if isinstance(args, list) else [command]
            lines.append(f"{name}  {shlex.join(str(word) for word in words)}")
            continue
        url = entry.get("url")
        if not isinstance(url, str):
            lines.append(f"{name}  (invalid: no url or command)")
            continue
        headers = entry.get("headers")
        if isinstance(headers, dict) and any(k.lower() == "authorization" for k in headers):
            state = "  [token]"
        elif name in credentials:
            state = "  [logged in]"
        else:
            state = ""
        lines.append(f"{name}  {url}{state}")
    return "\n".join(lines)


def read_arguments(value: Optional[str]) -> JsonObject:
    text = sys.stdin.read() if value == "-" else value or "{}"
    try:
        arguments = json.loads(text)
    except ValueError as error:
        raise McpError(f"arguments are not valid JSON: {error}") from error
    if not isinstance(arguments, dict):
        raise McpError("arguments must be a JSON object")
    return arguments


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hax-mcp", description="Use tools of MCP servers from the shell."
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("servers", help="list configured servers")

    tools = commands.add_parser("tools", help="list a server's tools, one per line")
    tools.add_argument("server")
    tools.add_argument("--json", action="store_true", help="write tool definitions as JSON")

    show = commands.add_parser("show", help="describe tools and their parameters")
    show.add_argument("server")
    show.add_argument("tools", nargs="+", metavar="tool")
    show.add_argument("--json", action="store_true", help="write tool definitions as JSON")

    call = commands.add_parser("call", help="call a tool")
    call.add_argument("server")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", help="JSON object, or - to read stdin")
    call.add_argument("--json", action="store_true", help="write the raw result as JSON")

    add = commands.add_parser(
        "add",
        help="add a server to the configuration",
        usage="%(prog)s [-H 'Name: value'] server url\n"
        "       %(prog)s [-e NAME=value] server [--] command [arg ...]",
        description="Add a remote server by URL, or a local one by the command that runs it. "
        "Put -- before a command whose arguments start with -.",
    )
    add.add_argument("server")
    add.add_argument("target", nargs="+", metavar="url | command [arg ...]")
    add.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        type=parse_header,
        metavar="'Name: value'",
        help="header of a remote server; ${VAR} is read from the environment",
    )
    add.add_argument(
        "-e",
        "--env",
        action="append",
        default=[],
        type=parse_env,
        metavar="NAME=value",
        help="variable for a local server; ${VAR} is read from the environment",
    )
    remove = commands.add_parser("remove", help="remove a server and its login")
    remove.add_argument("server")

    commands.add_parser("status", help="list running local servers")
    stop_parser = commands.add_parser(
        "stop",
        help="stop running local servers, all of them by default",
        description="Stop running local servers. Their state is lost; the next use of a server "
        "starts it again.",
    )
    stop_parser.add_argument("server", nargs="?", help="stop only this server")
    stop_parser.add_argument(
        "-C",
        "--dir",
        metavar="DIR",
        help="stop only instances working in DIR or in the project containing it",
    )

    login_parser = commands.add_parser("login", help="log in to a server (interactive)")
    login_parser.add_argument("server")
    logout_parser = commands.add_parser("logout", help="remove a stored login")
    logout_parser.add_argument("server")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "servers":
        write_output(format_servers())
        return 0
    if args.command == "add":
        target: list[str] = args.target
        # Classify by the expanded value, so '${MCP_URL}' is a URL; the entry keeps it as typed.
        if "://" in os.path.expandvars(target[0]):
            if len(target) > 1:
                raise McpError("a remote server takes a URL alone")
            if args.env:
                raise McpError("-e applies only to local servers")
            add_server(args.server, target[0], dict(args.header))
        else:
            if args.header:
                raise McpError("-H applies only to remote servers")
            add_local_server(args.server, target, dict(args.env))
        print(check_server(args.server))
        return 0
    if args.command == "remove":
        remove_server(args.server)
        logout(args.server)
        stop(args.server)
        print(f"removed {args.server}")
        return 0
    if args.command == "status":
        write_output(format_status(running()))
        return 0
    if args.command == "stop":
        stopped = stop(args.server, args.dir)
        for entry in stopped:
            print(f"stopped {entry['name']} in {display_path(str(entry['cwd']))}")
        if not stopped:
            where = f" in {display_path(project_dir(args.dir))}" if args.dir else ""
            subject = f"{args.server} is not" if args.server else "no local servers are"
            print(f"{subject} running{where}")
        return 0

    if args.command == "logout":
        removed = logout(args.server)
        print(f"logged out of {args.server}" if removed else f"not logged in to {args.server}")
        return 0

    server = get_server(args.server)
    if args.command == "login":
        if isinstance(server, LocalServer):
            raise McpError(f"{server.name} is a local server, which needs no login")
        login(server)
        print(f"logged in to {server.name}")
        return 0

    with open_session(server) as session:
        if args.command == "tools":
            tools = list_tools(session)
            write_output(dump_json(tools) if args.json else format_tools(tools), args.json)
            return 0
        if args.command == "show":
            tools = list_tools(session)
            selected = [find_tool(tools, name, server.name) for name in args.tools]
            if args.json:
                write_output("\n".join(dump_json(tool) for tool in selected), raw=True)
            else:
                write_output("\n\n".join(format_tool(tool) for tool in selected))
            return 0

        result = call_tool(session, args.tool, read_arguments(args.arguments))
        failed = result.get("isError") is True
        if args.json:
            write_output(dump_json(result), raw=True)
        elif failed:
            text = format_result(result) or "tool reported an error"
            print(sanitize_text(text), file=sys.stderr)
        else:
            write_output(format_result(result))
        return 1 if failed else 0


def run() -> None:
    try:
        raise SystemExit(main())
    except McpError as error:
        print(f"hax-mcp: {error}", file=sys.stderr)
        raise SystemExit(1)
    except KeyboardInterrupt:
        raise SystemExit(130)
    except BrokenPipeError:
        raise SystemExit(0)

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Optional

from . import VERSION, JsonObject, LoginRequired, McpError
from .auth import logout, read_credentials
from .config import add_server, config_path, get_server, load_config, remove_server
from .login import login
from .protocol import Session, call_tool, find_tool, list_tools
from .render import format_result, format_tool, format_tools
from .text import dump_json, sanitize_text, write_output
from .transport import HEADER_NAME


def parse_header(value: str) -> tuple[str, str]:
    name, separator, content = value.partition(":")
    if not separator or not HEADER_NAME.fullmatch(name.strip()):
        raise argparse.ArgumentTypeError(f"expected 'Name: value', got {value!r}")
    return name.strip(), content.strip()


def check_server(name: str) -> str:
    """Describe whether a newly added server is usable, without failing the addition."""
    try:
        with Session(get_server(name)) as session:
            count = len(list_tools(session))
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
        url = entry.get("url")
        if not isinstance(url, str):
            lines.append(f"{name}  (unsupported: not a remote server)")
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
        prog="hax-mcp", description="Use tools of remote MCP servers from the shell."
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

    add = commands.add_parser("add", help="add a server to the configuration")
    add.add_argument("server")
    add.add_argument("url")
    add.add_argument(
        "-H",
        "--header",
        action="append",
        default=[],
        type=parse_header,
        help="'Name: value' header; ${VAR} is read from the environment",
    )
    remove = commands.add_parser("remove", help="remove a server and its login")
    remove.add_argument("server")

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
        add_server(args.server, args.url, dict(args.header))
        print(check_server(args.server))
        return 0
    if args.command == "remove":
        remove_server(args.server)
        logout(args.server)
        print(f"removed {args.server}")
        return 0

    if args.command == "logout":
        removed = logout(args.server)
        print(f"logged out of {args.server}" if removed else f"not logged in to {args.server}")
        return 0

    server = get_server(args.server)
    if args.command == "login":
        login(server)
        print(f"logged in to {server.name}")
        return 0

    with Session(server) as session:
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

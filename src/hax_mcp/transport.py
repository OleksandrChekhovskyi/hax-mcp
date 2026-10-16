from __future__ import annotations

import contextlib
import http.client
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from email.message import Message
from typing import IO, Final, NamedTuple, Optional, Protocol, Union

from . import VERSION, JsonObject, JsonValue, McpError
from .text import one_line

AUTH_PARAM: Final = re.compile(r'([A-Za-z_]+)=(?:"([^"]*)"|([^\s,]*))')
HEADER_NAME: Final = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
HTTP_TIMEOUT_S: Final = 120
LOOPBACK_HOSTS: Final = {"127.0.0.1", "::1", "localhost"}
MAX_RESPONSE_BYTES: Final = 16 * 1024 * 1024
TRANSPORT_ERRORS: Final = (urllib.error.URLError, http.client.HTTPException, OSError)
USER_AGENT: Final = f"hax-mcp/{VERSION}"


def is_secure(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)


def require_secure(url: str) -> str:
    if not is_secure(url):
        raise McpError(f"refusing non-HTTPS URL: {one_line(url)}")
    return url


class Reply(NamedTuple):
    status: int
    headers: Message
    body: bytes


class RpcReply(NamedTuple):
    status: int
    headers: Message
    message: Optional[JsonObject]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to URLs that require_secure() would accept."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: http.client.HTTPMessage,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        if not is_secure(newurl):
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# urllib turns a redirected POST into a GET that still carries the Authorization header.
POST_OPENER: Final = urllib.request.build_opener(NoRedirect)
GET_OPENER: Final = urllib.request.build_opener(SecureRedirect)


def open_url(
    url: str, data: Optional[bytes], headers: dict[str, str], method: str
) -> Union[http.client.HTTPResponse, urllib.error.HTTPError]:
    request = urllib.request.Request(
        url, data=data, headers={"User-Agent": USER_AGENT, **headers}, method=method
    )
    opener = GET_OPENER if method == "GET" else POST_OPENER
    try:
        return opener.open(request, timeout=HTTP_TIMEOUT_S)
    except urllib.error.HTTPError as error:
        return error
    except TRANSPORT_ERRORS as error:
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        host = urllib.parse.urlsplit(url).netloc
        raise McpError(f"cannot reach {host}: {one_line(str(reason))}") from error


def read_limited(response: Union[http.client.HTTPResponse, urllib.error.HTTPError]) -> bytes:
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except TRANSPORT_ERRORS as error:
        raise McpError(f"connection failed: {one_line(str(error))}") from error
    if len(body) > MAX_RESPONSE_BYTES:
        raise McpError("response exceeded 16 MiB")
    return body


def http_request(
    url: str,
    data: Optional[bytes] = None,
    headers: Optional[dict[str, str]] = None,
    method: str = "GET",
) -> Reply:
    with open_url(url, data, headers or {}, method) as response:
        return Reply(response.getcode() or 0, response.headers, read_limited(response))


def parse_json_object(body: bytes) -> JsonObject:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def fetch_json(url: str) -> Optional[JsonObject]:
    reply = http_request(require_secure(url), headers={"Accept": "application/json"})
    if reply.status != 200:
        return None
    return parse_json_object(reply.body) or None


class LineReader(Protocol):
    def readline(self, size: int = -1, /) -> bytes: ...


def limited_lines(response: LineReader) -> Iterator[bytes]:
    """Read lines while the whole response stays within MAX_RESPONSE_BYTES, so one unterminated
    line cannot grow without bound."""
    remaining = MAX_RESPONSE_BYTES
    while True:
        line = response.readline(remaining + 1)
        if not line:
            return
        remaining -= len(line)
        if remaining < 0:
            raise McpError("response exceeded 16 MiB")
        yield line


def sse_messages(lines: Iterable[bytes]) -> Iterator[JsonValue]:
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if not line:
            if data:
                with contextlib.suppress(ValueError):
                    yield json.loads("\n".join(data))
            data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        with contextlib.suppress(ValueError):
            yield json.loads("\n".join(data))


def post_rpc(url: str, message: JsonObject, headers: dict[str, str]) -> RpcReply:
    request_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        **headers,
    }
    body = json.dumps(message).encode()
    with open_url(url, body, request_headers, "POST") as response:
        status = response.getcode() or 0
        reply: JsonValue = None
        if "text/event-stream" in response.headers.get("Content-Type", ""):
            # The stream may carry progress notifications first, and legacy servers may hold it
            # open after the response. An error the server could not tie to a request has no id.
            try:
                for candidate in sse_messages(limited_lines(response)):
                    if not isinstance(candidate, dict):
                        continue
                    if candidate.get("id") == message.get("id") or (
                        "error" in candidate and candidate.get("id") is None
                    ):
                        reply = candidate
                        break
            except TRANSPORT_ERRORS as error:
                raise McpError(f"connection failed: {one_line(str(error))}") from error
        else:
            reply = parse_json_object(read_limited(response)) or None
        return RpcReply(status, response.headers, reply if isinstance(reply, dict) else None)


def www_authenticate(headers: Message) -> dict[str, str]:
    """Parameters of the Bearer challenge, which may share the header with other schemes."""
    value = ", ".join(headers.get_all("WWW-Authenticate") or [])
    bearer = re.search(r"(?:^|,)\s*bearer\b", value, re.IGNORECASE)
    params: dict[str, str] = {}
    if bearer is None:
        return params
    # A later challenge may repeat a parameter name; the Bearer one comes first.
    for name, quoted, token in AUTH_PARAM.findall(value, bearer.end()):
        params.setdefault(name.lower(), quoted or token)
    return params

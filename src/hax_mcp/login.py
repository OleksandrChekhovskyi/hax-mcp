from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import secrets
import select
import socket
import subprocess
import sys
import time
import urllib.parse
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Optional, cast

from . import JsonObject, JsonValue, McpError
from .auth import apply_tokens, locked_credentials, oauth_error_text, token_request
from .config import Server
from .protocol import modern_request
from .text import one_line
from .transport import (
    fetch_json,
    http_request,
    parse_json_object,
    post_rpc,
    require_secure,
    www_authenticate,
)

# The metadata document at CLIENT_ID_URL lists these redirect ports; change both together.
CLIENT_ID_URL: Final = "https://usehax.dev/hax-mcp.json"
REDIRECT_PORTS: Final = (28471, 28472)
LOGIN_TIMEOUT_S: Final = 300


@dataclass
class Authorization:
    issuer: str
    metadata: JsonObject
    resource: str
    scope: Optional[str]


def origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}".lower()


def well_known_urls(base: str, kind: str) -> list[str]:
    # RFC 8414 and RFC 9728 drop only a bare "/" path; a trailing slash after a path is significant.
    path = urllib.parse.urlsplit(base).path
    path = "" if path == "/" else path
    urls = [f"{origin(base)}/.well-known/{kind}{path}"]
    if path and kind == "oauth-protected-resource":
        urls.append(f"{origin(base)}/.well-known/{kind}")
    if path and kind == "openid-configuration":
        urls.append(f"{origin(base)}{path.rstrip('/')}/.well-known/{kind}")
    return urls


def first_json(urls: Sequence[str]) -> Optional[JsonObject]:
    for url in urls:
        data = fetch_json(url)
        if data is not None:
            return data
    return None


def discover_authorization(server: Server) -> Authorization:
    message, headers = modern_request(1, "tools/list", {})
    reply = post_rpc(server.url, message, {**server.headers, **headers})
    challenge = www_authenticate(reply.headers) if reply.status == 401 else {}
    metadata_url = challenge.get("resource_metadata")
    resource_metadata = first_json(
        [metadata_url] if metadata_url else well_known_urls(server.url, "oauth-protected-resource")
    )

    scope = challenge.get("scope")
    if resource_metadata is not None:
        resource = resource_metadata.get("resource", server.url)
        servers = resource_metadata.get("authorization_servers")
        if not isinstance(resource, str) or origin(resource) != origin(server.url):
            raise McpError(f"{server.name}: resource metadata names a different server")
        if not isinstance(servers, list) or not servers or not isinstance(servers[0], str):
            raise McpError(f"{server.name}: resource metadata names no authorization server")
        issuer = servers[0]
        supported = resource_metadata.get("scopes_supported")
        if not scope and isinstance(supported, list):
            scope = " ".join(s for s in supported if isinstance(s, str))
    elif reply.status == 401:
        # Servers from before 2025-06-18 host authorization metadata at their own origin.
        resource, issuer = server.url, origin(server.url)
    else:
        raise McpError(f"{server.name}: server does not advertise OAuth login")

    require_secure(issuer)
    metadata = first_json(
        well_known_urls(issuer, "oauth-authorization-server")
        + well_known_urls(issuer, "openid-configuration")
    )
    if metadata is None:
        raise McpError(f"{server.name}: no authorization server metadata at {issuer}")
    if str(metadata.get("issuer", "")).rstrip("/") != issuer.rstrip("/"):
        raise McpError(f"{server.name}: authorization server metadata has a mismatched issuer")
    for endpoint in ("authorization_endpoint", "token_endpoint"):
        if not isinstance(metadata.get(endpoint), str):
            raise McpError(f"{server.name}: authorization server metadata lacks {endpoint}")
        require_secure(cast(str, metadata[endpoint]))
    methods = metadata.get("code_challenge_methods_supported")
    if not isinstance(methods, list) or "S256" not in methods:
        raise McpError(f"{server.name}: authorization server does not support PKCE")
    return Authorization(cast(str, metadata["issuer"]), metadata, resource, scope or None)


def uses_metadata_document(auth: Authorization) -> bool:
    return auth.metadata.get("client_id_metadata_document_supported") is True


def client_entry(auth: Authorization, client_id: str, auth_method: JsonValue) -> JsonObject:
    return {
        "issuer": auth.issuer,
        "token_endpoint": auth.metadata["token_endpoint"],
        "resource": auth.resource,
        "client_id": client_id,
        "auth_method": auth_method,
    }


def register_client(auth: Authorization, redirect_uri: str) -> JsonObject:
    if uses_metadata_document(auth):
        return client_entry(auth, CLIENT_ID_URL, "none")
    endpoint = auth.metadata.get("registration_endpoint")
    if not isinstance(endpoint, str):
        raise McpError(
            "the authorization server supports no client registration hax-mcp can use; "
            'configure an access token in "headers" instead'
        )
    methods = auth.metadata.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list) or "none" in methods:
        method = "none"
    elif "client_secret_post" in methods:
        method = "client_secret_post"
    else:
        method = "client_secret_basic"
    request: JsonObject = {
        "client_name": "hax-mcp",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": method,
    }
    if auth.scope:
        request["scope"] = auth.scope
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    reply = http_request(require_secure(endpoint), json.dumps(request).encode(), headers, "POST")
    data = parse_json_object(reply.body)
    if reply.status not in (200, 201) or not isinstance(data.get("client_id"), str):
        raise McpError(f"client registration failed: {oauth_error_text(data, reply.status)}")
    client_id = cast(str, data["client_id"])
    client = client_entry(auth, client_id, data.get("token_endpoint_auth_method", method))
    if isinstance(data.get("client_secret"), str):
        client["client_secret"] = data["client_secret"]
    return client


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode()).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


def open_browser(url: str) -> None:
    if sys.platform == "darwin":
        command = ["open", url]
    elif os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        command = ["xdg-open", url]
    else:
        return
    with contextlib.suppress(OSError):
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def query_params(url: str) -> dict[str, str]:
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url.strip()).query))


def serve_redirect(listener: socket.socket, state: str) -> Optional[dict[str, str]]:
    """Answer one connection; return its query when it is this login's redirect."""
    connection, _ = listener.accept()
    with connection:
        connection.settimeout(5)
        request = b""
        with contextlib.suppress(OSError):
            while b"\r\n\r\n" not in request and len(request) < 8192:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                request += chunk
        target = request.split(b"\r\n", 1)[0].split(b" ")
        params = None
        status, text = "404 Not Found", "Not found."
        if len(target) == 3 and target[0] == b"GET":
            url = target[1].decode("latin-1")
            if urllib.parse.urlsplit(url).path == "/callback":
                candidate = query_params(url)
                if candidate.get("state") == state:
                    params = candidate
                    status, text = "200 OK", "Login received. You can close this tab."
                else:
                    status, text = "400 Bad Request", "This redirect is not for the pending login."
        body = f"<!doctype html><title>hax-mcp</title><p>{text}</p>".encode()
        head = (
            f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        with contextlib.suppress(OSError):
            connection.sendall(head.encode() + body)
        return params


def open_listener(ports: Sequence[int]) -> socket.socket:
    for port in ports:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Each served redirect leaves its port in TIME_WAIT, which would block the next login on
        # the fixed metadata-document ports. This does not let two listeners share a port.
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", port))
            listener.listen(4)
            return listener
        except OSError:
            listener.close()
    raise McpError(f"cannot listen for the login redirect on 127.0.0.1 ports {list(ports)}")


def wait_for_redirect(listener: socket.socket, state: str) -> dict[str, str]:
    # A browser on another machine (ssh) cannot reach this loopback listener, so the URL it
    # ends on may be pasted instead.
    deadline = time.monotonic() + LOGIN_TIMEOUT_S
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise McpError("timed out waiting for authorization")
        ready, _, _ = select.select([listener, sys.stdin], [], [], remaining)
        if sys.stdin in ready:
            line = sys.stdin.readline()
            if not line:
                raise McpError("login cancelled")
            params = query_params(line)
            if params.get("state") == state:
                return params
            print("That URL does not belong to this login; paste the full URL.", file=sys.stderr)
        if listener in ready:
            params = serve_redirect(listener, state)
            if params is not None:
                return params


def login(server: Server) -> None:
    if server.has_authorization():
        raise McpError(f"{server.name}: credentials are configured in headers; no login needed")
    if not sys.stdin.isatty():
        raise McpError("login is interactive; run it in a terminal")
    auth = discover_authorization(server)
    # A registered client gets whatever port is free; a metadata document lists fixed ones.
    ports = REDIRECT_PORTS if uses_metadata_document(auth) else (0,)
    with open_listener(ports) as listener:
        redirect_uri = f"http://127.0.0.1:{listener.getsockname()[1]}/callback"
        client = register_client(auth, redirect_uri)
        verifier, challenge = pkce_pair()
        state = secrets.token_urlsafe(32)
        query = {
            "response_type": "code",
            "client_id": str(client["client_id"]),
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "resource": auth.resource,
        }
        if auth.scope:
            query["scope"] = auth.scope
        endpoint = str(auth.metadata["authorization_endpoint"])
        separator = "&" if "?" in endpoint else "?"
        url = endpoint + separator + urllib.parse.urlencode(query)
        print(
            f"Open this URL to log in to {server.name}:\n\n  {one_line(url)}\n\n"
            "Waiting for the browser. If it cannot reach this machine, paste the URL it ends on:",
            file=sys.stderr,
        )
        open_browser(url)
        params = wait_for_redirect(listener, state)

    # RFC 9207: a response from another issuer is a mix-up attack, error responses included.
    returned_issuer = params.get("iss")
    iss_required = auth.metadata.get("authorization_response_iss_parameter_supported") is True
    if (returned_issuer or iss_required) and returned_issuer != auth.issuer:
        raise McpError("authorization response came from an unexpected issuer")
    if "error" in params:
        detail = one_line(params.get("error_description")) or one_line(params["error"])
        raise McpError(f"authorization failed: {detail}")
    if not params.get("code"):
        raise McpError("authorization response carried no code")

    form = {
        "grant_type": "authorization_code",
        "code": params["code"],
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
    }
    status, response = token_request(client, form)
    entry: JsonObject = {"url": server.url, **client}
    if status != 200 or not apply_tokens(entry, response):
        raise McpError(f"token exchange failed: {oauth_error_text(response, status)}")
    with locked_credentials() as credentials:
        credentials[server.name] = entry

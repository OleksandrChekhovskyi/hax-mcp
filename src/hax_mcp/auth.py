from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import time
import urllib.parse
from collections.abc import Iterator
from typing import Final, Optional, cast

from . import JsonObject, LoginRequired, McpError
from .config import Server, state_dir, write_json
from .text import one_line
from .transport import http_request, parse_json_object, require_secure

REFRESH_MARGIN_S: Final = 60
TERMINAL_OAUTH_ERRORS: Final = {"invalid_grant", "invalid_client", "unauthorized_client"}


def credentials_path() -> str:
    return os.path.join(state_dir(), "credentials.json")


def read_credentials() -> dict[str, JsonObject]:
    try:
        with open(credentials_path(), encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    except ValueError:
        return {}
    except OSError as error:
        raise McpError(f"cannot read {credentials_path()}: {error}") from error
    if not isinstance(data, dict):
        return {}
    return {name: entry for name, entry in data.items() if isinstance(entry, dict)}


@contextlib.contextmanager
def locked_credentials() -> Iterator[dict[str, JsonObject]]:
    """Yield the stored credentials for one read-modify-write transaction.

    Concurrent hax-mcp processes serialize here, so a rotating refresh token is redeemed once.
    Changes are written when the body completes; an exception discards them.
    """
    directory = state_dir()
    os.makedirs(directory, mode=0o700, exist_ok=True)
    with open(os.path.join(directory, "credentials.lock"), "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        credentials = read_credentials()
        snapshot = json.dumps(credentials, sort_keys=True)
        yield credentials
        if json.dumps(credentials, sort_keys=True) != snapshot:
            write_json(credentials_path(), cast(JsonObject, credentials))


def load_login(server: Server) -> Optional[JsonObject]:
    if server.has_authorization():
        return None
    entry = read_credentials().get(server.name)
    # A login is bound to the URL it was issued for; a renamed endpoint must not receive it.
    if entry is None or entry.get("url") != server.url:
        return None
    return entry if isinstance(entry.get("access_token"), str) else None


def token_expiring(entry: JsonObject) -> bool:
    expires_at = entry.get("expires_at")
    return isinstance(expires_at, (int, float)) and expires_at - time.time() < REFRESH_MARGIN_S


def login_required(server: str, problem: str = "login required") -> str:
    return f"{server}: {problem}; the user must run `hax-mcp login {server}` in a terminal"


def oauth_error_text(data: JsonObject, status: int) -> str:
    text = one_line(data.get("error_description")) or one_line(data.get("error"))
    return (text or f"HTTP {status}")[:200]


def token_request(client: JsonObject, form: dict[str, str]) -> tuple[int, JsonObject]:
    form = {**form, "resource": str(client["resource"])}
    headers = {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"}
    client_id = str(client["client_id"])
    secret = str(client.get("client_secret", ""))
    if client.get("auth_method") == "client_secret_basic":
        # RFC 6749 section 2.3.1 form-encodes both parts before joining them.
        pair = f"{urllib.parse.quote_plus(client_id)}:{urllib.parse.quote_plus(secret)}"
        headers["Authorization"] = "Basic " + base64.b64encode(pair.encode()).decode()
    else:
        form["client_id"] = client_id
        if client.get("auth_method") == "client_secret_post":
            form["client_secret"] = secret
    url = require_secure(str(client["token_endpoint"]))
    reply = http_request(url, urllib.parse.urlencode(form).encode(), headers, "POST")
    return reply.status, parse_json_object(reply.body)


def apply_tokens(entry: JsonObject, response: JsonObject) -> bool:
    """Merge a token response into `entry`. Returns False, leaving it unchanged, when unusable."""
    access_token = response.get("access_token")
    token_type = response.get("token_type", "bearer")
    if not isinstance(access_token, str) or str(token_type).lower() != "bearer":
        return False
    entry["access_token"] = access_token
    if isinstance(response.get("refresh_token"), str):
        entry["refresh_token"] = response["refresh_token"]
    expires_in = response.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        entry["expires_at"] = int(time.time() + expires_in)
    else:
        entry.pop("expires_at", None)
    return True


def refresh_login(server: Server, seen: JsonObject, force: bool) -> JsonObject:
    """Return a usable login, refreshing it when expiring or, with `force`, unconditionally.

    Another process may have refreshed the login since `seen` was read; its token is adopted
    rather than redeeming the rotated refresh token again. Only an explicit OAuth rejection
    removes the login: proxies produce 4xx statuses that say nothing about the grant.
    """
    expired = LoginRequired(login_required(server.name, "login expired"))
    status, response, rejected = 0, cast(JsonObject, {}), False
    with locked_credentials() as credentials:
        entry = credentials.get(server.name)
        if entry is None or entry.get("url") != server.url:
            raise expired
        if entry.get("access_token") != seen.get("access_token"):
            return entry
        if not force and not token_expiring(entry):
            return entry
        refresh_token = entry.get("refresh_token")
        if not isinstance(refresh_token, str):
            # Without a refresh token, the token stays in use until the server rejects it.
            if not force:
                return entry
            raise expired
        form = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        status, response = token_request(entry, form)
        if status == 200 and apply_tokens(entry, response):
            return entry
        rejected = status in (400, 401) and response.get("error") in TERMINAL_OAUTH_ERRORS
        if rejected:
            del credentials[server.name]
    if rejected:
        raise expired
    raise McpError(f"{server.name}: token refresh failed: {oauth_error_text(response, status)}")


def logout(name: str) -> bool:
    with locked_credentials() as credentials:
        return credentials.pop(name, None) is not None

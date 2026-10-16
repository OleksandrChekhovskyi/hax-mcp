from __future__ import annotations

import base64
import time
import unittest
import urllib.parse
from email.message import Message
from unittest import mock

from support import Json, McpTestCase
from hax_mcp import LoginRequired, McpError, auth, protocol, transport


class StoreTests(McpTestCase):
    def test_login_is_bound_to_its_url(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t", url="https://elsewhere.example/mcp")
        self.assertIsNone(auth.load_login(self.server()))

    def test_configured_authorization_overrides_stored_login(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t")
        headers = {"Authorization": "Bearer static"}
        self.configure({"remote": {"url": f"{fake.base}/mcp", "headers": headers}})
        self.assertIsNone(auth.load_login(self.server()))

    def test_logout_reports_whether_a_login_was_removed(self) -> None:
        fake = self.serve()
        self.store_login(fake, access_token="t")
        self.assertTrue(auth.logout("remote"))
        self.assertFalse(auth.logout("remote"))


class TokenRequestTests(unittest.TestCase):
    def send(self, client: Json) -> tuple[dict[str, str], dict[str, str]]:
        reply = transport.Reply(200, Message(), b"{}")
        client = {"resource": "r", "token_endpoint": "https://t.example", **client}
        with mock.patch.object(auth, "http_request", return_value=reply) as request:
            auth.token_request(client, {})
        _, body, headers, _ = request.call_args.args
        return headers, dict(urllib.parse.parse_qsl(body.decode()))

    def test_basic_authentication_form_encodes_credentials(self) -> None:
        client = {"client_id": "a b", "client_secret": "s:t", "auth_method": "client_secret_basic"}
        headers, form = self.send(client)
        credentials = base64.b64encode(b"a+b:s%3At").decode()
        self.assertEqual(headers["Authorization"], f"Basic {credentials}")
        self.assertEqual(form, {"resource": "r"})

    def test_post_authentication_sends_secret_in_form(self) -> None:
        client = {"client_id": "c", "client_secret": "s", "auth_method": "client_secret_post"}
        headers, form = self.send(client)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(form, {"resource": "r", "client_id": "c", "client_secret": "s"})


class RefreshTests(McpTestCase):
    def list_tools(self) -> None:
        with protocol.Session(self.server()) as session:
            protocol.list_tools(session)

    def test_expiring_token_is_refreshed_before_use(self) -> None:
        fake = self.serve(token="token-2")
        self.store_login(fake, access_token="token-1", refresh_token="refresh-1", expires_at=0)
        self.list_tools()
        self.assertEqual(fake.methods(), ["tools/list"])
        entry = self.credentials()["remote"]
        self.assertEqual((entry["access_token"], entry["refresh_token"]), ("token-2", "refresh-2"))

    def test_expiring_token_without_refresh_token_is_used(self) -> None:
        fake = self.serve(token="token-1")
        self.store_login(fake, access_token="token-1", expires_at=time.time() + 30)
        self.list_tools()
        self.assertEqual(fake.methods(), ["tools/list"])

    def test_rejected_token_is_refreshed_once(self) -> None:
        fake = self.serve(token="token-2")
        self.store_login(fake, access_token="stale", refresh_token="refresh-1")
        self.list_tools()
        self.assertEqual(fake.methods(), ["tools/list", "tools/list"])
        self.assertEqual(len(fake.token_forms), 1)

    def test_revoked_refresh_token_removes_login(self) -> None:
        fake = self.serve(token="token-2")
        self.store_login(fake, access_token="stale", refresh_token="revoked")
        with self.assertRaisesRegex(LoginRequired, "login expired"):
            self.list_tools()
        self.assertNotIn("remote", self.credentials())

    def test_transient_refresh_failure_keeps_login(self) -> None:
        fake = self.serve(token="token-2")
        self.store_login(fake, access_token="stale", refresh_token="refresh-1")
        with mock.patch.object(auth, "token_request", return_value=(503, {})):
            with self.assertRaisesRegex(McpError, "refresh failed: HTTP 503"):
                self.list_tools()
        self.assertEqual(self.credentials()["remote"]["access_token"], "stale")

    def test_refresh_adopts_token_rotated_by_another_process(self) -> None:
        fake = self.serve(token="token-9")
        self.store_login(fake, access_token="token-9", refresh_token="refresh-9")
        entry = auth.refresh_login(self.server(), {"access_token": "token-1"}, force=True)
        self.assertEqual(entry["access_token"], "token-9")
        self.assertEqual(fake.token_forms, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import io
import socket
import threading
import unittest
import urllib.parse
import urllib.request
from unittest import mock

from support import FakeServer, FakeTty, McpTestCase
from hax_mcp import McpError, login


class DiscoveryTests(unittest.TestCase):
    def test_well_known_urls_follow_discovery_order(self) -> None:
        self.assertEqual(
            login.well_known_urls("https://a.example/mcp/", "oauth-protected-resource"),
            [
                "https://a.example/.well-known/oauth-protected-resource/mcp/",
                "https://a.example/.well-known/oauth-protected-resource",
            ],
        )
        self.assertEqual(
            login.well_known_urls("https://a.example/tenant/", "openid-configuration"),
            [
                "https://a.example/.well-known/openid-configuration/tenant/",
                "https://a.example/tenant/.well-known/openid-configuration",
            ],
        )
        for root in ("https://a.example", "https://a.example/"):
            self.assertEqual(
                login.well_known_urls(root, "oauth-authorization-server"),
                ["https://a.example/.well-known/oauth-authorization-server"],
            )


class LoginTests(McpTestCase):
    def browse(self, follow: bool, tty: FakeTty) -> list[threading.Thread]:
        """Stand in for the browser: approve the authorization, then reach the redirect target
        directly when `follow` is set, or have the user paste the final URL into `tty`."""
        threads: list[threading.Thread] = []

        def visit(url: str) -> None:
            if follow:
                with urllib.request.urlopen(url, timeout=10) as response:
                    response.read()
                return
            parts = urllib.parse.urlsplit(url)
            connection = http.client.HTTPConnection(parts.netloc, timeout=10)
            connection.request("GET", f"{parts.path}?{parts.query}")
            location = connection.getresponse().getheader("Location")
            connection.close()
            assert location is not None
            tty.type(location + "\n")

        def open_browser(url: str) -> None:
            thread = threading.Thread(target=visit, args=(url,))
            thread.start()
            threads.append(thread)

        patch = mock.patch.object(login, "open_browser", open_browser)
        patch.start()
        self.addCleanup(patch.stop)
        return threads

    def login(self, follow: bool, cimd: bool = False) -> FakeServer:
        fake = self.serve(token="unset", cimd=cimd)
        tty = FakeTty()
        self.addCleanup(tty.close)
        threads = self.browse(follow, tty)
        with mock.patch("sys.stdin", tty), mock.patch("sys.stderr", io.StringIO()):
            login.login(self.server())
        for thread in threads:
            thread.join()
        return fake

    def test_login_through_loopback_redirect(self) -> None:
        fake = self.login(follow=True)
        entry = self.credentials()["remote"]
        self.assertEqual((entry["access_token"], entry["refresh_token"]), ("token-2", "refresh-2"))
        self.assertEqual(entry["client_id"], "client-1")
        registration = next(body for method, _, body in fake.log if method == "register")
        self.assertEqual(registration["redirect_uris"], [fake.authorize_query["redirect_uri"]])
        self.assertEqual(fake.authorize_query["scope"], "read")
        self.assertEqual(fake.authorize_query["resource"], f"{fake.base}/mcp")

    def test_login_accepts_pasted_redirect(self) -> None:
        self.login(follow=False)
        self.assertEqual(self.credentials()["remote"]["access_token"], "token-2")

    def test_login_prefers_client_metadata_document(self) -> None:
        with socket.socket() as busy, socket.socket() as probe:
            busy.bind(("127.0.0.1", 0))
            probe.bind(("127.0.0.1", 0))
            ports = (busy.getsockname()[1], probe.getsockname()[1])
            probe.close()
            with mock.patch.object(login, "REDIRECT_PORTS", ports):
                fake = self.login(follow=True, cimd=True)
        self.assertNotIn("register", fake.methods())
        self.assertEqual(fake.authorize_query["client_id"], login.CLIENT_ID_URL)
        redirect = f"http://127.0.0.1:{ports[1]}/callback"
        self.assertEqual(fake.authorize_query["redirect_uri"], redirect)
        self.assertEqual(self.credentials()["remote"]["client_id"], login.CLIENT_ID_URL)

    def test_login_rejects_response_from_another_issuer(self) -> None:
        fake = self.serve(token="unset")
        fake.issuer_param = "https://attacker.example"
        tty = FakeTty()
        self.addCleanup(tty.close)
        threads = self.browse(True, tty)
        with mock.patch("sys.stdin", tty), mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaisesRegex(McpError, "unexpected issuer"):
                login.login(self.server())
        for thread in threads:
            thread.join()
        self.assertEqual(fake.token_forms, [])

    def test_registration_takes_a_secret_without_public_clients(self) -> None:
        fake = self.serve(token="unset")
        fake.auth_methods = ["client_secret_basic", "client_secret_post"]
        auth = login.discover_authorization(self.server())
        client = login.register_client(auth, "http://127.0.0.1:1/callback")
        registration = next(body for method, _, body in fake.log if method == "register")
        self.assertEqual(registration["token_endpoint_auth_method"], "client_secret_post")
        self.assertEqual(client["auth_method"], "client_secret_post")
        self.assertEqual(client["client_secret"], "s")

    def test_listener_port_is_reusable_after_a_redirect(self) -> None:
        with login.open_listener((0,)) as listener:
            port = listener.getsockname()[1]
            with socket.create_connection(("127.0.0.1", port)) as client:
                client.sendall(b"GET /callback?state=s HTTP/1.1\r\n\r\n")
                self.assertIsNotNone(login.serve_redirect(listener, "s"))
                client.recv(4096)
        login.open_listener((port,)).close()

    def test_login_requires_a_terminal(self) -> None:
        self.serve(token="unset")
        with mock.patch("sys.stdin", io.StringIO()):
            with self.assertRaisesRegex(McpError, "interactive"):
                login.login(self.server())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import http.client
import io
import unittest
import urllib.request
from email.message import Message
from unittest import mock

from support import McpTestCase
from hax_mcp import McpError, transport


class PostTests(McpTestCase):
    def test_event_stream_errors_without_id_are_the_reply(self) -> None:
        fake = self.serve()
        reply = transport.post_rpc(f"{fake.base}/stream-error", {"id": 7}, {})
        self.assertEqual(reply.status, 400)
        error = {"code": -32020, "message": "mismatch"}
        self.assertEqual(reply.message, {"jsonrpc": "2.0", "error": error})

    def test_posts_do_not_follow_redirects(self) -> None:
        fake = self.serve()
        reply = transport.post_rpc(f"{fake.base}/moved", {"id": 1}, {"Authorization": "Bearer t"})
        self.assertEqual((reply.status, reply.message), (307, None))
        self.assertEqual(fake.methods(), [])


class TransportTests(unittest.TestCase):
    def test_redirects_to_insecure_urls_are_refused(self) -> None:
        handler = transport.SecureRedirect()
        request = urllib.request.Request("https://a.example/.well-known/x")
        body, headers = io.BytesIO(), http.client.HTTPMessage()
        for target, followed in (("https://b.example/x", True), ("http://b.example/x", False)):
            redirect = handler.redirect_request(request, body, 302, "Found", headers, target)
            self.assertEqual(redirect is not None, followed, target)

    def test_line_reads_stop_at_the_response_limit(self) -> None:
        stream = io.BytesIO(b"data: " + b"x" * 100)
        with mock.patch.object(transport, "MAX_RESPONSE_BYTES", 10):
            with self.assertRaisesRegex(McpError, "exceeded"):
                list(transport.limited_lines(stream))
        self.assertEqual(stream.tell(), 11)

    def test_plain_http_is_limited_to_loopback(self) -> None:
        self.assertEqual(transport.require_secure("http://127.0.0.1/"), "http://127.0.0.1/")
        self.assertEqual(transport.require_secure("https://example.com"), "https://example.com")
        with self.assertRaisesRegex(McpError, "non-HTTPS"):
            transport.require_secure("http://example.com/mcp")

    def test_sse_messages_join_data_lines_and_skip_comments(self) -> None:
        lines = [b": ping\n", b'data: {"a":\n', b"data: 1}\n", b"\n", b"data: [2]"]
        self.assertEqual(list(transport.sse_messages(lines)), [{"a": 1}, [2]])

    def test_www_authenticate_reads_quoted_and_bare_parameters(self) -> None:
        headers = Message()
        headers["WWW-Authenticate"] = 'Bearer resource_metadata="https://x/y", Scope="a b", e=t'
        self.assertEqual(
            transport.www_authenticate(headers),
            {"resource_metadata": "https://x/y", "scope": "a b", "e": "t"},
        )

    def test_www_authenticate_picks_the_bearer_challenge(self) -> None:
        separate, combined, basic_only = Message(), Message(), Message()
        separate["WWW-Authenticate"] = 'Basic realm="r", scope="basic"'
        separate["WWW-Authenticate"] = 'Bearer resource_metadata="https://x/y", scope="s"'
        combined["WWW-Authenticate"] = 'Basic realm="r", bearer scope="s", resource_metadata=u'
        basic_only["WWW-Authenticate"] = 'Basic realm="r", scope="basic"'
        expected = {"resource_metadata": "https://x/y", "scope": "s"}
        self.assertEqual(transport.www_authenticate(separate), expected)
        combined_expected = {**expected, "resource_metadata": "u"}
        self.assertEqual(transport.www_authenticate(combined), combined_expected)
        self.assertEqual(transport.www_authenticate(basic_only), {})


if __name__ == "__main__":
    unittest.main()

"""lm-42: opp-axi prefers the tool gateway, and a gateway that did not answer
never renders as "no results".

The contract under test is the one `docs/tool-gateway.md` wrote down and that is
easy to misread:

    LAWNMOWER_GATEWAY unset  ->  shell out locally, exactly as today
    LAWNMOWER_GATEWAY set    ->  call the gateway; on 5xx or timeout, say so and fail

"Falls back to the local CLI" describes the UNSET case. It is not a per-call
error handler, and a test that allowed a silent retry against `sf` would let the
gateway be broken for a week while every call quietly used the credential that
was supposed to have moved.

These drive a real HTTP server on a loopback port rather than mocking urllib, so
they exercise the actual request, headers and status handling.
"""

import json
import os
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from opp_axi import cli, gateway

TOKEN = "g" * 40


class _Handler(BaseHTTPRequestHandler):
    """Scripted gateway. `server.script` decides what comes back."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        self.server.last_path = self.path
        self.server.last_auth = self.headers.get("Authorization")
        self.server.last_ua = self.headers.get("User-Agent")
        self.server.last_cf_id = self.headers.get("CF-Access-Client-Id")
        self.server.last_cf_secret = self.headers.get("CF-Access-Client-Secret")
        length = int(self.headers.get("Content-Length") or 0)
        self.server.last_body = json.loads(self.rfile.read(length) or b"{}")
        status, payload = self.server.script
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GatewayClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
        cls.httpd.last_ua = None
        cls.httpd.last_cf_id = None
        cls.httpd.last_cf_secret = None
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def env(self, **extra):
        e = {"LAWNMOWER_GATEWAY": self.url, "LAWNMOWER_GATEWAY_TOKEN": TOKEN}
        e.update(extra)
        return mock.patch.dict(os.environ, e, clear=True)

    # -- routing ---------------------------------------------------------

    def test_unset_gateway_means_no_endpoint(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(gateway.endpoint())

    def test_blank_gateway_means_no_endpoint(self):
        for v in ("", "   "):
            with mock.patch.dict(os.environ, {"LAWNMOWER_GATEWAY": v}, clear=True):
                self.assertIsNone(gateway.endpoint())

    def test_trailing_slash_is_trimmed(self):
        with mock.patch.dict(os.environ, {"LAWNMOWER_GATEWAY": "http://x/"}, clear=True):
            self.assertEqual(gateway.endpoint(), "http://x")

    def test_unset_gateway_uses_the_local_cli(self):
        """The whole migration story: unset is today's behaviour, untouched."""
        fake = mock.Mock(returncode=0, stdout=json.dumps(
            {"status": 0, "result": {"records": [{"Id": "006local"}]}}), stderr="")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch.object(cli, "_run", return_value=fake) as m:
                out = cli.sf_query("SELECT Id FROM Opportunity")
        self.assertEqual(out, [{"Id": "006local"}])
        argv = m.call_args[0][0]
        self.assertEqual(argv[:3], ["sf", "data", "query"])

    def test_set_gateway_does_not_touch_the_local_cli(self):
        self.httpd.script = (200, {"records": [{"Id": "006gw"}], "totalSize": 1})
        with self.env():
            with mock.patch.object(cli, "_run") as m:
                out = cli.sf_query("SELECT Id FROM Opportunity")
        m.assert_not_called()
        self.assertEqual(out, [{"Id": "006gw"}])
        self.assertEqual(self.httpd.last_path, "/sf/query")
        self.assertEqual(self.httpd.last_auth, f"Bearer {TOKEN}")
        self.assertEqual(self.httpd.last_body, {"soql": "SELECT Id FROM Opportunity"})

    # -- the contract ----------------------------------------------------

    def test_a_gateway_error_dies_and_never_falls_back_to_sf(self):
        """If this ever starts falling back, the gateway can be broken for a week
        while every call silently uses the credential that was meant to move."""
        self.httpd.script = (502, {"error": {"code": "sf_error", "message": "INVALID_SESSION_ID"}})
        with self.env():
            with mock.patch.object(cli, "_run") as m:
                with self.assertRaises(SystemExit):
                    cli.sf_query("SELECT Id FROM Opportunity")
        m.assert_not_called()

    def test_an_unreachable_gateway_dies_rather_than_returning_empty(self):
        with mock.patch.dict(os.environ, {
            "LAWNMOWER_GATEWAY": "http://127.0.0.1:1",
            "LAWNMOWER_GATEWAY_TOKEN": TOKEN,
        }, clear=True):
            with mock.patch.object(cli, "_run") as m:
                with self.assertRaises(SystemExit):
                    cli.sf_query("SELECT Id FROM Opportunity")
        m.assert_not_called()

    def test_the_error_message_names_the_gateway(self):
        """'sf query failed' when the real cause is a dead gateway sends whoever
        reads it to the wrong machine."""
        with mock.patch.dict(os.environ, {
            "LAWNMOWER_GATEWAY": "http://127.0.0.1:1",
            "LAWNMOWER_GATEWAY_TOKEN": TOKEN,
        }, clear=True):
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.sf_query("SELECT Id FROM Opportunity")
        self.assertIn("did not answer", str(cm.exception))
        self.assertIn("127.0.0.1:1", str(cm.exception))

    def test_a_200_without_records_is_not_an_empty_result(self):
        self.httpd.script = (200, {"totalSize": 0})
        with self.env():
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.sf_query("SELECT Id FROM Opportunity")
        self.assertIn("refusing to read that as an empty result", str(cm.exception))

    def test_non_json_from_the_gateway_raises(self):
        self.httpd.script = (200, b"<html>502 Bad Gateway</html>")
        with self.env():
            with self.assertRaises(gateway.GatewayError):
                gateway.sf_query("SELECT Id FROM Opportunity")

    def test_a_genuine_zero_match_returns_an_empty_list(self):
        """The other half: empty must still be readable as empty."""
        self.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
        with self.env():
            self.assertEqual(cli.sf_query("SELECT Id FROM Opportunity WHERE Id='000'"), [])

    def test_a_refusal_relays_the_gateways_reason(self):
        self.httpd.script = (400, {"error": {
            "code": "soql_rejected",
            "message": "soql must begin with SELECT; this gateway is read-only"}})
        with self.env():
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.sf_query("DELETE FROM Opportunity")
        self.assertIn("read-only", str(cm.exception))

    # -- user agent -------------------------------------------------------

    def test_a_named_user_agent_is_sent_not_pythons_default(self):
        """Cloudflare's Browser Integrity Check 403s `Python-urllib/*` with error
        1010, before Access or the gateway sees the request -- a valid service
        token does not save you. Measured 9/18/26 against the live hostname."""
        self.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
        with self.env():
            gateway.sf_query("SELECT Id FROM Opportunity")
        ua = self.httpd.last_ua
        self.assertTrue(ua)
        self.assertNotIn("Python-urllib", ua)
        self.assertIn("opp-axi", ua)

    def test_a_1010_block_is_named_rather_than_reported_as_a_refusal(self):
        self.httpd.script = (403, b"error code: 1010")
        with self.env():
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.sf_query("SELECT Id FROM Opportunity")
        msg = str(cm.exception)
        self.assertIn("Browser Integrity Check", msg)
        self.assertIn("User-Agent", msg)

    # -- cloudflare access ----------------------------------------------

    def test_access_headers_are_sent_when_configured(self):
        self.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
        with self.env(CF_ACCESS_CLIENT_ID="cid-123", CF_ACCESS_CLIENT_SECRET="sec-456"):
            gateway.sf_query("SELECT Id FROM Opportunity")
        self.assertEqual(self.httpd.last_cf_id, "cid-123")
        self.assertEqual(self.httpd.last_cf_secret, "sec-456")

    def test_no_access_credential_sends_no_headers(self):
        """A direct in-cluster call has no Access in front of it, so absent
        credentials must not become an error or an empty header."""
        self.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
        with mock.patch.dict(os.environ, {
            "LAWNMOWER_GATEWAY": self.url,
            "LAWNMOWER_GATEWAY_TOKEN": TOKEN,
            "LAWNMOWER_GATEWAY_CF_ACCESS_FILE": "/nonexistent/cf",
        }, clear=True):
            gateway.sf_query("SELECT Id FROM Opportunity")
        self.assertIsNone(self.httpd.last_cf_id)

    def test_access_credentials_are_read_from_the_file(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".cf", delete=False) as fh:
            fh.write("# lm-42\nCF_ACCESS_CLIENT_ID=file-cid\nCF_ACCESS_CLIENT_SECRET=file-sec\n")
            path = fh.name
        try:
            self.httpd.script = (200, {"records": [], "totalSize": 0, "done": True})
            with mock.patch.dict(os.environ, {
                "LAWNMOWER_GATEWAY": self.url,
                "LAWNMOWER_GATEWAY_TOKEN": TOKEN,
                "LAWNMOWER_GATEWAY_CF_ACCESS_FILE": path,
            }, clear=True):
                gateway.sf_query("SELECT Id FROM Opportunity")
            self.assertEqual(self.httpd.last_cf_id, "file-cid")
            self.assertEqual(self.httpd.last_cf_secret, "file-sec")
        finally:
            os.unlink(path)

    def test_a_followed_access_login_page_is_named_too(self):
        """urlopen follows the redirect, so the common case is a 200 of HTML
        from cloudflareaccess.com rather than an HTTPError. Reported as "not
        JSON" it reads like the gateway misbehaving; the request never got
        there. This is what actually happened on 9/18/26."""
        self.httpd.script = (200, b"<html>Sign in</html>")
        with self.env():
            with mock.patch.object(
                gateway.urllib.request, "urlopen", wraps=gateway.urllib.request.urlopen
            ):
                # simulate the followed redirect by reporting a cloudflareaccess URL
                real = gateway.urllib.request.urlopen

                class _Resp:
                    def __init__(self, inner): self._i = inner
                    def __enter__(self): return self
                    def __exit__(self, *a): self._i.close()
                    def read(self): return b"<html>Sign in</html>"
                    def geturl(self): return "https://craigcloud.cloudflareaccess.com/cdn-cgi/access/login/toolgw"

                with mock.patch.object(gateway.urllib.request, "urlopen",
                                       lambda req, timeout=None: _Resp(real(req, timeout=timeout))):
                    with self.assertRaises(gateway.GatewayError) as cm:
                        gateway.sf_query("SELECT Id FROM Opportunity")
        msg = str(cm.exception)
        self.assertIn("Cloudflare Access", msg)
        self.assertIn("CF_ACCESS_CLIENT_ID", msg)

    def test_an_access_login_redirect_says_so_rather_than_blaming_the_gateway(self):
        """Cloudflare answers 302 to the login page when no service token is
        sent. Reported as a generic refusal it sends you to read gateway logs
        that contain nothing, because the request never reached the gateway."""
        self.httpd.script = (302, b"")
        with self.env():
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.sf_query("SELECT Id FROM Opportunity")
        msg = str(cm.exception)
        self.assertIn("Cloudflare Access", msg)
        self.assertIn("CF_ACCESS_CLIENT_ID", msg)

    # -- token -----------------------------------------------------------

    def test_token_comes_from_the_file_when_the_env_is_unset(self):
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".token", delete=False) as fh:
            fh.write(TOKEN + "\n")
            path = fh.name
        try:
            with mock.patch.dict(os.environ, {
                "LAWNMOWER_GATEWAY": self.url,
                "LAWNMOWER_GATEWAY_TOKEN_FILE": path,
            }, clear=True):
                self.assertEqual(gateway.token(), TOKEN)  # newline stripped
        finally:
            os.unlink(path)

    def test_no_token_anywhere_is_a_named_failure_not_a_401(self):
        with mock.patch.dict(os.environ, {
            "LAWNMOWER_GATEWAY": self.url,
            "LAWNMOWER_GATEWAY_TOKEN_FILE": "/nonexistent/token",
        }, clear=True):
            with self.assertRaises(gateway.GatewayError) as cm:
                gateway.token()
        self.assertIn("no token was found", str(cm.exception))


if __name__ == "__main__":
    unittest.main()

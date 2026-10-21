import unittest

from auth import ForwardedProto, get_client_ip, trusted_proxies_from


class Request:
    def __init__(self, remote_addr, forwarded=None):
        self.remote_addr = remote_addr
        self.headers = {}
        if forwarded is not None:
            self.headers["X-Forwarded-For"] = forwarded


class ClientIpTests(unittest.TestCase):
    def test_ignores_forwarded_header_from_untrusted_client(self):
        request = Request("198.51.100.20", "203.0.113.8")
        self.assertEqual(get_client_ip(request), "198.51.100.20")

    def test_uses_rightmost_forwarded_value_from_trusted_proxy(self):
        request = Request("127.0.0.1", "203.0.113.8, 198.51.100.20")
        self.assertEqual(get_client_ip(request), "198.51.100.20")

    def test_uses_remote_addr_without_forwarded_header(self):
        self.assertEqual(get_client_ip(Request("127.0.0.1")), "127.0.0.1")


class TrustedProxiesTests(unittest.TestCase):
    def test_loopback_is_always_trusted(self):
        self.assertEqual(trusted_proxies_from({}), {"127.0.0.1", "::1"})

    def test_configured_proxies_are_added(self):
        cfg = {"dashboard_auth": {"trusted_proxies": ["10.10.0.3", " ", None]}}
        self.assertEqual(trusted_proxies_from(cfg), {"127.0.0.1", "::1", "10.10.0.3"})

    def test_a_single_string_is_accepted(self):
        self.assertIn("10.10.0.3", trusted_proxies_from({"dashboard_auth": {"trusted_proxies": "10.10.0.3"}}))

    def test_configured_proxy_forwards_the_client_ip(self):
        request = Request("10.10.0.3", "198.51.100.20")
        self.assertEqual(get_client_ip(request, trusted_proxies_from(
            {"dashboard_auth": {"trusted_proxies": ["10.10.0.3"]}})), "198.51.100.20")
        self.assertEqual(get_client_ip(request), "10.10.0.3")


class ForwardedProtoTests(unittest.TestCase):
    def scheme(self, remote, proto, trusted=frozenset({"10.10.0.3"})):
        seen = {}

        def inner(environ, start_response):
            seen["scheme"] = environ["wsgi.url_scheme"]
            return []

        environ = {"REMOTE_ADDR": remote, "wsgi.url_scheme": "http"}
        if proto is not None:
            environ["HTTP_X_FORWARDED_PROTO"] = proto
        ForwardedProto(inner, lambda: trusted)(environ, lambda *a: None)
        return seen["scheme"]

    def test_trusted_proxy_sets_https(self):
        self.assertEqual(self.scheme("10.10.0.3", "https"), "https")

    def test_untrusted_client_cannot_claim_https(self):
        self.assertEqual(self.scheme("198.51.100.20", "https"), "http")

    def test_rightmost_value_wins_and_junk_is_ignored(self):
        self.assertEqual(self.scheme("10.10.0.3", "javascript, https"), "https")
        self.assertEqual(self.scheme("10.10.0.3", "ftp"), "http")
        self.assertEqual(self.scheme("10.10.0.3", None), "http")


if __name__ == "__main__":
    unittest.main()

import unittest

from auth import get_client_ip


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


if __name__ == "__main__":
    unittest.main()

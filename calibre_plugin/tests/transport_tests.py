"""Optional token transport behavior tests."""
import subprocess
import unittest
from unittest.mock import patch

from .. import transport


class CurlTransportByteSafetyTests(unittest.TestCase):
    def test_curl_stdin_receives_the_exact_binary_body(self):
        completed = type("Completed", (), {
            "returncode": 0, "stdout": b"ok\n200", "stderr": b""})()
        with patch.object(transport, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(transport.subprocess, "run", return_value=completed) as run:
            status, body = transport._http_post_via_curl(
                "https://example.test/token", b"\x00\xff\nbody", {}, 3)
        self.assertEqual((200, b"ok"), (status, body))
        self.assertEqual(b"\x00\xff\nbody", run.call_args.kwargs["input"])
        self.assertIn("@-", run.call_args.args[0])

    def test_curl_process_error_preserves_status_without_response_replay(self):
        failure = subprocess.CalledProcessError(
            28, ["curl"], output=b"", stderr=b"timed out")
        with patch.object(transport, "_curl_binary", return_value="/usr/bin/curl"), \
                patch.object(transport.subprocess, "run", side_effect=failure):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                transport._http_post_via_curl(
                    "https://example.test/token", b"grant", {}, 3)
        self.assertEqual(28, raised.exception.returncode)

    def test_curl_cffi_receives_arbitrary_bytes_without_utf8_replacement(self):
        captured = {}

        class Response:
            status_code = 200
            content = b"ok"

        class Session:
            def post(self, _url, **kwargs):
                captured.update(kwargs)
                return Response()

        status, body = transport._http_post_via_curl_cffi(
            Session(), "https://example.test/token", b"\x00\xff", {}, 3)
        self.assertEqual((200, b"ok"), (status, body))
        self.assertEqual(b"\x00\xff", captured["data"])


if __name__ == "__main__":
    unittest.main()

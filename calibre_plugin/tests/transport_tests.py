"""Tests for the dependency-free WebSocket framing transport."""
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch
from urllib.error import URLError

from ..cdp_transport import _Ws, _http_get_json


class DevToolsHttpTests(unittest.TestCase):
    def test_expected_failures_are_unavailable_but_program_errors_propagate(self):
        for error in (URLError("offline"), OSError("timeout")):
            with self.subTest(error=error), patch(
                    "urllib.request.build_opener", side_effect=error):
                self.assertIsNone(_http_get_json("http://127.0.0.1:1"))
        response = unittest.mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"not json"
        with patch("urllib.request.build_opener") as opener:
            opener.return_value.open.return_value = response
            self.assertIsNone(_http_get_json("http://127.0.0.1:1"))
        with patch("urllib.request.build_opener", side_effect=RuntimeError("bug")):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                _http_get_json("http://127.0.0.1:1")


class WebSocketTransportTests(unittest.TestCase):
    def test_recv_json_reassembles_a_two_frame_text_message(self):
        left, right = socket.socketpair()
        left.setblocking(True)
        right.setblocking(True)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        message = json.dumps({"method": "Runtime.evaluate", "id": 3}).encode()
        split = len(message) // 2
        ready = threading.Event()
        first = message[:split]
        second = message[split:]

        def frame(fin, payload):
            length = len(payload)
            if length < 126:
                header = bytes([fin, length])
            elif length < 65536:
                header = bytes([fin, 126]) + struct.pack(">H", length)
            else:
                header = bytes([fin, 127]) + struct.pack(">Q", length)
            return header + payload

        def server():
            ready.wait(3)
            # A completed binary message is ignored before the text message.
            right.sendall(frame(0x02, b"binary"))
            right.sendall(frame(0x80, b" message"))
            right.sendall(frame(0x01, first))
            right.sendall(frame(0x80, second))

        thread = threading.Thread(target=server)
        thread.start()
        ready.set()
        try:
            self.assertEqual({"method": "Runtime.evaluate", "id": 3},
                             _Ws(left).recv_json(timeout=3))
        finally:
            thread.join(3)

    @staticmethod
    def _read_frame(sock):
        """Read one frame and return (opcode, fin, masked, payload)."""
        def read_exact(count):
            data = b""
            while len(data) < count:
                chunk = sock.recv(count - len(data))
                if not chunk:
                    raise AssertionError("socket closed before frame was read")
                data += chunk
            return data

        header = read_exact(2)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", read_exact(8))[0]
        masked = bool(header[1] & 0x80)
        mask = read_exact(4) if masked else b""
        payload = read_exact(length) if length else b""
        if mask:
            payload = bytes(b ^ mask[i % 4]
                            for i, b in enumerate(payload))
        return header[0] & 0x0F, bool(header[0] & 0x80), masked, payload

    def test_ping_gets_a_masked_pong_and_unsolicited_pong_is_ignored(self):
        left, right = socket.socketpair()
        left.setblocking(True)
        right.setblocking(True)
        right.settimeout(3)
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        ready = threading.Event()
        seen = []

        def frame(opcode, payload):
            return bytes([0x80 | opcode, len(payload)]) + payload

        def server():
            ready.wait(3)
            right.sendall(frame(0xA, b"unsolicited-pong"))
            right.sendall(frame(0x9, b"ping-data"))
            right.sendall(frame(0x1, b'{"id": 5}'))
            seen.append(self._read_frame(right))

        thread = threading.Thread(target=server)
        thread.start()
        ready.set()
        try:
            self.assertEqual({"id": 5}, _Ws(left).recv_json(timeout=3))
        finally:
            thread.join(3)
        self.assertEqual(1, len(seen))
        self.assertEqual(0xA, seen[0][0])
        self.assertTrue(seen[0][2])
        self.assertEqual(b"ping-data", seen[0][3])


if __name__ == "__main__":
    unittest.main()

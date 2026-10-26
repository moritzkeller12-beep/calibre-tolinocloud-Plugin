"""Tests for the dependency-free WebSocket framing transport."""
import json
import socket
import struct
import threading
import unittest

from ..cdp_transport import _Ws


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


if __name__ == "__main__":
    unittest.main()

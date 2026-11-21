"""Tests for the dependency-free WebSocket framing transport."""
import base64
import hashlib
import io
import json
import socket
import struct
import threading
import unittest
from unittest.mock import patch

from ..cdp_transport import _Ws, _http_get_json
from ._support import LocalHTTP


class DevToolsHttpTests(unittest.TestCase):
    def test_a_refused_connection_is_unavailable(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        self.assertIsNone(
            _http_get_json("http://127.0.0.1:%d" % port, timeout=2))

    def test_invalid_json_is_unavailable(self):
        with LocalHTTP(lambda *_args: (200, {}, b"not json")) as server:
            self.assertIsNone(
                _http_get_json(server.url("/json/version"), timeout=2))

    def test_program_errors_propagate(self):
        with patch("urllib.request.build_opener",
                   side_effect=RuntimeError("bug")):
            with self.assertRaisesRegex(RuntimeError, "bug"):
                _http_get_json("http://127.0.0.1:1")


GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def server_reply(request, accept=None, upgrade=b"websocket",
                 connection=b"Upgrade",
                 status=b"HTTP/1.1 101 Switching Protocols"):
    """Antwort wie ein echter Server: Accept unabhaengig berechnet."""
    key = b""
    for line in request.split(b"\r\n"):
        if line.lower().startswith(b"sec-websocket-key:"):
            key = line.split(b":", 1)[1].strip()
    digest = base64.b64encode(hashlib.sha1(key + GUID).digest())
    return (b"%s\r\nUpgrade: %s\r\nConnection: %s\r\n"
            b"Sec-WebSocket-Accept: %s\r\n\r\n"
            % (status, upgrade, connection,
               digest if accept is None else accept))


class FakeSocket:
    """Socket-Doppel fuer den Upgrade-Handshake (kein echter Server)."""

    def __init__(self, reply_factory):
        self.reply = None
        self.reply_factory = reply_factory
        self.sent = b""
        self.closed = False

    def sendall(self, data):
        self.sent += data

    def settimeout(self, value):
        self.timeout = value

    def recv(self, size):
        if self.reply is None:
            self.reply = self.reply_factory(self.sent)
        chunk, self.reply = self.reply[:size], self.reply[size:]
        return chunk

    def close(self):
        self.closed = True


class HandshakeTests(unittest.TestCase):
    """Nur eine vollstaendige 101-Antwort gilt als offene Verbindung."""

    def handshake(self, **options):
        self.sock = FakeSocket(
            reply_factory=lambda request: server_reply(request, **options))
        with patch("calibre_plugin.cdp_transport.socket.create_connection",
                   return_value=self.sock):
            return _Ws.connect("127.0.0.1", 9222, "/devtools/page/x",
                               timeout=2)

    def test_a_complete_handshake_opens_the_connection(self):
        ws = self.handshake()
        self.assertIsInstance(ws, _Ws)
        self.assertIn(b"GET /devtools/page/x HTTP/1.1", self.sock.sent)
        self.assertIn(b"Sec-WebSocket-Key: ", self.sock.sent)
        self.assertIn(b"Sec-WebSocket-Version: 13", self.sock.sent)
        self.assertFalse(self.sock.closed)

    def test_a_connection_header_with_several_tokens_is_accepted(self):
        # `Connection` ist eine Kommaliste; `Upgrade` ist ein eigener Eintrag.
        ws = self.handshake(connection=b"keep-alive, Upgrade")
        self.assertIsInstance(ws, _Ws)

    def test_a_connection_token_must_match_exactly(self):
        # Die Zeichenfolge irgendwo im Header genuegt nicht.
        with self.assertRaises(OSError):
            self.handshake(connection=b"notupgrade")

    def test_an_incomplete_handshake_is_rejected(self):
        cases = {
            "falscher Accept": {"accept": b"falscher-digest"},
            "kein Upgrade": {"upgrade": b"h2c"},
            "kein Connection-Upgrade": {"connection": b"close"},
            "kein 101": {"status": b"HTTP/1.1 200 OK"},
        }
        for label, options in cases.items():
            with self.subTest(label):
                with self.assertRaises(OSError) as caught:
                    self.handshake(**options)
                self.assertIn("handshake rejected", str(caught.exception))
                self.assertTrue(self.sock.closed)


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

    def test_a_completed_message_does_not_count_towards_the_next_one(self):
        """Der Fragmentpuffer wird nach jeder Nachricht geleert."""
        size = 16 * 1024 * 1024
        # Eine fragmentierte Binaernachricht wird uebersprungen. Die danach
        # gelesene JSON-Nachricht nutzt das volle erlaubte Limit fuer sich.
        frames = (b"\x02\x01a\x80\x01b\x81\x7f" + struct.pack(">Q", size)
                  + b'{"id": 2}'.ljust(size))
        sock = FakeSocket(None)
        sock.recv = io.BytesIO(frames).read
        self.assertEqual({"id": 2}, _Ws(sock).recv_json(timeout=3))

    def test_an_oversized_frame_is_rejected_before_reading_the_payload(self):
        """Die Laengenangabe darf keinen beliebig grossen Puffer anfordern."""
        left, right = socket.socketpair()
        self.addCleanup(left.close)
        self.addCleanup(right.close)
        right.sendall(bytes([0x81, 127])
                      + struct.pack(">Q", 16 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(OSError, "too large"):
            _Ws(left).recv_json(timeout=3)


if __name__ == "__main__":
    unittest.main()

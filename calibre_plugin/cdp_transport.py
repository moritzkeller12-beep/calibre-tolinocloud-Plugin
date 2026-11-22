"""Dependency-free websocket and DevTools HTTP transport helpers."""
import base64
from http.client import HTTPException
import hashlib
import json
import secrets
import socket
import struct
import time


# Eine DevTools-Nachricht ist klein; ein Riesenframe ist ein Fehler, kein
# Grund, beliebig viel Speicher zu belegen.
_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

# RFC 6455: der Server bestaetigt den Handshake mit diesem Digest.
_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def _accept_value(key):
    """Der erwartete ``Sec-WebSocket-Accept``-Wert zu einem Client-Key."""
    return base64.b64encode(
        hashlib.sha1(key.encode("ascii") + _WS_GUID).digest()).decode("ascii")


# ---------------------------------------------------------------- WebSocket

class _Ws:
    """Minimal RFC6455 client over a plain socket (no dependencies)."""

    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

    @staticmethod
    def connect(host, port, path, timeout=10):
        sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        request = (
            "GET %s HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: %s\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key)
        )
        try:
            sock.sendall(request.encode("ascii"))
            sock.settimeout(timeout)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        except OSError:
            # Auch ein Timeout beim Handshake ist ein OSError. Ohne das
            # Schliessen blieb der Socket bis zum Garbage Collector offen.
            sock.close()
            raise
        head, _, rest = data.partition(b"\r\n\r\n")
        lines = [line.strip() for line in head.split(b"\r\n") if line.strip()]
        status_line = lines[0] if lines else b""
        headers = {}
        for line in lines[1:]:
            name, separator, value = line.partition(b":")
            if separator:
                headers[name.strip().lower()] = value.strip()
        # Nur eine echte 101-Antwort mit den Upgrade-Headern und dem
        # richtigen Accept-Digest ist ein WebSocket. Ohne diese Pruefung
        # wuerde auch eine fremde HTTP-Antwort als Verbindung gelten.
        # `Connection` ist eine Kommaliste: nur ein eigener Eintrag zaehlt,
        # nicht die Zeichenfolge irgendwo im Header.
        connection_tokens = {
            token.strip().lower()
            for token in headers.get(b"connection", b"").split(b",")}
        if (b" 101 " not in status_line
                or headers.get(b"upgrade", b"").lower() != b"websocket"
                or b"upgrade" not in connection_tokens
                or headers.get(b"sec-websocket-accept", b"").decode(
                    "latin-1") != _accept_value(key)):
            sock.close()
            raise OSError("websocket handshake rejected: %s"
                          % status_line[:120].decode("ascii", "replace"))
        ws = _Ws(sock)
        ws.buf = rest
        return ws

    def _send_frame(self, opcode, payload):
        """Send one masked, final frame."""
        header = bytearray([0x80 | opcode])
        mask = secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def send_json(self, obj):
        self._send_frame(0x1, json.dumps(obj).encode("utf-8"))

    def _read_exact(self, n, deadline):
        while len(self.buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise OSError("websocket read timeout")
            self.sock.settimeout(min(remaining, 5))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                # Ein Timeout des 5-Sekunden-Slices ist nicht die Deadline.
                continue
            if not chunk:
                raise OSError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _read_frame(self, deadline, pending=0):
        """Einen Frame lesen: ``(final, opcode, payload)``.

        RFC 6455: FIN/Opcode und MASK/Länge belegen je ein Byte; ``pending``
        ist die bereits gesammelte Fragmentgröße, damit die Obergrenze vor
        dem Lesen der Nutzlast greift.
        """
        header = self._read_exact(2, deadline)
        final = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        length = header[1] & 0x7F
        masked = header[1] & 0x80
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8, deadline))[0]
        if length > _MAX_MESSAGE_BYTES or pending + length > _MAX_MESSAGE_BYTES:
            raise OSError("websocket message too large")
        mask = self._read_exact(4, deadline) if masked else b""
        payload = self._read_exact(length, deadline)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return final, opcode, payload

    def recv_json(self, timeout=10):
        deadline = time.time() + max(1, timeout)
        fragmented = bytearray()
        fragmented_opcode = None
        while True:
            final, opcode, payload = self._read_frame(deadline, len(fragmented))
            if opcode == 0x8:  # close
                raise OSError("websocket closed by peer")
            if opcode in (0x9, 0xA):  # answer ping; ignore pong
                if opcode == 0x9:
                    self._send_frame(0xA, payload)
                continue
            if opcode in (0x1, 0x2):
                if not final:
                    fragmented = bytearray(payload)
                    fragmented_opcode = opcode
                    continue
                message = payload
            elif opcode == 0x0 and fragmented_opcode is not None:
                fragmented.extend(payload)
                if not final:
                    continue
                message = bytes(fragmented)
                opcode = fragmented_opcode
            else:
                continue
            # Die Nachricht ist vollstaendig: ihre Bytes duerfen nicht ins
            # Groessenlimit der naechsten Nachricht einfliessen.
            fragmented = bytearray()
            fragmented_opcode = None
            if opcode == 0x1:
                try:
                    return json.loads(message.decode("utf-8"))
                except ValueError:
                    continue

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _http_get_json(url, timeout=5):
    """Fetch a local DevTools JSON endpoint without using environment proxies."""
    try:
        from urllib.request import ProxyHandler, build_opener
        opener = build_opener(ProxyHandler({}))
        with opener.open(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (OSError, HTTPException, ValueError):
        return None



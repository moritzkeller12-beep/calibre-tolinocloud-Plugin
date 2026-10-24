"""Dependency-free websocket and DevTools HTTP transport helpers."""
import base64
import json
import secrets
import socket
import struct
import time
from urllib.parse import urlsplit


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
        sock.sendall(request.encode("ascii"))
        sock.settimeout(timeout)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            sock.close()
            raise OSError("websocket handshake rejected: %s"
                          % head[:120].decode("ascii", "replace"))
        ws = _Ws(sock)
        ws.buf = rest
        return ws

    def send_json(self, obj):
        payload = json.dumps(obj).encode("utf-8")
        header = bytearray([0x81])  # FIN + text frame
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

    def _read_exact(self, n, deadline):
        while len(self.buf) < n:
            remaining = deadline - time.time()
            if remaining <= 0:
                raise OSError("websocket read timeout")
            self.sock.settimeout(min(remaining, 5))
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:
                # Zeitfenster ohne Daten: DIE Deadline entscheidet,
                # nicht das 5-Sekunden-Slice. Frueher wanderte dieser
                # timeout als OSError nach oben und tötete den Lauscher
                # bei jeder laengeren Stillphase des Readers.
                continue
            if not chunk:
                raise OSError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self, timeout=10):
        deadline = time.time() + max(1, timeout)
        while True:
            # RFC-6455-Framekopf: byte 0 = FIN/opcode, byte 1 = MASK bit + Laenge.
            # Byte 1 auszulassen kappte jede CDP-Antwort an falscher Stelle.
            header = self._read_exact(2, deadline)
            opcode = header[0] & 0x0F
            length = header[1]
            masked = length & 0x80
            length &= 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2, deadline))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8, deadline))[0]
            mask = self._read_exact(4, deadline) if masked else b""
            payload = self._read_exact(length, deadline)
            if mask:
                payload = bytes(b ^ mask[i % 4]
                                for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                raise OSError("websocket closed by peer")
            if opcode in (0x9, 0xA):  # ping/pong: ignore
                continue
            if opcode in (0x1, 0x2):
                try:
                    return json.loads(payload.decode("utf-8"))
                except ValueError:
                    continue

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _is_loopback_url(url):
    """True fuer 127.0.0.1/localhost/::1 -- der Browser-Debug-Port."""
    try:
        host = urlsplit(str(url or "")).hostname or ""
    except (TypeError, ValueError):
        return False
    return host in ("127.0.0.1", "localhost", "::1")


def _http_get_json(url, timeout=5):
    """Tiny JSON GET used for /json/list (curl_cffi when available).

    Fuer Loopback kommt nur ``urllib`` zum Einsatz: Der Debug-Port laeuft auf
    demselben Rechner, eine Browser-Impersonation bringt dort nichts und
    kostet nur Zeit. Umgebungs-Proxies werden fuer Loopback ausdruecklich
    umgangen, damit kein Proxy den lokalen Aufruf abfaengt.
    """
    loopback = _is_loopback_url(url)
    if not loopback:
        try:
            from curl_cffi import requests as cf_requests  # type: ignore
            response = cf_requests.get(url, timeout=timeout,
                                       impersonate="chrome")
            return response.json()
        except Exception:
            pass
    try:
        from urllib.request import ProxyHandler, build_opener, urlopen
        open_url = (build_opener(ProxyHandler({})).open if loopback
                    else urlopen)
        with open_url(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _ws_path_to_host_port(path):
    host, _, rest = path.partition(":")
    port, _, ws_path = rest.partition("/")
    return host, int(port), "/" + ws_path



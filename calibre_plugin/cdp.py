"""Live-Token-Grab über das Chrome DevTools Protocol (CDP).

Warum dieses Modul existiert: Der Web Reader rotiert seinen Refresh-Token
bei jedem Hintergrund-Refresh, und Keycloak akzeptiert innerhalb einer
Sitzung nur den NEUESTEN Token. Historische Kopien im Browser-Storage auf
der Festplatte sind damit zwangsläufig verbraucht -- ihr Replay gegen den
Token-Endpunkt beantwortet Keycloak mit "invalid_grant" und kann dessen
Wiederverwendungsschutz auslösen, der die GESAMTE Sitzung widerruft (das
war der Grund für die "ich werde sofort wieder abgemeldet"-Loop).

Der Ausweg: ein eigenes Chromium-Fenster (privates Profil), in dem der
Benutzer den Web Reader normal bedient. Sobald die Bücherliste geladen
ist, lesen wir den AKTUELLEN Token direkt aus dem Seiten-Speicher der
laufenden Seite (Runtime.evaluate über CDP) -- einmalig, unmittelbar vor
dem Tausch am Token-Endpunkt. Kein Replay, keine Konkurrenz um den Token.

Verwendet wird eine minimale WebSocket-Implementierung auf sockety
(RFC 6455 Basiscclient ohne Abhängigkeiten); curl_cffi wird nur als
optimaler HTTP-Fallback für den WS-Handshake genutzt.
"""

import base64
import hashlib
import json
import os
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time

try:
    from .tolino import PARTNERS, TolinoAuthError
except ImportError:  # direkter Import (außerhalb des Plugin-Pakets)
    from tolino import PARTNERS, TolinoAuthError


# ---------------------------------------------------------------- Vorbedingungen

def chromium_candidates(home=None):
    """Installed Chromium-family browsers, most common first.

    Returns ``(argv_prefix, label)`` pairs: native installs launch via
    their binary path, Flatpak installs via ``flatpak run`` (launching
    the sandboxed binary directly fails outside its runtime), Snap via
    ``snap run``. On Linux the Flatpak detection matters in practice:
    the user's storage paths showed Chromium/Brave under ~/.var/app,
    which only the Flatpak launcher can start. Windows/macOS path-based
    detection is included so the module works wherever Calibre runs.
    """
    home = home or os.path.expanduser("~")
    flat = os.path.join(home, ".var", "app")
    snap = os.path.join(home, "snap")
    execs = [
        ("google-chrome", "Google Chrome"),
        ("google-chrome-stable", "Google Chrome"),
        ("chromium", "Chromium"),
        ("chromium-browser", "Chromium"),
        ("brave-browser", "Brave"),
        ("microsoft-edge", "Edge"),
        ("microsoft-edge-stable", "Edge"),
        ("vivaldi", "Vivaldi"),
        ("vivaldi-stable", "Vivaldi"),
        ("opera", "Opera"),
    ]
    found = []
    for exe, label in execs:
        path = shutil.which(exe)
        if path:
            found.append(([path], label))
    # Flatpak: only offer an app whose installation directory actually
    # exists AND whose launcher binary is available (sandboxed binaries
    # cannot be executed directly -- they need the flatpak runtime).
    flatpak_apps = [
        ("org.chromium.Chromium", "chrome", "Chromium (Flatpak)"),
        ("com.brave.Browser", "brave", "Brave (Flatpak)"),
        ("com.google.Chrome", "chrome", "Chrome (Flatpak)"),
        ("com.vivaldi.Vivaldi", "vivaldi", "Vivaldi (Flatpak)"),
    ]
    flatpak_bin = shutil.which("flatpak")
    if flatpak_bin and os.path.isdir(flat):
        for app_id, command, label in flatpak_apps:
            if os.path.isdir(os.path.join(flat, app_id)):
                found.append(([flatpak_bin, "run", "--command=%s" % command,
                               app_id], label))
    # Snap: the snap shim usually sits on PATH already (covered above);
    # keep the snap run fallback for stripped-down environments.
    if shutil.which("snap") and os.path.isdir(os.path.join(snap, "chromium")):
        found.append(([shutil.which("snap"), "run", "chromium"],
                      "Chromium (Snap)"))
    return found


def _windows_chrome_paths():
    home = os.environ.get("LOCALAPPDATA") or \
        os.path.join(os.path.expanduser("~"), "AppData", "Local")
    program_dirs = [
        os.environ.get("PROGRAMFILES") or "C:\\Program Files",
        os.environ.get("PROGRAMFILES(X86)") or "C:\\Program Files (x86)",
    ]
    rel_paths = [
        ("Google\\Chrome\\Application\\chrome.exe", "Google Chrome"),
        ("Chromium\\Application\\chrome.exe", "Chromium"),
        ("BraveSoftware\\Brave-Browser\\Application\\brave.exe", "Brave"),
        ("Microsoft\\Edge\\Application\\msedge.exe", "Edge"),
    ]
    found = []
    for base in program_dirs:
        for rel, label in rel_paths:
            path = os.path.join(base, rel)
            if os.path.isfile(path) and not any(label == f[1] for f in found):
                found.append(([path], label))
    return found


def _macos_chrome_paths():
    roots = ["/Applications", os.path.join(os.path.expanduser("~"),
                                           "Applications")]
    bundles = [
        ("Google Chrome.app/Contents/MacOS/Google Chrome", "Google Chrome"),
        ("Chromium.app/Contents/MacOS/Chromium", "Chromium"),
        ("Brave Browser.app/Contents/MacOS/Brave Browser", "Brave"),
        ("Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Edge"),
    ]
    found = []
    for base in roots:
        for rel, label in bundles:
            path = os.path.join(base, rel)
            if os.path.isfile(path) and not any(label == f[1] for f in found):
                found.append(([path], label))
    return found


def pick_chromium():
    """Best available Chromium-family browser, or None.

    Returns ``(argv_prefix, label)``; the argv prefix is either a binary
    path or a flatpak/snap launcher followed by nothing yet -- the
    Chromium flags are appended by the caller.
    """
    found = chromium_candidates()
    if sys.platform == "win32":
        found = found + _windows_chrome_paths()
    elif sys.platform == "darwin":
        found = found + _macos_chrome_paths()
    return found[0] if found else None


def _start_port():
    """Fixed debug port: a reused live grabber window must be findable."""
    return 9223


def _profile_dir():
    """Private profile dir for the grabber window (fresh on each start)."""
    return os.path.join(tempfile.gettempdir(), "tolino_grab_profile")


def _clean_profile_dir():
    shutil.rmtree(_profile_dir(), ignore_errors=True)


def _reader_url(partner_id):
    partner = PARTNERS.get(int(partner_id)) or {}
    return (partner.get("reader_url")
            or "https://webreader.mytolino.com/library/index.html")


def devtools_port_alive(port=None):
    """True when a grabber window's DevTools endpoint answers right now.

    Used by callers that want to reuse the running "Im Browser anmelden"
    window before falling back to disk scraping: a live window always
    holds the reader's CURRENT token, while disk copies are historical.
    """
    return bool(_http_get_json(
        "http://127.0.0.1:%d/json/version" % (port or _start_port()), 2))


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
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv_json(self, timeout=10):
        deadline = time.time() + max(1, timeout)
        while True:
            # RFC 6455 frame header: byte 0 = FIN/opcode, byte 1 = MASK bit
            # + 7-bit length. The first version dropped byte 1 and read a
            # third byte as the length -- every CDP response was then cut
            # at the wrong offsets and Runtime.evaluate never returned a
            # usable value (the "plugin finds nothing" bug report).
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


# ---------------------------------------------------------------- CDP-Griff

def _http_get_json(url, timeout=5):
    """Tiny JSON GET used for /json/list (curl_cffi when available)."""
    try:
        from curl_cffi import requests as cf_requests  # type: ignore
        response = cf_requests.get(url, timeout=timeout, impersonate="chrome")
        return response.json()
    except Exception:
        pass
    try:
        from urllib.request import urlopen
        with urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _find_page_ws_url(port, needle, timeout=30):
    """Wait for a DevTools page target whose URL contains `needle`."""
    deadline = time.time() + timeout
    path_part = None
    while time.time() < deadline:
        targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
        for target in targets:
            if target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            if needle in url:
                path_part = str(target.get("webSocketDebuggerUrl") or "")
                if path_part.startswith("ws://"):
                    path_part = path_part[len("ws://"):]
                    return path_part
        time.sleep(0.5)
    return None


def _ws_path_to_host_port(path):
    host, _, rest = path.partition(":")
    port, _, ws_path = rest.partition("/")
    return host, int(port), "/" + ws_path


# Das Snippet läuft IN der Web-Reader-Seite (Runtime.evaluate) und holt
# genau die Werte, die auch der Disk-Scrape sucht -- nur eben live:
#   - Keycloak oidc.user:<issuer>:<client> Blob (refresh_token + sid/exp)
#   - klassische localStorage/IndexedDB-ähnliche Keys (refresh_token, t_auth)
_GRAB_SNIPPET = r"""
(function () {
  var out = {refresh: [], hardware: []};
  function push(val, bucket) {
    if (typeof val === 'string' && val.length > 20 && out[bucket].indexOf(val) < 0) {
      out[bucket].push(val);
    }
  }
  function fromObj(obj) {
    if (!obj || typeof obj !== 'object') return;
    var r = obj.refresh_token || obj.refreshToken || obj['refresh-token']
      || obj.t_auth_token || (obj.credential && obj.credential.refresh_token);
    if (typeof r === 'string') push(r, 'refresh');
    var h = obj.hardware_id || obj.hardwareId || obj.device_id
      || obj.deviceId || obj.hardwareId;
    if (typeof h === 'string') push(h, 'hardware');
  }
  try {
    for (var i = 0; i < localStorage.length; i++) {
      var k = localStorage.key(i);
      var v = localStorage.getItem(k);
      if (!v) continue;
      fromObj(tryParse(v));
      if (/^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$/.test(v)) {
        push(v, 'refresh');
      }
      if (/^[0-9a-fA-F-]{8,}$/.test(v) && /hardware|device/i.test(k)) {
        push(v, 'hardware');
      }
    }
  } catch (e) {}
  try {
    for (var i = 0; i < sessionStorage.length; i++) {
      var k = sessionStorage.key(i);
      fromObj(tryParse(sessionStorage.getItem(k)));
    }
  } catch (e) {}
  try {
    if (window.indexedDB && indexedDB.databases) {
      indexedDB.databases().then(function (dbs) {
        // IndexedDB-Inhalt wird vom Grabber nicht synchron gelesen; der
        // oidc.user-Blob im localStorage enthält bei Keycloak-Partnern
        // den aktuellen Token bereits vollständig.
      });
    }
  } catch (e) {}
  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }
  return JSON.stringify(out);
})()
"""


def grab_from_existing_reader(port=9222, timeout=15):
    """Read the CURRENT tokens from an already-running Web Reader tab.

    Connects to an existing browser's DevTools endpoint (only useful when
    that browser was started with --remote-debugging-port, e.g. the user
    followed the manual fallback). Returns
    ``{"refresh": [...], "hardware": [...]}`` ordered freshest-first.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
        for target in targets:
            if target.get("type") != "page":
                continue
            if "mytolino.com" not in str(target.get("url") or ""):
                continue
            ws_url = str(target.get("webSocketDebuggerUrl") or "")
            if not ws_url.startswith("ws://"):
                continue
            result = _evaluate_in_target(ws_url, _GRAB_SNIPPET, timeout)
            if result:
                return result
        time.sleep(0.5)
    return {"refresh": [], "hardware": []}


def _evaluate_in_target(ws_url, expression, timeout=10):
    """Run a JS expression in a DevTools page target, return its JSON value."""
    try:
        path = ws_url[len("ws://"):]
        host, port, ws_path = _ws_path_to_host_port(path)
        ws = _Ws.connect(host, port, ws_path, timeout=timeout)
    except (OSError, ValueError):
        return None
    try:
        ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True,
        }})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = ws.recv_json(timeout=max(1, deadline - time.time()))
            if message.get("id") != 1:
                continue
            result = (message.get("result") or {}).get("result") or {}
            value = result.get("value")
            if not value:
                return None
            try:
                parsed = json.loads(value)
            except ValueError:
                return None
            if isinstance(parsed, dict):
                return {
                    "refresh": [t for t in parsed.get("refresh", [])
                                if isinstance(t, str)],
                    "hardware": [t for t in parsed.get("hardware", [])
                                 if isinstance(t, str)],
                }
            return None
        return None
    except OSError:
        return None
    finally:
        ws.close()


# ---------------------------------------------------------------- Fenster-Flow

def launch_reader_window(partner_id):
    """Open a dedicated grabber Chromium window on the Web Reader page.

    Returns (binary, port) or raises TolinoAuthError when no Chromium
    browser is installed. A leftover grabber window from an earlier
    attempt is reused when its DevTools endpoint still answers (the user
    may have kept the sign-in window open across retries).
    """
    binary, label = pick_chromium() or (None, None)
    if not binary:
        raise TolinoAuthError(
            "Kein Chromium-Browser gefunden (Chrome/Chromium/Brave/Edge). "
            "Die Browser-Anmeldung benötigt eines davon für die Live-"
            "Token-Übernahme. Alternativ: aktuellen Refresh-Token manuell "
            "aus einem laufenden Web Reader übernehmen (siehe README)."
        )
    port = _start_port()
    # Reuse path: is a grabber window from an earlier attempt still up?
    if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
        return binary, port
    _clean_profile_dir()
    args = list(binary) + [
        "--user-data-dir=%s" % _profile_dir(),
        "--remote-debugging-port=%d" % port,
        "--no-first-run", "--no-default-browser-check",
        "--disable-session-crashed-bubble",
        "--disable-infobars",
        "--window-size=1280,900",
        "--new-window",
        _reader_url(partner_id),
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008  # DETACHED_PROCESS: Calibre bleibt bedienbar
    try:
        subprocess.Popen(args, creationflags=creationflags,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as exc:
        raise TolinoAuthError(
            "Chromium konnte nicht gestartet werden (%s): %s" % (label, exc))
    # Wait until the DevTools endpoint answers.
    deadline = time.time() + 20
    while time.time() < deadline:
        if _http_get_json("http://127.0.0.1:%d/json/version" % port, 2):
            return binary, port
        time.sleep(0.5)
    raise TolinoAuthError(
        "Chromium startete, aber der DevTools-Port %d blieb stumm. Läuft "
        "evtl. schon eine Instanz ohne Debugging-Port? Schließe alle "
        "Chromium-Fenster und versuche es erneut." % port)


def collect_grab(partner_id, port, timeout=180, progress=None):
    """Poll the grabber window until the reader exposes the current token.

    The in-page grab only returns the reader's CURRENT refresh token, so
    there is no replay risk; we simply wait (up to `timeout` seconds) for
    the user to complete the Web Reader sign-in. The moment a token
    appears it is returned together with any hardware candidate.
    """
    deadline = time.time() + timeout
    target_seen = False
    while time.time() < deadline:
        targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
        reader = None
        for target in targets:
            if target.get("type") != "page":
                continue
            url = str(target.get("url") or "")
            if "mytolino.com" in url:
                reader = target
                break
        if reader is None:
            if target_seen and progress:
                progress("Fenster geschlossen -- Abbruch.")
            if target_seen:
                raise TolinoAuthError(
                    "Das Anmeldefenster wurde geschlossen, bevor ein Token "
                    "gelesen werden konnte. Bitte erneut versuchen und das "
                    "Fenster offen lassen, bis die Übernahme durch ist.")
            time.sleep(1)
            continue
        target_seen = True
        ws_url = str(reader.get("webSocketDebuggerUrl") or "")
        if ws_url.startswith("ws://"):
            result = _evaluate_in_target(ws_url, _GRAB_SNIPPET, 10)
            if result and result.get("refresh"):
                return result
            if progress:
                progress("Warte auf Web-Reader-Anmeldung "
                         "(Bücherliste laden) ...")
        time.sleep(2)
    raise TolinoAuthError(
        "Timeout: Im Anmeldefenster wurde keine Web-Reader-Anmeldung "
        "erkannt. Bitte im Fenster anmelden, bis die Bücherliste sichtbar "
        "ist, und es erneut versuchen.")


def grab_live_tokens(partner_id, hardware, timeout=300, progress=None):
    """Open the grabber window and read the reader's CURRENT tokens.

    Returns the grabbed dict ``{"refresh": [...], "hardware": [...]}``;
    the token-endpoint exchange stays in tolino.py (single source of
    truth for the client swap, testable without a browser).
    """
    _binary, port = launch_reader_window(partner_id)
    if progress:
        progress("Anmeldefenster geöffnet -- im Web Reader anmelden "
                 "(Bücherliste abwarten).")
    grabbed = collect_grab(partner_id, port, timeout=timeout, progress=progress)
    if progress:
        progress("Aktueller Token aus dem Web Reader gelesen -- werde "
                 "ihn jetzt am Token-Endpunkt tauschen ...")
    return grabbed

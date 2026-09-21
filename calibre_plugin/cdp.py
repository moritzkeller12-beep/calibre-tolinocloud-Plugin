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
  var out = {refresh: [], hardware: [], idb: []};
  function push(val, bucket) {
    if (typeof val === 'string' && val.length > 20 &&
        out[bucket].indexOf(val) < 0) {
      out[bucket].push(val);
    }
  }
  function tryParse(text) {
    try { return JSON.parse(text); } catch (e) { return null; }
  }
  function walk(value, depth, seen) {
    if (!value || depth > 6) return;
    if (typeof value === 'string') {
      // JWT-Form: drei base64url-Teile. Der Refresh-Token ist ein JWT
      // mit typ "Refresh"; Access-Tokens sind kurzlebiger und werden
      // vom Grabber spaeter depriorisiert, nicht ausgefiltert.
      if (/^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$/.test(value)) {
        push(value, 'refresh');
      }
      return;
    }
    if (typeof value !== 'object') return;
    if (seen.indexOf(value) >= 0) return;
    seen.push(value);
    for (var key in value) {
      if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
      var v = value[key];
      if (typeof v === 'string') {
        if (/refresh/i.test(key)) walk(v, depth + 1, seen);
        if (/hardware|device/i.test(key) &&
            /^[0-9a-fA-F-]{8,}$/.test(v)) push(v, 'hardware');
        var parsed = tryParse(v);
        if (parsed) walk(parsed, depth + 1, seen);
      } else if (v && typeof v === 'object') {
        walk(v, depth + 1, seen);
      }
    }
  }
  function scanStore(store) {
    if (!store) return;
    try {
      for (var i = 0; i < store.length; i++) {
        var value = store.getItem(store.key(i));
        if (value) walk(value, 0, []);
      }
    } catch (e) {}
  }
  scanStore(localStorage);
  scanStore(sessionStorage);
  // Der keycloak-js/oidc-Client des Readers legt Tokensets haeufig in
  // IndexedDB ab -- der Inhalt wird per zweitem CDP-Aufruf gelesen,
  // hier genuegt die Datenbankliste (Promise + awaitPromise=True).
  return new Promise(function (resolve) {
    var settled = false;
    function finish() {
      if (!settled) { settled = true; resolve(JSON.stringify(out)); }
    }
    try {
      if (window.indexedDB && indexedDB.databases) {
        indexedDB.databases().then(function (dbs) {
          for (var i = 0; i < dbs.length; i++) {
            if (dbs[i] && dbs[i].name) out.idb.push(String(dbs[i].name));
          }
          finish();
        }, finish);
        setTimeout(finish, 800);
        return;
      }
    } catch (e) {}
    finish();
  });
})()
"""


# Liest Token-/Hardware-Werte aus allen Object-Stores einer IndexedDB.
# Der keycloak-js/oidc-Client des Web Readers legt sein Token-Set (mit dem
# AKTUELLEN Refresh-Token) haeufig hier ab statt im localStorage -- genau
# deshalb fand der vorherige Grab bei angemeldetem Fenster nichts.
_IDB_SNIPPET = r"""
(function (dbname) {
  return new Promise(function (resolve) {
    var out = {refresh: [], hardware: []};
    function push(val, bucket) {
      if (typeof val === 'string' && val.length > 20 &&
          out[bucket].indexOf(val) < 0) {
        out[bucket].push(val);
      }
    }
    function tryParse(text) {
      try { return JSON.parse(text); } catch (e) { return null; }
    }
    function walk(value, depth, seen) {
      if (!value || depth > 6) return;
      if (typeof value === 'string') {
        if (/^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$/.test(value)) {
          push(value, 'refresh');
        }
        return;
      }
      if (typeof value !== 'object') return;
      if (seen.indexOf(value) >= 0) return;
      seen.push(value);
      for (var key in value) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) continue;
        var v = value[key];
        if (typeof v === 'string') {
          if (/refresh/i.test(key)) push(v, 'refresh');
          if (/hardware|device/i.test(key) &&
              /^[0-9a-fA-F-]{8,}$/.test(v)) push(v, 'hardware');
          var parsed = tryParse(v);
          if (parsed) walk(parsed, depth + 1, seen);
        } else if (v && typeof v === 'object') {
          walk(v, depth + 1, seen);
        }
      }
    }
    var settled = false;
    function finish() {
      if (!settled) { settled = true; resolve(JSON.stringify(out)); }
    }
    setTimeout(finish, 3000);
    try {
      var open = indexedDB.open(dbname);
      open.onsuccess = function (event) {
        var db = event.target.result;
        var names = [];
        for (var i = 0; i < (db.objectStoreNames || []).length; i++) {
          names.push(db.objectStoreNames[i]);
        }
        var remaining = names.length;
        if (!remaining) { db.close(); finish(); return; }
        names.forEach(function (storeName) {
          try {
            var tx = db.transaction(storeName, 'readonly');
            var req = tx.objectStore(storeName).getAll();
            req.onsuccess = function () {
              (req.result || []).forEach(function (entry) {
                walk(entry, 0, []);
              });
              remaining -= 1;
              if (remaining <= 0) { db.close(); finish(); }
            };
            req.onerror = function () {
              remaining -= 1;
              if (remaining <= 0) { db.close(); finish(); }
            };
          } catch (e) {
            remaining -= 1;
            if (remaining <= 0) { finish(); }
          }
        });
      };
      open.onerror = function () { finish(); };
    } catch (e) { finish(); }
  });
})('%s')
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
            result = _evaluate_in_target(ws_url, _GRAB_SNIPPET, timeout,
                                         await_promise=True)
            if result:
                return result
        time.sleep(0.5)
    return {"refresh": [], "hardware": []}


def _evaluate_raw_in_target(ws_url, expression, timeout=10,
                            await_promise=False):
    """Like _evaluate_in_target but returns CDP's full result dict.

    Returns ``{"ok": bool, "value": <decoded JSON value or raw string>,
    "exception": str}`` -- callers that need the exceptionDetails (e.g.
    an in-page fetch whose failure text matters) use this variant.
    """
    try:
        path = ws_url[len("ws://"):]
        host, port, ws_path = _ws_path_to_host_port(path)
        ws = _Ws.connect(host, port, ws_path, timeout=timeout)
    except (OSError, ValueError):
        return {"ok": False, "value": None, "exception": "connect failed"}
    try:
        ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True,
            "awaitPromise": bool(await_promise),
        }})
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = ws.recv_json(timeout=max(1, deadline - time.time()))
            if message.get("id") != 1:
                continue
            if "error" in message:
                return {"ok": False, "value": None,
                        "exception": str(message.get("error"))[:200]}
            result = (message.get("result") or {}).get("result") or {}
            details = (message.get("result") or {}).get("exceptionDetails")
            if details:
                text = str((details.get("exception") or {}).get("description")
                           or details.get("text") or "page error")
                return {"ok": False, "value": None,
                        "exception": text[:300]}
            if result.get("type") == "string":
                return {"ok": True, "value": result.get("value"),
                        "exception": None}
            if "value" in result:
                return {"ok": True, "value": result.get("value"),
                        "exception": None}
            return {"ok": False, "value": None,
                    "exception": "no value returned"}
        return {"ok": False, "value": None, "exception": "evaluate timeout"}
    except OSError as exc:
        return {"ok": False, "value": None, "exception": str(exc)[:200]}
    finally:
        ws.close()


def _evaluate_in_target(ws_url, expression, timeout=10, await_promise=False):
    """Run a JS expression in a DevTools page target, return its JSON value.

    ``await_promise=True`` makes CDP resolve a Promise returned by the
    expression -- the grab snippet returns one while it enumerates
    IndexedDB databases (keycloak-js stores the token set there).
    """
    try:
        path = ws_url[len("ws://"):]
        host, port, ws_path = _ws_path_to_host_port(path)
        ws = _Ws.connect(host, port, ws_path, timeout=timeout)
    except (OSError, ValueError):
        return None
    try:
        ws.send_json({"id": 1, "method": "Runtime.evaluate", "params": {
            "expression": expression, "returnByValue": True,
            "awaitPromise": bool(await_promise),
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
                    "idb": [t for t in parsed.get("idb", [])
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


def read_idb_tokens(ws_url, db_names, timeout=10):
    """Walk every IndexedDB database's object stores for token values.

    Returns the merged ``{"refresh": [...], "hardware": [...]}`` dict;
    databases that fail to open are skipped (the localStorage/session
    scan already ran in the main grab).
    """
    merged = {"refresh": [], "hardware": []}
    for name in db_names or []:
        if not name or not str(name).strip():
            continue
        expression = _IDB_SNIPPET % str(name).replace("'", "\\'")
        result = _evaluate_in_target(ws_url, expression, timeout,
                                     await_promise=True)
        if not result:
            continue
        for bucket in ("refresh", "hardware"):
            for value in result.get(bucket) or []:
                if value not in merged[bucket]:
                    merged[bucket].append(value)
    return merged


def grab_once_from_grabber(port=None, timeout=12):
    """ONE grab attempt against the running grabber window (no polling).

    Returns the grabbed dict (refresh/hardware/idb lists, possibly empty)
    or None when no reader tab exists. Used by the extract button so the
    Calibre GUI never blocks for minutes waiting for a sign-in that may
    not happen -- the user just clicks the button again after signing in.
    """
    port = port or _start_port()
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
    for target in targets:
        if target.get("type") != "page":
            continue
        if "mytolino.com" not in str(target.get("url") or ""):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if not ws_url.startswith("ws://"):
            continue
        grabbed = _evaluate_in_target(ws_url, _GRAB_SNIPPET, timeout,
                                      await_promise=True) or \
            {"refresh": [], "hardware": [], "idb": []}
        idb_names = grabbed.pop("idb", []) or []
        if idb_names:
            idb_tokens = read_idb_tokens(ws_url, idb_names, timeout)
            for bucket in ("refresh", "hardware"):
                for value in idb_tokens.get(bucket) or []:
                    if value not in grabbed[bucket]:
                        grabbed[bucket].append(value)
        return grabbed
    return None


# Fuehrt den OAuth-Refresh-Grant IN der Reader-Seite aus: gleiches TLS,
# gleiche Sec-Fetch-Familie, gleicher Origin-Kontext wie der Web Reader
# selbst -- der Bot-Schutz vor dem Token-Endpunkt blockt genau diesen
# Anfragen nicht (der Reader macht sie ja staendig selbst).
_EXCHANGE_SNIPPET = r"""
(function (tokenUrl, body) {
  return fetch(tokenUrl, {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body,
    credentials: 'omit',
    referrer: 'https://webreader.mytolino.com/'
  }).then(function (response) {
    return response.text().then(function (text) {
      return JSON.stringify({
        status: response.status,
        body: text.substring(0, 4096)
      });
    });
  }).catch(function (err) {
    return JSON.stringify({status: 0, body: 'fetch failed: ' + err});
  });
})(TOKEN_URL_PLACEHOLDER, BODY_PLACEHOLDER)
"""


def exchange_refresh_in_browser(ws_url, token_url, form_body, timeout=25):
    """Run the refresh-token grant inside the reader page via in-page fetch.

    Returns ``(status, body_text)``. ``status`` mirrors the endpoint's
    HTTP code (0 when the in-page fetch itself failed). The bot-protection
    WAF in front of the token endpoint accepts this request because it is
    the reader's own fingerprint -- the exact request family the reader
    performs on every background rotation.
    """
    expression = (_EXCHANGE_SNIPPET
                  .replace("TOKEN_URL_PLACEHOLDER",
                           json.dumps(str(token_url)))
                  .replace("BODY_PLACEHOLDER", json.dumps(str(form_body))))
    raw = _evaluate_raw_in_target(ws_url, expression, timeout,
                                  await_promise=True)
    if not raw.get("ok"):
        return 0, "page evaluate failed: %s" % (raw.get("exception") or "?")
    try:
        payload = json.loads(raw.get("value") or "{}")
    except ValueError:
        return 0, "unparseable page reply"
    return (int(payload.get("status") or 0),
            str(payload.get("body") or ""))


def reader_ws_url(port=None):
    """WebSocket-Debugger-URL des ersten mytolino-Reader-Tabs (oder None)."""
    port = port or _start_port()
    targets = _http_get_json("http://127.0.0.1:%d/json/list" % port) or []
    for target in targets:
        if target.get("type") != "page":
            continue
        if "mytolino.com" not in str(target.get("url") or ""):
            continue
        ws_url = str(target.get("webSocketDebuggerUrl") or "")
        if ws_url.startswith("ws://"):
            return ws_url
    return None


def await_token_response(ws_url, timeout=120, progress=None):
    """Catch a FRESH refresh_token out of the reader's own token response.

    Enables CDP Fetch interception on the reader tab and waits for the
    next POST to the OAuth token endpoint; the response body is parsed
    and its refresh_token (plus any hardware fields) returned. This is
    the one token guaranteed unused: it is the response the reader is
    about to consume itself.

    Returns ``{"refresh": [...], "hardware": [...]}`` or None when the
    reader tab goes away before a token response was caught.
    """
    if not ws_url or not str(ws_url).startswith("ws://"):
        return None
    try:
        path = ws_url[len("ws://"):]
        host, port, ws_path = _ws_path_to_host_port(path)
        ws = _Ws.connect(host, port, ws_path, timeout=10)
    except (OSError, ValueError):
        return None
    deadline = time.time() + max(15, timeout)
    paused = None
    try:
        ws.send_json({"id": 1, "method": "Fetch.enable", "params": {
            "patterns": [{"urlPattern": "*token*", "requestStage":
                          "Response"}],
        }})
        while time.time() < deadline:
            try:
                message = ws.recv_json(timeout=max(1, deadline - time.time()))
            except OSError:
                break  # tab closed / ws dropped
            method = str(message.get("method") or "")
            params = message.get("params") or {}
            if message.get("id") == 101:  # getResponseBody reply (late)
                continue
            if method != "Fetch.requestPaused":
                continue
            paused = params.get("requestId")
            request = params.get("request") or {}
            response = params.get("response") or {}
            status = int(response.get("status") or 0)
            url = str(request.get("url") or "")
            if status != 200 or "token" not in url:
                ws.send_json({"id": 100, "method": "Fetch.continueRequest",
                              "params": {"requestId": paused}})
                paused = None
                continue
            # 200-er Token-Antwort: Body abgreifen, dann die Antwort an
            # den Reader durchreichen (er verbraucht sie ganz normal --
            # wir sind nur stiller Mitleser).
            ws.send_json({"id": 101, "method": "Fetch.getResponseBody",
                          "params": {"requestId": paused}})
            body, encoded = "", False
            inner_deadline = time.time() + 8
            while time.time() < inner_deadline:
                reply = ws.recv_json(timeout=max(1, inner_deadline -
                                                time.time()))
                if reply.get("id") == 101:
                    result = reply.get("result") or {}
                    body = result.get("body") or ""
                    encoded = bool(result.get("base64Encoded"))
                    break
            if encoded and body:
                body = base64.b64decode(body).decode("utf-8", "replace")
            ws.send_json({"id": 102, "method": "Fetch.fulfillRequest",
                          "params": {"requestId": paused,
                                     "responseCode": status}})
            paused = None
            out = {"refresh": [], "hardware": []}
            try:
                payload = json.loads(body or "{}")
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                refresh = payload.get("refresh_token")
                if isinstance(refresh, str) and len(refresh) > 20:
                    out["refresh"].append(refresh)
                hw = (payload.get("hardware_id")
                      or payload.get("hardwareId"))
                if isinstance(hw, str) and hw:
                    out["hardware"].append(hw)
            if out["refresh"]:
                return out
            # Antwort ohne refresh_token (z. B. Password-Grant-Preheat):
            # weiter lauschen, bis eine Token-Rotation kommt.
    except OSError:
        pass
    finally:
        try:
            ws.close()
        except OSError:
            pass
    return None


def describe_grab_state(port=None, timeout=12):
    """Short diagnostic: reader tab present? any token values visible?

    Redacted by construction -- only counts and store names, never
    token values.
    """
    grabbed = grab_once_from_grabber(port, timeout)
    if grabbed is None:
        return "kein Web-Reader-Tab im Anmeldefenster offen"
    return ("%d Refresh-Kandidat(en), %d Hardware-Kandidat(en) live in der "
            "Seite (IndexedDB-Datenbanken: %s)" % (
                len(grabbed.get("refresh") or []),
                len(grabbed.get("hardware") or []),
                ", ".join(sorted(set(grabbed.get("idb") or []))) or "keine"))


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
            result = _evaluate_in_target(ws_url, _GRAB_SNIPPET, 10,
                                         await_promise=True)
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




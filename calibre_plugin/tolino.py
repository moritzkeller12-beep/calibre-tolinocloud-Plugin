import json
import mimetypes
import os
import platform
import re
import sqlite3
import struct
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


class TolinoError(Exception):
    pass


class TolinoAuthError(TolinoError):
    pass


class TolinoApiError(TolinoError):
    pass


PARTNERS = {
    3: {
        "name": "Thalia.de",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.de/auth/oauth2/token",
        "auth_url": "https://www.thalia.de/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles",
    },
    4: {"name": "Thalia.at", "client_id": "webshop01",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/de.buch.appservices/api/4004/oauth2/token",
        "auth_url": "https://www.thalia.at/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles"},
    6: {"name": "Buch.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE"},
    8: {"name": "Books.ch / orellfuessli.ch", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.orellfuessli.ch/auth/oauth2/token",
        "auth_url": "https://www.orellfuessli.ch/auth/oauth2/autologin",
        "reader_url": "https://webreader.mytolino.com/library/",
        "token_headers": {
            "Origin": "https://webreader.mytolino.com",
            "Referer": "https://webreader.mytolino.com/",
        },
        "x_buchde.mandant_id": "37",
        "x_buchde.skin_id": "17",
        "client_type": "TOLINO_WEBREADER",
        "client_version": "5.2.0"},
    13: {
        "name": "Hugendubel.de",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://api.hugendubel.de/rest/oauth2/token",
        "auth_url": "https://www.hugendubel.de/oauth/authorize",
        "reader_url": "https://webreader.hugendubel.de/library/index.html",
    },
    23: {"name": "Osiander.de", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token",
        "auth_url": "https://www.osiander.de/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles"},
    30: {"name": "Buecher.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE",
        "token_url": "https://www.buecher.de/oauth2/token",
        "auth_url": "https://www.buecher.de/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/"},
}

BASE_URL = "https://bosh.pageplace.de/bosh/rest"
OAUTH_STATE_TTL = 300
TOLINO_READER_ORIGIN = "https://webreader.mytolino.com"


_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


def _find_browser_storage_paths():
    """Return candidate browser profile directories for the current platform.

    Entries may be Chromium "User Data" roots (parent of profile dirs with
    "Local Storage/leveldb") or Firefox profile roots (children are profile
    dirs). Missing paths are fine; the diagnostic reports what exists.
    """
    paths = []
    system = platform.system()

    def add(path):
        if path and path not in paths:
            paths.append(path)

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        roaming = os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming"))
        add(os.path.join(local, "Google", "Chrome", "User Data"))
        add(os.path.join(local, "Microsoft", "Edge", "User Data"))
        add(os.path.join(local, "BraveSoftware", "Brave-Browser", "User Data"))
        add(os.path.join(local, "Chromium", "User Data"))
        add(os.path.join(local, "Vivaldi", "User Data"))
        add(os.path.join(local, "Yandex", "YandexBrowser", "User Data"))
        add(os.path.join(local, "Opera Software", "Opera Stable"))
        add(os.path.join(roaming, "Mozilla", "Firefox", "Profiles"))

    elif system == "Darwin":
        support = os.path.expanduser("~/Library/Application Support")
        add(os.path.join(support, "Google", "Chrome"))
        add(os.path.join(support, "Microsoft Edge"))
        add(os.path.join(support, "BraveSoftware", "Brave-Browser"))
        add(os.path.join(support, "Chromium"))
        add(os.path.join(support, "Vivaldi"))
        add(os.path.join(support, "com.operasoftware.Opera"))
        add(os.path.join(support, "Firefox", "Profiles"))

    else:  # Linux and other Unix
        home = os.path.expanduser("~")
        xdg = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
        flatpak = os.path.join(home, ".var", "app")
        snap = os.path.join(home, "snap")

        # Native (deb/rpm) Chromium-family browsers: <app>/ profiles live
        # directly under XDG_CONFIG_HOME (Default/Local Storage/leveldb).
        for app in (
            "google-chrome", "google-chrome-beta", "google-chrome-unstable",
            "chromium", "chromium-browser", "microsoft-edge",
            "BraveSoftware/Brave-Browser", "vivaldi", "opera",
            "yandex-browser",
        ):
            add(os.path.join(xdg, app))

        # Flatpak sandboxed browsers: ~/.var/app/<app-id>/config/<app>/
        for app_id, app_dir in (
            ("com.google.Chrome", "google-chrome"),
            ("com.google.ChromeDev", "google-chrome-unstable"),
            ("org.chromium.Chromium", "chromium"),
            ("com.microsoft.Edge", "microsoft-edge"),
            ("com.brave.Browser", "BraveSoftware/Brave-Browser"),
            ("com.vivaldi.Vivaldi", "vivaldi"),
            ("com.opera.Opera", "opera"),
            ("de.yasuar.yandex-browser" , "yandex-browser"),
            ("org.mozilla.firefox", None),  # Firefox handled below
            ("org.chromium.Chromium.browser", "chromium"),
        ):
            base = os.path.join(flatpak, app_id, "config")
            if app_dir is None:
                # Flatpak Firefox keeps profiles under ~/.var/app/<id>/.mozilla
                add(os.path.join(flatpak, app_id, ".mozilla", "firefox"))
                add(os.path.join(base, "mozilla", "firefox"))
            else:
                add(os.path.join(base, app_dir))

        # Snap browsers: ~/snap/<name>/common/... (current data dir)
        add(os.path.join(snap, "firefox", "common", ".mozilla", "firefox"))
        add(os.path.join(snap, "chromium", "common", "chromium"))
        add(os.path.join(snap, "opera", "common", "opera"))
        add(os.path.join(snap, "brave", "common", "BraveSoftware", "Brave-Browser"))

        # Classic Firefox locations
        add(os.path.join(home, ".mozilla", "firefox"))

    return [os.path.normpath(p) for p in paths]


# Origins (scheme+host, no trailing path) that may hold Tolino credentials.
TOLINO_STORAGE_ORIGINS = (
    "webreader.mytolino.com",
    "pageplace.de",
    "orellfuessli.ch",
    "thalia.de", "thalia.at",
    "buch.de", "books.ch",
    "hugendubel.de", "osiander.de", "buecher.de",
    "buchhaus.ch", "wolters-mauritz.de", "book-club-family.de",
    "delibur.com", "thienemueller.de", "ohlala.ch",
    "keycloak", "auth",  # generic IdP path hints
)


def _origin_matches(host_text):
    text = str(host_text).casefold()
    return any(origin in text for origin in TOLINO_STORAGE_ORIGINS)


# --- Chromium LevelDB: raw, dependency-free readers -----------------------
#
# Modern Chromium stores localStorage in LevelDB files (Local Storage/
# leveldb/*.ldb + *.log). Keys are <origin>\x00\x01<key>, values are
# \x01<utf8> or \x00<uint8 length><utf16le>. We simply scan the raw bytes of
# every record and pull out the UTF-8 keys/values belonging to Tolino
# origins, which is robust across record encodings.

_LEVELDB_BLOCK_SIZE = 32768
_LEVELDB_HEADER_SIZE = 7
_LEVELDB_MAGIC = b"\xf7\xcf\xf0\x9c\x96\x5d\xb6\x8b"


def _varint(data, pos):
    """Decode a LevelDB varint; returns (value, next_pos) or (None, pos)."""
    result = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, pos
        shift += 7
        if shift > 63:
            return None, pos
    return None, pos


def _leveldb_log_payloads(data):
    """Yield record payloads from LevelDB log physical blocks.

    Format: 32 KiB blocks of [crc32(4), length(2), type(1), payload];
    type 1 = FULL, 2/3/4 = FIRST/MIDDLE/LAST fragments.
    """
    pos = 0
    n = len(data)
    pending = b""
    while pos + _LEVELDB_HEADER_SIZE <= n:
        block_pos = pos % _LEVELDB_BLOCK_SIZE
        if block_pos + _LEVELDB_HEADER_SIZE > _LEVELDB_BLOCK_SIZE:
            # block trailer padding; skip to the next block boundary
            pos += _LEVELDB_BLOCK_SIZE - block_pos
            continue
        length, record_type = struct.unpack_from("<HB", data, pos + 4)
        if record_type == 0 or length > _LEVELDB_BLOCK_SIZE:
            break  # zero padding or corrupt block
        pos += _LEVELDB_HEADER_SIZE
        if length > n - pos:
            break
        payload = data[pos:pos + length]
        pos += length
        if record_type == 1:  # FULL
            if pending:
                pending = b""
            yield payload
        elif record_type == 2:  # FIRST
            pending = payload
        elif record_type == 3:  # MIDDLE
            pending += payload
        elif record_type == 4:  # LAST
            yield pending + payload
            pending = b""


def _leveldb_writebatch_pairs(payload):
    """Yield (key, value) pairs from one WriteBatch payload.

    Layout: sequence(8 LE) + count(4 LE), then entries:
    type(1: 1=put, 0=delete) klen(varint) key vlen(varint) value.
    """
    if len(payload) < 12:
        return
    pos = 12
    n = len(payload)
    while pos < n:
        op = payload[pos]
        pos += 1
        if op == 0:  # delete
            _, pos = _varint(payload, pos)
            continue
        if op != 1:  # unknown op; cannot resync safely
            return
        klen, pos = _varint(payload, pos)
        if klen is None or pos + klen > n:
            return
        key = payload[pos:pos + klen]
        pos += klen
        vlen, pos = _varint(payload, pos)
        if vlen is None or pos + vlen > n:
            return
        value = payload[pos:pos + vlen]
        pos += vlen
        yield key, value


def _leveldb_records_from_log(data):
    """Yield (key, value) pairs from a Chromium localStorage LevelDB log."""
    for payload in _leveldb_log_payloads(data):
        for key, value in _leveldb_writebatch_pairs(payload):
            yield key, value


def _leveldb_records_from_ldb(data):
    """Yield (key, value) pairs from an .ldb/.sst table file (simplified scan)."""
    # .ldb files contain data blocks prefixed by 5-byte trailer handles. A full
    # parser needs block restarts/compression; scanning for the origin marker
    # plus a following value byte is far simpler and good enough for finding
    # token-shaped records.
    marker = _TOLINO_ORIGIN_MARKER
    pos = 0
    n = len(data)
    while True:
        idx = data.find(marker, pos)
        if idx < 0:
            return
        # The key continues until a control byte (0x01/0x02) or value marker.
        end = idx + len(marker)
        key_end = end
        while key_end < n and data[key_end] not in (0x00, 0x01, 0x02):
            key_end += 1
        key = data[idx:key_end]
        value_start = key_end
        if value_start < n and data[value_start] in (0x01, 0x02):
            value_start += 1
        value_end = value_start
        while value_end < n and data[value_end] not in (0x00, 0x01, 0x02):
            value_end += 1
        value = data[value_start:value_end]
        yield key, value
        pos = idx + len(marker)


_TOLINO_ORIGIN_MARKER = b"webreader.mytolino.com"


def _decode_leveldb_text(raw):
    """Decode a LevelDB key/value blob to text when it looks like UTF-8."""
    if not raw:
        return ""
    # Chromium prepends a scheme marker like _https:// or https://; keep it.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ""
    return text


def _leveldb_pair_has_token_shape(key_text, value_text):
    lowered = key_text.casefold()
    for name in ("refresh", "t_auth", "hardware"):
        if name in lowered:
            return True
    return False


def _leveldb_value_text(raw):
    """Decode a Chromium localStorage value blob (\x01utf8 / \x00len utf16)."""
    if not raw:
        return ""
    if raw[0] == 0x01:
        return _decode_leveldb_text(raw[1:])
    if raw[0] == 0x00 and len(raw) >= 3:
        # \x00 + uint8 length + UTF-16LE bytes
        size = raw[1]
        blob = raw[2:2 + size * 2]
        try:
            return blob.decode("utf-16-le")
        except UnicodeDecodeError:
            return ""
    return _decode_leveldb_text(raw)


def _scan_chromium_leveldb(db_dir):
    """Scan a Chromium Local/Session Storage LevelDB directory for Tolino keys."""
    found = {}
    for file_name in sorted(os.listdir(db_dir)):
        if not file_name.endswith((".log", ".ldb")):
            continue
        file_path = os.path.join(db_dir, file_name)
        try:
            with open(file_path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if _TOLINO_ORIGIN_MARKER not in data:
            continue
        if file_name.endswith(".log"):
            iterator = _leveldb_records_from_log(data)
        else:
            iterator = _leveldb_records_from_ldb(data)
        for key, value in iterator:
            key_text = _decode_leveldb_text(key)
            value_text = _leveldb_value_text(value)
            if not _origin_matches(key_text) or not value_text:
                continue
            if not _leveldb_pair_has_token_shape(key_text, value_text):
                continue
            # Drop the scheme prefix and the 0x00/0x01 separator from the key
            storage_key = key_text
            for prefix in ("_https://", "https://", "_http://", "http://"):
                if storage_key.casefold().startswith(prefix.casefold()):
                    storage_key = storage_key[len(prefix):]
                    break
            found[storage_key.lstrip("\x00\x01\x02")] = value_text
    return found


# --- Firefox (LSNG): webappsstore.sqlite ----------------------------------


def _lsng_value_text(value):
    """Decode one LSNG value (BLOB with 0x01/0x02 prefix, or str)."""
    if isinstance(value, str):
        return value
    if value is None:
        return None
    blob = bytes(value)
    if not blob:
        return ""
    prefix, body = blob[:1], blob[1:]
    # LSNG spec: 0x01 prefix = UTF-16LE string, 0x02 prefix = UTF-8 string.
    if prefix == b"\x01":
        try:
            return body.decode("utf-16-le")
        except UnicodeDecodeError:
            return body.decode("utf-8", "replace")
    if prefix == b"\x02":
        return body.decode("utf-8", "replace")
    # Legacy row without prefix: try UTF-8, fall back to UTF-16LE heuristic.
    try:
        return blob.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return blob.decode("utf-16-le")
        except UnicodeDecodeError:
            return blob.decode("utf-8", "replace")


def _read_firefox_storage(profile_path):
    """Read localStorage from Firefox LSNG webappsstore.sqlite (BLOB values)."""
    results = {}
    db_path = os.path.join(profile_path, "webappsstore.sqlite")
    if not os.path.exists(db_path):
        return results
    try:
        conn = sqlite3.connect("file:%s?immutable=1" % db_path, uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT originKey, key, value FROM data")
            for origin_key, key, value in cursor.fetchall():
                origin_text = _lsng_value_text(origin_key)
                value = _lsng_value_text(value)
                if not isinstance(key, str):
                    key = str(key)
                results["%s/%s" % (origin_text, key)] = value
        finally:
            conn.close()
    except Exception:
        return results
    return results


def _read_firefox_session_storage(profile_path):
    """Firefox sessionStorage lives in the same LSNG database (scoped column)."""
    results = {}
    db_path = os.path.join(profile_path, "webappsstore.sqlite")
    if not os.path.exists(db_path):
        return results
    try:
        conn = sqlite3.connect("file:%s?immutable=1" % db_path, uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT originKey, key, value, conversionType FROM data")
            for origin_key, key, value, conversion in cursor.fetchall():
                if (conversion or 0) & 1:  # sessionStorage flag (1 << 0)
                    continue
                origin_text = _lsng_value_text(origin_key)
                value = _lsng_value_text(value)
                results["%s/%s" % (origin_text, key)] = value
        finally:
            conn.close()
    except Exception:
        return results
    return results


def _collect_storage_under(path, notes):
    """Walk a candidate root and harvest any Tolino storage entries found.

    Finds Chromium "Local Storage/leveldb" and "Session Storage/leveldb"
    directories plus Firefox "webappsstore.sqlite" files at any depth up to
    5; ``notes`` receives one entry per distinct storage found.
    """
    found = {}
    seen_dirs = set()
    if not os.path.isdir(path):
        return found
    base_depth = path.rstrip(os.sep).count(os.sep)
    try:
        walker = os.walk(path)
        for root, dirs, files in walker:
            depth = root.rstrip(os.sep).count(os.sep) - base_depth
            if depth >= 5:
                dirs[:] = []
            # Chromium: any directory named leveldb inside Local/Session Storage
            if root.endswith(os.path.join("Local Storage", "leveldb")) or \
                    root.endswith(os.path.join("Session Storage", "leveldb")):
                if root not in seen_dirs:
                    seen_dirs.add(root)
                    notes.append("Chromium-Storage: %s" % root)
                    found.update(_scan_chromium_leveldb(root))
                dirs[:] = []  # no deeper storage below a leveldb dir
                continue
            # Firefox: webappsstore.sqlite at any depth (profiles root or tree)
            for name in files:
                if name == "webappsstore.sqlite":
                    db = os.path.join(root, name)
                    notes.append("Firefox-Storage: %s" % db)
                    found.update(_read_firefox_storage(root))
                    found.update(_read_firefox_session_storage(root))
            # Skip heavy noise dirs that never hold web storage
            dirs[:] = [d for d in dirs if d not in (
                "Cache", "Cache2", "Code Cache", "GPUCache", "DawnCache",
                "GrShaderCache", "ShaderCache", "Service Worker", "blob_storage",
                "IndexedDB", "Sessions", "Crashpad", "thumbnails")]
    except OSError:
        return found
    return found


def _extract_tokens_from_storage(storage_data):
    """Extract refresh_token and hardware_id from a storage snapshot.

    Works on string, bytes or JSON-bundle values; never logs token values.
    """
    refresh_token = None
    hardware_id = None

    def value_text(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", "replace")
        return str(value)

    def usable(text):
        text = (text or "").strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, dict):
            for name in TOKEN_VALUE_KEYS:
                nested = parsed.get(name)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return None

    for key, value in storage_data.items():
        key_lower = str(key).casefold()
        text = value_text(value)
        if refresh_token is None:
            if any(name in key_lower for name in ("refresh", "t_auth")):
                refresh_token = usable(text)
        if hardware_id is None:
            if any(name in key_lower for name in ("hardware", "device")):
                hardware_id = usable(text)

    if refresh_token is None:
        for value in storage_data.values():
            candidate = usable(value_text(value))
            if candidate:
                refresh_token = candidate
                break

    if refresh_token is not None and not isinstance(refresh_token, str):
        refresh_token = str(refresh_token)
    if hardware_id is not None and not isinstance(hardware_id, str):
        hardware_id = str(hardware_id)
    return refresh_token, hardware_id


TOKEN_VALUE_KEYS = ("refresh_token", "t_auth_token", "refreshToken", "refresh-token")
HARDWARE_VALUE_KEYS = ("hardware_id", "hardwareId", "device_id", "deviceId")


def extract_login_tokens(storage):
    """Extract refresh token and hardware ID from one login storage snapshot.

    Accepts a mapping of storage keys to values (from an embedded browser's
    Local Storage or the page's sessionStorage/localStorage). Values may be
    plain strings or JSON strings; credential values are never logged.
    """
    refresh = None
    hardware = None
    if not isinstance(storage, dict):
        return None, None

    def _candidate(value):
        if not isinstance(value, str):
            return None
        text = value.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except ValueError:
            return text
        if isinstance(parsed, dict):
            for key in TOKEN_VALUE_KEYS:
                nested = parsed.get(key)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
            return None
        return None

    for key, value in storage.items():
        key_text = str(key)
        key_lower = key_text.casefold()
        if refresh is None:
            if key_lower in {name.casefold() for name in TOKEN_VALUE_KEYS}:
                refresh = _candidate(value)
            elif any(name in key_lower for name in ("refresh", "t_auth")):
                refresh = _candidate(value)
        if hardware is None:
            for name in HARDWARE_VALUE_KEYS:
                if name.casefold() in key_lower:
                    hardware = _candidate(value)
                    break
    if refresh is None:
        # Fallback: some partners (e.g. Keycloak) store the token set as a
        # JSON object under a generic storage key.
        for value in storage.values():
            if not isinstance(value, str):
                continue
            try:
                parsed = json.loads(value)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                for name in TOKEN_VALUE_KEYS:
                    nested = parsed.get(name)
                    if isinstance(nested, str) and nested.strip():
                        refresh = nested.strip()
                        break
            if refresh is not None:
                break
    return refresh, hardware


def scrape_browser_tokens(diagnose=False):
    """Read Tolino refresh_token/hardware_id from installed browsers.

    Supports modern Chromium LevelDB stores (Chrome, Edge, Brave, Chromium,
    Vivaldi, Opera) and modern Firefox LSNG (webappsstore.sqlite) without any
    third-party module. When ``diagnose`` is true, returns a third element:
    a redacted list describing what was scanned (no token values).
    """
    notes = []
    checked = []
    for path in _find_browser_storage_paths():
        exists = os.path.isdir(path)
        checked.append("%s%s" % (path, "" if exists else "  (fehlt)"))
        if not exists:
            continue
        storage = _collect_storage_under(path, notes)
        if storage:
            refresh_token, hardware_id = _extract_tokens_from_storage(storage)
            if refresh_token and hardware_id:
                return (refresh_token, hardware_id, notes) if diagnose \
                    else (refresh_token, hardware_id)
    if diagnose and not notes:
        notes.append("Kein Chromium- (Local Storage/leveldb) oder Firefox-Storage "
                     "(webappsstore.sqlite) gefunden. Geprüfte Orte:")
        notes.extend(checked)
    return (None, None, notes) if diagnose else (None, None)


_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|"
    r"t_auth_token|password|secret)\b\s*[:=]\s*"
    r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^,\s;}&]+)"
)


def sanitize_error(value, secrets=()):
    """Remove configured credentials and token-shaped values from any output."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if (
                "token" in str(key).casefold()
                or str(key).casefold() in {
                    "authorization", "password", "secret", "t_auth_token"
                }
            ) else sanitize_error(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_error(item, secrets) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    candidates = set()
    for secret in secrets:
        if secret is None:
            continue
        raw = str(secret)
        trimmed = raw.strip()
        if trimmed:
            candidates.update((raw, trimmed, '"' + trimmed + '"', "'" + trimmed + "'"))
            if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
                normalized = trimmed[1:-1].strip()
                candidates.update((normalized, '"' + normalized + '"',
                                   "'" + normalized + "'"))
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: "%s=[REDACTED]" % match.group(1), text
    )
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    return _JWT_PATTERN.sub("[REDACTED]", text)


def normalize_refresh_token(value):
    """Normalize a token copied from a browser field without exposing it."""
    raw = "" if value is None else str(value)
    trimmed = raw.strip()
    whitespace_removed = trimmed != raw
    quote_removed = len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'"
    if quote_removed:
        trimmed = trimmed[1:-1].strip()
        whitespace_removed = whitespace_removed or trimmed != raw.strip()[1:-1]
    return trimmed, {
        "token_category": "configured_refresh_token",
        "token_length": len(trimmed),
        "token_prefix": (trimmed[:4] + "...") if trimmed else "",
        "outer_quotes_removed": quote_removed,
        "surrounding_whitespace_removed": whitespace_removed,
    }


def _query_value(query, name):
    if not isinstance(query, dict):
        raise TolinoAuthError("Browser login returned invalid callback data.")
    values = query.get(name)
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        return values
    if isinstance(values, (tuple, list)):
        return next(iter(values), None)
    raise TolinoAuthError("Browser login returned invalid %s data." % name)


def hardware_id():
    os_id = {"Windows": "1", "Darwin": "2", "Linux": "3"}.get(platform.system(), "x")
    return "%sxxA-00BCD-EFGHI-JKLMN-OPQRh" % os_id


def callback_redirect_uri(port):
    return "http://127.0.0.1:%d/callback" % int(port)


def validate_callback(query, expected_state, created_at, now=None):
    """Validate one OAuth callback without accepting tokens from the URL."""
    now = time.time() if now is None else now
    if now - created_at > OAUTH_STATE_TTL:
        raise TolinoAuthError("Browser login expired. Please try again.")
    if _query_value(query, "state") != expected_state:
        raise TolinoAuthError("Browser login state did not match.")
    if _query_value(query, "error"):
        raise TolinoAuthError("Browser login was rejected by the partner.")
    code = _query_value(query, "code")
    if not code:
        raise TolinoAuthError("Browser login returned no authorization code.")
    return code


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.query = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Login received. You can return to Calibre.")

    def log_message(self, *_args):
        return


def browser_login(partner_id, hardware, timeout=OAUTH_STATE_TTL):
    """Run an OAuth callback for all partners with valid auth_url."""
    partner = PARTNERS.get(int(partner_id))
    
    # Special handling for Orell Fussli (partner 8) - uses Keycloak
    # Keycloak redirects to webreader.mytolino.com, not back to localhost callback
    if int(partner_id) == 8:
        if not partner or not partner.get("auth_url"):
            raise TolinoAuthError(
                "Orell F\u00fcssli authentication requires manual token extraction. "
                "Use 'Token aus Browser extrahieren' instead."
            )
        params = {
            "client_id": partner["client_id"],
            "response_type": "code",
            "scope": partner["scope"],
        }
        for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
            if partner.get(key):
                params[key] = partner[key]
        auth_url = partner["auth_url"] + "?" + urlencode(params)
        if not webbrowser.open(auth_url):
            raise TolinoAuthError("Could not open the system browser.")
        raise TolinoAuthError(
            "Orell F\u00fcssli authentication completed in browser. "
            "Please: 1) Sign in in the browser that just opened, "
            "2) Close the browser when done, "
            "3) Click 'Token aus Browser extrahieren' to get your tokens."
        )
    
    if not partner or not partner.get("auth_url"):
        raise TolinoAuthError(
            "This Tolino partner has no OAuth authorization URL configured. "
            "Use a Web Reader refresh token in the configuration as fallback."
        )
    if not partner.get("token_url"):
        raise TolinoAuthError(
            "This Tolino partner has no token endpoint configured. "
            "Use a Web Reader refresh token in the configuration as fallback."
        )
    state = uuid.uuid4().hex
    created_at = time.time()
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = timeout
    redirect_uri = callback_redirect_uri(server.server_port)
    
    params = {
        "client_id": partner["client_id"],
        "response_type": "code",
        "scope": partner["scope"],
        "redirect_uri": redirect_uri,
        "state": state,
    }
    for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
        if partner.get(key):
            params[key] = partner[key]
    
    if not webbrowser.open(partner["auth_url"] + "?" + urlencode(params)):
        server.server_close()
        raise TolinoAuthError("Could not open the system browser.")
    while not hasattr(server, "query") and time.time() - created_at < timeout:
        server.handle_request()
    query = getattr(server, "query", {})
    server.server_close()
    code = validate_callback(query, state, created_at)
    client = TolinoClient(partner_id, hardware)
    payload = {
        "client_id": partner["client_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if partner_id != 8:
        payload["scope"] = partner["scope"]
    data = client._request(partner["token_url"], "POST", payload,
                           form=True, authenticated=False)
    if not data.get("access_token") or not data.get("refresh_token"):
        raise TolinoAuthError("Browser login returned an incomplete token response.")
    return data["refresh_token"], client.hardware


def redact_error_text(value):
    """Keep provider status text while removing credential-shaped values."""
    return sanitize_error(value)[:500]


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, username=None,
                 secret=None, timeout=45, token_callback=None):
        if partner_id not in PARTNERS:
            raise TolinoError("Unsupported Tolino partner ID: %s" % partner_id)
        self.partner_id = int(partner_id)
        self.partner = PARTNERS[self.partner_id]
        self.hardware = hardware or hardware_id()
        self.refresh, self.token_diagnostics = normalize_refresh_token(refresh)
        self.username = username
        self.password = secret
        self.timeout = timeout
        self.access = None
        self.expires_at = 0
        self.last_http_status = None
        self.last_error_text = None
        self.token_callback = token_callback
        self._login_lock = threading.Lock()

    def auth_diagnostics(self):
        """Return safe authentication context for the debug report."""
        return {
            "partner_id": self.partner_id,
            "partner_name": self.partner["name"],
            "client_id": self.partner.get("client_id"),
            "scope": self.partner.get("scope"),
            "token_url": self.partner.get("token_url"),
            "grant_type": "refresh_token",
            "hardware_id": self.hardware,
            "reseller_id": str(self.partner_id),
            "http_status": self.last_http_status,
            "error_text": sanitize_error(self.last_error_text,
                                         (self.refresh, self.access)),
            **self.token_diagnostics,
        }

    def login(self):
        # A valid access token avoids spending the rotating refresh token twice.
        if self.access and time.time() < self.expires_at:
            return self.refresh
        with self._login_lock:
            if self.access and time.time() < self.expires_at:
                return self.refresh
            return self._login()

    def _login(self):
        if self.refresh:
            if not self.partner.get("token_url"):
                raise TolinoAuthError(
                    "This partner has no verified refresh-token endpoint in the reference client."
                )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh,
            }
            if self.partner_id != 8:
                payload["scope"] = self.partner["scope"]
        elif self.username and self.password:
            raise TolinoAuthError(
                "Username/password login requires the partner's browser OAuth flow. "
                "Use a Web Reader refresh token for this plugin."
            )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": self.partner["scope"],
            }
        else:
            raise TolinoAuthError("Configure a refresh token or username/password.")
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except TolinoApiError as exc:
            detail = str(exc)
            if "invalid_grant" in detail.casefold() or "reuse exceeded" in detail.casefold():
                # Invalidate the current refresh token to prevent reuse
                self.refresh = None
                self.access = None
                self.expires_at = 0
                raise TolinoAuthError(
                    "Tolino rejected this refresh token because it was reused or invalid. "
                    "Sign in to the Web Reader again, copy its new refresh token, and "
                    "do not test this token repeatedly."
                ) from exc
            raise TolinoAuthError("Tolino authentication failed: %s" % exc)
        if not data.get("access_token"):
            raise TolinoAuthError("Tolino token response did not contain access_token.")
        previous_refresh = self.refresh
        self.access = data["access_token"]
        new_refresh = data.get("refresh_token", self.refresh)
        
        # CRITICAL: Save the new refresh token IMMEDIATELY before any further use
        # Tolino invalidates refresh tokens after single use, so we must persist
        # the new token before making any authenticated requests with it
        if new_refresh != previous_refresh:
            self.refresh = new_refresh
            if self.token_callback:
                self.token_callback(self.refresh)
        else:
            self.refresh = new_refresh
        
        self.expires_at = time.time() + max(0, int(data.get("expires_in", 3600)) - 60)
        return self.refresh

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _retry=True):
        if authenticated and (not self.access or time.time() >= self.expires_at):
            self.login()
        body = None
        headers = {"User-Agent": "Calibre-Tolino-Plugin/0.2"}
        if authenticated:
            headers.update({
                "t_auth_token": self.access,
                "hardware_id": self.hardware,
                "reseller_id": str(self.partner_id),
            })
            for key in ("client_type", "client_version"):
                if self.partner.get(key):
                    headers[key] = self.partner[key]
        elif url == self.partner.get("token_url"):
            headers.update(self.partner.get("token_headers", {}))
        if data is not None:
            if form:
                body = urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, bytes):
                body = data
                headers["Content-Type"] = content_type or "application/octet-stream"
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = content_type or "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self.last_http_status = response.status
                self.last_error_text = None
                raw = response.read()
        except HTTPError as exc:
            raw_detail = exc.read().decode("utf-8", "replace")
            self.last_http_status = exc.code
            try:
                payload = json.loads(raw_detail)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                safe_detail = {
                    key: payload[key] for key in ("error", "error_description")
                    if key in payload
                }
                detail = json.dumps(safe_detail, ensure_ascii=False, sort_keys=True)
            else:
                detail = raw_detail
            self.last_error_text = sanitize_error(
                detail, (self.refresh, self.access)
            )[:500]
            retryable_auth = (
                exc.code == 401 and authenticated and self.refresh and _retry
                and "invalid_grant" not in self.last_error_text.casefold()
                and "reuse exceeded" not in self.last_error_text.casefold()
            )
            if retryable_auth:
                self.access = None
                self.login()
                return self._request(url, method, data, form, True,
                                     content_type, _retry=False)
            if exc.code in (401, 403):
                self.access = None
                raise TolinoAuthError("Tolino rejected authentication (%s)." % exc.code)
            raise TolinoApiError("Tolino HTTP %s: %s" % (exc.code, self.last_error_text))
        except (URLError, OSError) as exc:
            raise TolinoApiError("Tolino request failed: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise TolinoApiError("Tolino returned invalid JSON.")

    def inventory(self):
        data = self._request(BASE_URL + "/inventory/delta?strip=true")
        inventory = data.get("PublicationInventory", {})
        if isinstance(inventory, dict):
            records = []
            for key in ("edata", "ebook"):
                value = inventory.get(key, [])
                records.extend(value if isinstance(value, list) else [value])
            return records
        return inventory if isinstance(inventory, list) else []

    def inventory_ids(self):
        ids = set()
        for item in self.inventory():
            if not isinstance(item, dict):
                continue
            value = item.get("deliverableId") or item.get("deliverable_id") or item.get("id")
            if not value:
                for container in ("deliverable", "metadata", "ebook", "edata"):
                    nested = item.get(container)
                    if isinstance(nested, dict):
                        value = (nested.get("deliverableId") or
                                 nested.get("deliverable_id") or nested.get("id"))
                        if value:
                            break
            if value:
                ids.add(str(value))
        return ids

    @staticmethod
    def _multipart(fields, file_path, field="file"):
        boundary = ("----CalibreTolino%s" % uuid.uuid4().hex).encode("ascii")
        chunks = []
        for key, value in fields.items():
            chunks.extend([b"--" + boundary + b"\r\n",
                           ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
                           str(value).encode(), b"\r\n"])
        name = os.path.basename(file_path)
        guessed_type = mimetypes.guess_type(name)
        mime = next(iter(guessed_type), None) or "application/octet-stream"
        chunks.extend([b"--" + boundary + b"\r\n",
                       ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, name)).encode(),
                       ("Content-Type: %s\r\n\r\n" % mime).encode(),
                       open(file_path, "rb").read(), b"\r\n",
                       b"--" + boundary + b"--\r\n"])
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary.decode()

    def upload(self, file_path):
        body, content_type = self._multipart({}, file_path)
        data = self._request(BASE_URL + "/upload", "POST", body,
                             content_type=content_type)
        deliverable = data.get("metadata", {}).get("deliverableId")
        if not deliverable:
            raise TolinoApiError("Upload response did not contain deliverableId.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, file_path):
        body, content_type = self._multipart({"deliverableId": deliverable_id}, file_path)
        self._request(BASE_URL + "/cover", "POST", body, content_type=content_type)

    def delete(self, deliverable_id):
        self._request(BASE_URL + "/deletecontent?deliverableId=" + str(deliverable_id))

import json
import mimetypes
import os
import platform
import re
import shutil
import sqlite3
import struct
import tempfile
import threading
import time
import uuid
import webbrowser
import base64
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote as _unquote, urlencode, urlparse, quote as _url_quote
from urllib.request import Request, urlopen
import subprocess


class TolinoError(Exception):
    pass


class TolinoAuthError(TolinoError):
    pass


class TolinoApiError(TolinoError):
    pass


# --- Gemeinsamer Web Reader: EIN Login-Profil fuer alle Shops ---------#
# Jeder Reseller (thalia, Hugendubel, eBook.de, ...) traegt als OAuth-
# Redirect https://webreader.mytolino.com/library/; uses_shared_webreader()
# entscheidet, welcher Shop dort liegt.
WEBREADER_URL = "https://webreader.mytolino.com/library/"
WEBREADER_TOKEN_HEADERS = {
    "Origin": "https://webreader.mytolino.com",
    "Referer": "https://webreader.mytolino.com/",
}
WEBREADER_CLIENT_TYPE = "TOLINO_WEBREADER"
WEBREADER_CLIENT_VERSION = "5.2.0"

PARTNERS = {
    # "reseller_id" = ID, die die Tolino-API erwartet (Header/Protokoll);
    # "key" ist nur die interne, historische Positions-ID (Migration).
    # Endpunkte folgen v2/resellerconfig -- dieselbe Quelle wie beim Reader.
    1: {
        "name": "Thalia.de",
        "key": 3,
        "reseller_id": "3",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.de/auth/oauth2/token",
        "auth_url": "https://www.thalia.de/auth/oauth2/authorize",
        "reader_url": WEBREADER_URL,
    },
    2: {"name": "Thalia.at",
        "key": 4,
        "reseller_id": "4",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/auth/oauth2/token",
        "auth_url": "https://www.thalia.at/auth/oauth2/authorize",
        "reader_url": WEBREADER_URL},
    # Buch.de ist eingestellt (www.buch.de leitet auf thalia.de um);
    # ohne eigene Endpunkte bleibt hier der klare Fehler "keine
    # OAuth-Anmeldeseite hinterlegt". Der moderne Nachfolger des
    # Shops steht als Partner 8 (eBook.de) daneben.
    3: {"name": "Buch.de",
        "key": 6,
        "reseller_id": "6",
        "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE"},
    4: {"name": "Books.ch / orellfuessli.ch",
        "key": 8,
        "reseller_id": "8",
        "client_id": "webreader",
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
    5: {"name": "Hugendubel.de",
        "key": 13,
        "reseller_id": "13",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://www.hugendubel.de/oauth/token",
        "auth_url": "https://www.hugendubel.de/oauth/authorize",
        # webreader.hugendubel.de leitet 301 auf den gemeinsamen Reader
        # (mit reseller=13&autologin=true) -- ab jetzt direkt dorthin.
        "reader_url": WEBREADER_URL},
    6: {"name": "Osiander.de",
        "key": 23,
        "reseller_id": "23",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token",
        "auth_url": "https://www.osiander.de/auth/oauth2/authorize",
        "reader_url": WEBREADER_URL},
    7: {"name": "Buecher.de",
        "key": 30,
        "reseller_id": "30",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.buecher.de/auth/oauth2/token",
        "auth_url": "https://www.buecher.de/auth/oauth2/authorize",
        "reader_url": WEBREADER_URL},
    8: {"name": "eBook.de",
        "key": 81,
        "reseller_id": "81",
        "client_id": "ebookde0501html5readerV0001",
        "scope": "e-publishing",
        "token_url": "https://www.ebook.de/oauth/token",
        "auth_url": "https://www.ebook.de/oauth/authorize",
        "reader_url": WEBREADER_URL},
}

# Historic plugin-internal IDs -> new consecutive IDs.
LEGACY_PARTNER_IDS = {
    partner["key"]: pid for pid, partner in PARTNERS.items() if "key" in partner
}

def resolve_partner_id(value):
    """Map a saved (possibly historic) partner ID to the current plugin ID."""
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return value
    if pid in PARTNERS:
        return pid
    if pid in LEGACY_PARTNER_IDS:
        return LEGACY_PARTNER_IDS[pid]
    return pid


def force_legacy_partner_id(value):
    """Unconditionally map a historic partner ID (for the one-time config
    migration, where a stored ID can only come from the legacy numbering)."""
    try:
        pid = int(value)
    except (TypeError, ValueError):
        return value
    return LEGACY_PARTNER_IDS.get(pid, pid)

def uses_shared_webreader(partner):
    """True when the shop runs on the shared Tolino Web Reader."""
    if not isinstance(partner, dict):
        return False
    return "webreader.mytolino.com" in str(partner.get("reader_url") or "")


BASE_URL = "https://bosh.pageplace.de/bosh/rest"
OAUTH_STATE_TTL = 300
# registerhw muss am BOSH-Dienst ankommen, bevor die ID akzeptiert wird
# (Feldbefund 0.9.36: der Diagnose-Test direkt nach der Registrierung
# warf noch 400, der naechste Klick Sekunden spaeter lief) -- vor dem
# ersten und vor dem letzten Retry wird kurz gewartet.
_REGISTER_SETTLE_SECONDS = 1.5
# Feldbefund 0.9.38: der erste Token-Aufruf nach der Browser-Anmeldung traf
# die Bot-Schutz-Laufzeitbahn (403 + HTML-Fehlerseite), jeder spaetere Klick
# lief durch -- zwei Warteversuche, bevor der Fehler sichtbar wird.
_WAF_RETRY_SECONDS = (2.0, 5.0)


_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


# --- crypto-js-kompatibles AES (nur stdlib) -------------------------------
# CryptoJS.AES.encrypt(value, VERSION.PHRASE) -> Base64 in IndexedDB
# (userToken/userInfos): OpenSSL-"Salted__"-KDF, AES-256-CBC; jede
# Passphrase wird akzeptiert, auch eine kuenftig geaenderte.

_AES_SBOX = []
_AES_INV_SBOX = []
_AES_RCON = []


def _aes_init_tables():
    """Build AES S-box / inverse S-box / round constants once (stdlib only)."""
    global _AES_SBOX, _AES_INV_SBOX, _AES_RCON
    if _AES_SBOX:
        return

    def xtime(a):
        a <<= 1
        return ((a ^ 0x1B) & 0xFF) if a & 0x100 else a

    def mul(a, b):
        result = 0
        while b:
            if b & 1:
                result ^= a
            a = xtime(a)
            b >>= 1
        return result & 0xFF

    # S-box: multiplicative inverse in GF(2^8) + affine transform.
    sbox = [0] * 256
    inverse_sbox = [0] * 256
    # log/antilog tables over the generator 0x03
    log_table = [0] * 256
    alog_table = [0] * 256
    value = 1
    for exponent in range(255):
        log_table[value] = exponent
        alog_table[exponent] = value
        value ^= xtime(value)  # multiply by generator 3: v*3 = v ^ (v*2)
    log_table[0] = None
    for value in range(256):
        if value == 0:
            inverse = 0
        else:
            inverse = alog_table[(255 - log_table[value]) % 255]
        transformed = inverse
        result = inverse
        for _ in range(4):
            result = ((result << 1) | (result >> 7)) & 0xFF
            transformed ^= result
        sbox[value] = transformed ^ 0x63
    for value, transformed in enumerate(sbox):
        inverse_sbox[transformed] = value
    rcon = [0x01]
    for _ in range(9):
        rcon.append(xtime(rcon[-1]))
    _AES_SBOX[:] = sbox
    _AES_INV_SBOX[:] = inverse_sbox
    _AES_RCON[:] = rcon


def _aes_expand_key(key):
    """Expand a 16/24/32-byte key into 4-byte round-key words."""
    _aes_init_tables()
    nk = len(key) // 4
    words = [list(key[i:i + 4]) for i in range(0, len(key), 4)]
    rounds = {16: 10, 24: 12, 32: 14}[len(key)]
    total = 4 * (rounds + 1)
    rcon_index = 0
    while len(words) < total:
        temp = list(words[-1])
        if len(words) % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_AES_SBOX[b] for b in temp]
            temp[0] ^= _AES_RCON[rcon_index]
            rcon_index += 1
        elif len(key) == 32 and len(words) % 4 == 0:
            temp = [_AES_SBOX[b] for b in temp]
        base = list(words[-nk])
        words.append([base[j] ^ temp[j] for j in range(4)])
    return words, rounds


def _aes_rounds(state, words, rounds, decrypt=False):
    """Run the AES round function over a 4x4 state (state[r][c] = in[4c+r])."""
    _aes_init_tables()

    def add_round_key(current, round_index):
        for c in range(4):
            for r in range(4):
                current[r][c] ^= words[round_index * 4 + c][r]

    def sub_bytes(current, inverse=False):
        box = _AES_INV_SBOX if inverse else _AES_SBOX
        for r in range(4):
            for c in range(4):
                current[r][c] = box[current[r][c]]

    def shift_rows(current, inverse=False):
        for r in range(1, 4):
            row = current[r]
            if not inverse:
                current[r] = row[r:] + row[:r]
            else:
                current[r] = row[-r:] + row[:-r]

    def xtime(v):
        v <<= 1
        return ((v ^ 0x1B) & 0xFF) if v & 0x100 else v

    def mul(v, m):
        result = 0
        while m:
            if m & 1:
                result ^= v
            v = xtime(v)
            m >>= 1
        return result & 0xFF

    def mix_columns(current, inverse=False):
        for c in range(4):
            a = [current[r][c] for r in range(4)]
            if not inverse:
                current[0][c] = mul(a[0], 2) ^ mul(a[1], 3) ^ a[2] ^ a[3]
                current[1][c] = a[0] ^ mul(a[1], 2) ^ mul(a[2], 3) ^ a[3]
                current[2][c] = a[0] ^ a[1] ^ mul(a[2], 2) ^ mul(a[3], 3)
                current[3][c] = mul(a[0], 3) ^ a[1] ^ a[2] ^ mul(a[3], 2)
            else:
                current[0][c] = (mul(a[0], 14) ^ mul(a[1], 11) ^
                                 mul(a[2], 13) ^ mul(a[3], 9))
                current[1][c] = (mul(a[0], 9) ^ mul(a[1], 14) ^
                                 mul(a[2], 11) ^ mul(a[3], 13))
                current[2][c] = (mul(a[0], 13) ^ mul(a[1], 9) ^
                                 mul(a[2], 14) ^ mul(a[3], 11))
                current[3][c] = (mul(a[0], 11) ^ mul(a[1], 13) ^
                                 mul(a[2], 9) ^ mul(a[3], 14))

    if not decrypt:
        add_round_key(state, 0)
        for round_index in range(1, rounds):
            sub_bytes(state)
            shift_rows(state)
            mix_columns(state)
            add_round_key(state, round_index)
        sub_bytes(state)
        shift_rows(state)
        add_round_key(state, rounds)
    else:
        add_round_key(state, rounds)
        for round_index in range(rounds - 1, 0, -1):
            shift_rows(state, inverse=True)
            sub_bytes(state, inverse=True)
            add_round_key(state, round_index)
            mix_columns(state, inverse=True)
        shift_rows(state, inverse=True)
        sub_bytes(state, inverse=True)
        add_round_key(state, 0)
    return state


def _state_from_block(block):
    """Load one 16-byte block into FIPS-197 state layout (columns first)."""
    return [[block[4 * c + r] for c in range(4)] for r in range(4)]


def _state_to_block(state):
    out = bytearray(16)
    for c in range(4):
        for r in range(4):
            out[4 * c + r] = state[r][c]
    return bytes(out)


def _aes_decrypt_block(block, words, rounds):
    """Decrypt one 16-byte block with AES."""
    return _state_to_block(
        _aes_rounds(_state_from_block(block), words, rounds, decrypt=True))


def _evp_bytes_to_key(password, salt, key_len=32, iv_len=16):
    """OpenSSL EVP_BytesToKey (MD5) as used by CryptoJS."""
    derived = b""
    previous = b""
    while len(derived) < key_len + iv_len:
        previous = hashlib.md5(previous + password + salt).digest()
        derived += previous
    return derived[:key_len], derived[key_len:key_len + iv_len]


def _pkcs7_unpad(data):
    if not data or len(data) % 16:
        return None
    pad = data[-1]
    if not 1 <= pad <= 16 or data[-pad:] != bytes([pad]) * pad:
        return None
    return data[:-pad]


def cryptojs_decrypt(b64_text, passphrase=""):
    """Decrypt a CryptoJS.AES.encrypt Base64 blob; return "" on any mismatch."""
    try:
        raw = base64.b64decode("".join(str(b64_text or "").split()), validate=False)
    except Exception:
        return ""
    if len(raw) < 32 or raw[:8] != b"Salted__":
        return ""
    salt = raw[8:16]
    ciphertext = raw[16:]
    if not ciphertext or len(ciphertext) % 16:
        return ""
    key, iv = _evp_bytes_to_key(str(passphrase).encode("utf-8"), salt)
    words, rounds = _aes_expand_key(key)
    plain = b""
    previous = iv
    for offset in range(0, len(ciphertext), 16):
        block = ciphertext[offset:offset + 16]
        decrypted = _aes_decrypt_block(block, words, rounds)
        plain += bytes(a ^ b for a, b in zip(decrypted, previous))
        previous = block
    plain = _pkcs7_unpad(plain)
    if plain is None:
        return ""
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return ""


def _find_browser_storage_paths():
    """Return candidate browser profile directories for the current platform."""
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

        # Calibre's own QtWebEngine data: the embedded login window persists
        # cookies/localStorage under the "tolino-cloud-sync-login" profile,
        # so its storage is also a valid harvest source.
        calibre_data = os.environ.get(
            "XDG_DATA_HOME", os.path.join(home, ".local", "share"))
        add(os.path.join(calibre_data, "calibre", "webengine",
                         "tolino-cloud-sync-login"))
        add(os.path.join(calibre_data, "calibre", "webengine",
                         "QtWebEngine", "tolino-cloud-sync-login"))

    return [os.path.normpath(p) for p in paths]


# Origins (scheme+host, no trailing path) that may hold Tolino credentials.
TOLINO_STORAGE_ORIGINS = (
    "webreader.mytolino.com",
    "pageplace.de",
    "orellfuessli.ch",
    "thalia.de", "thalia.at",
    "buch.de", "books.ch",
    "hugendubel.de", "osiander.de", "buecher.de", "ebook.de",
    "buchhaus.ch", "wolters-mauritz.de", "book-club-family.de",
    "delibur.com", "thienemueller.de", "ohlala.ch",
    "keycloak", "auth",  # generic IdP path hints
)


def _origin_matches(host_text):
    text = str(host_text).casefold()
    return any(origin in text for origin in TOLINO_STORAGE_ORIGINS)


# --- Chromium LevelDB: Roh-Leser ohne Abhaengigkeiten ---------------------
# localStorage liegt in LevelDB (Keys <origin>\x00\x01<key>); wir
# scannen die Rohbytes nach Tolino-Origins -- robust ueber alle
# Record-Codierungen hinweg.

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
    """Yield record payloads from LevelDB log physical blocks."""
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
    """Yield (key, value) pairs from one WriteBatch payload."""
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
    """Yield (key, value) pairs from an .ldb/.sst table file."""
    try:
        blocks = _leveldb_ldb_blocks(data)
    except Exception:
        blocks = []
    for block in blocks:
        for key, value in _leveldb_ldb_pairs(block):
            yield key, value
    # Fallback for uncompressed or exotic files: raw marker scan.
    if blocks:
        return
    yield from _leveldb_records_from_ldb_raw(data)


def _leveldb_records_from_ldb_raw(data):
    """Naive scan for marker-bearing records in an uncompressed table."""
    marker = _TOLINO_ORIGIN_MARKER
    pos = 0
    n = len(data)
    while True:
        idx = data.find(marker, pos)
        if idx < 0:
            return
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
        yield key, data[value_start:value_end]
        pos = idx + len(marker)


_SNAPPY_LITERAL = 0
_SNAPPY_COPY1 = 1
_SNAPPY_COPY2 = 2
_SNAPPY_COPY4 = 3


def _snappy_uncompress(data):
    """Minimal Snappy decompressor (no dependencies). Returns b"" on error."""
    try:
        pos = 0
        out = bytearray()
        total, pos = _varint(data, pos)
        if total is None or total > 64 * 1024 * 1024:
            return b""
        n = len(data)
        while pos < n and len(out) < total:
            tag = data[pos]
            kind = tag & 0x03
            if kind == _SNAPPY_LITERAL:
                length = (tag >> 2) + 1
                pos += 1
                if length > 60:
                    extra = length - 60
                    if pos + extra > n:
                        return b""
                    length = int.from_bytes(data[pos:pos + extra], "little") + 1
                    pos += extra
                if pos + length > n:
                    return b""
                out += data[pos:pos + length]
                pos += length
            else:
                if kind == _SNAPPY_COPY1:
                    if pos + 1 >= n:
                        return b""
                    offset = ((tag >> 2) & 0x07) << 8 | data[pos + 1]
                    length = (tag >> 5) + 4
                    pos += 2
                elif kind == _SNAPPY_COPY2:
                    if pos + 3 >= n:
                        return b""
                    offset = int.from_bytes(data[pos + 1:pos + 3], "little")
                    length = (tag >> 2) & 0x3F
                    pos += 3
                else:  # COPY4
                    if pos + 4 >= n:
                        return b""
                    offset = int.from_bytes(data[pos + 1:pos + 5], "little")
                    length = (tag >> 2) & 0x3F
                    pos += 5
                if offset == 0 or offset > len(out):
                    return b""
                start = len(out) - offset
                for _ in range(length):
                    out.append(out[start])
                    start += 1
        return bytes(out) if len(out) == total else b""
    except Exception:
        return b""


def _leveldb_ldb_blocks(data):
    """Return decompressed data blocks of one .ldb table (footer-based)."""
    if len(data) < 48:
        return []
    footer = data[-48:]
    magic = struct.unpack_from("<Q", footer, 40)[0]
    if magic != 0xDB4775248B80FB57:
        return []
    meta_offset, meta_size = struct.unpack_from("<II", footer, 0)
    index_offset, index_size = struct.unpack_from("<II", footer, 8)
    if index_offset + index_size > len(data) - 48:
        return []
    index = _snappy_uncompress(
        data[index_offset:index_offset + index_size])
    if not index:
        index = data[index_offset:index_offset + index_size]
    # Index entries: shared(varint) non_shared(varint) value_len(varint)
    # key(non_shared bytes) + block_handle(offset varint, size varint).
    blocks = []
    pos = 0
    n = len(index)
    while pos < n:
        shared, pos = _varint(index, pos)
        non_shared, pos = _varint(index, pos)
        value_len, pos = _varint(index, pos)
        if shared is None or non_shared is None or value_len is None:
            break
        pos += non_shared  # skip separator key
        handle_offset, pos = _varint(index, pos)
        handle_size, pos = _varint(index, pos)
        if handle_offset is None or handle_size is None:
            break
        block_data = data[handle_offset:handle_offset + handle_size]
        if len(block_data) < 5:
            continue
        body = block_data[:-5]  # 1 type byte + 4 crc
        ctype = block_data[-5]
        if ctype == 0:
            blocks.append(body)
        elif ctype == 1:
            decompressed = _snappy_uncompress(body)
            if decompressed:
                blocks.append(decompressed)
    return blocks


def _leveldb_ldb_pairs(block):
    """Yield (key, value) pairs from one decompressed .ldb data block."""
    pos = 0
    n = len(block)
    last_key = b""
    while pos < n:
        start = pos
        shared, pos = _varint(block, pos)
        non_shared, pos = _varint(block, pos)
        value_len, pos = _varint(block, pos)
        if (shared is None or non_shared is None or value_len is None
                or pos + non_shared + value_len > n
                or shared > len(last_key)):
            # Not a valid entry (restart point / padding) – stop this block.
            break
        key = last_key[:shared] + block[pos:pos + non_shared]
        pos += non_shared
        value = block[pos:pos + value_len]
        pos += value_len
        last_key = key
        if _TOLINO_ORIGIN_MARKER in key:
            yield key, value
        elif start == pos:  # safety: never loop forever
            break


_TOLINO_ORIGIN_MARKER = b"webreader.mytolino.com"
# Byte markers of every origin whose storage may hold reader credentials:
# used for the cheap file-level prefilter when scanning LevelDB directories.
_TOLINO_STORAGE_MARKERS = tuple(
    origin.encode("utf-8") for origin in TOLINO_STORAGE_ORIGINS
    if origin not in ("keycloak", "auth") and "." in origin
)


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


def _scan_chromium_leveldb(db_dir, recency_hint=None):
    """Scan a Chromium Local/Session Storage LevelDB directory for Tolino keys."""
    found = {}
    for file_name in sorted(os.listdir(db_dir)):
        if not file_name.endswith((".log", ".ldb")):
            continue
        if recency_hint == _RECENCY_LIVE and not file_name.endswith(".log"):
            continue
        file_path = os.path.join(db_dir, file_name)
        try:
            with open(file_path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        # Session Storage keys carry no origin, so also accept files that
        # contain any known Tolino origin in UTF-16LE (IndexedDB-style
        # encoding) before giving up on the file.
        if not any(marker in data for marker in _TOLINO_STORAGE_MARKERS):
            utf16_markers = tuple(
                marker.decode("utf-8").encode("utf-16-le")
                for marker in _TOLINO_STORAGE_MARKERS)
            if not any(marker in data for marker in utf16_markers):
                continue
        if file_name.endswith(".log"):
            iterator = _leveldb_records_from_log(data)
            tier = _RECENCY_LIVE if recency_hint == _RECENCY_LIVE else _RECENCY_LDB
        else:
            iterator = _leveldb_records_from_ldb(data)
            tier = _RECENCY_LDB
        for key, value in iterator:
            key_text = _decode_leveldb_text(key)
            value_text = _leveldb_value_text(value)
            if not value_text:
                continue
            is_session = "session storage" in db_dir.casefold()
            if not _origin_matches(key_text) and not (
                    is_session and _leveldb_pair_has_token_shape(key_text,
                                                                  value_text)):
                continue
            # Drop the scheme prefix and the 0x00/0x01 separator from the key
            storage_key = key_text
            for prefix in ("_https://", "https://", "_http://", "http://"):
                if storage_key.casefold().startswith(prefix.casefold()):
                    storage_key = storage_key[len(prefix):]
                    break
            found[storage_key.lstrip("\x00\x01\x02")] = value_text
            _RECENCY_BUCKETS[value_text] = tier
    return found


# --- Firefox (LSNG): webappsstore.sqlite ----------------------------------


def _sqlite_snapshot_copy(db_path):
    """Copy db + WAL/journal next to it so a live Firefox can be read."""
    directory = tempfile.mkdtemp(prefix="tolino-scan-")
    try:
        target = os.path.join(directory, os.path.basename(db_path))
        shutil.copy2(db_path, target)
        for suffix in ("-wal", "-shm", "-journal"):
            side = db_path + suffix
            if os.path.exists(side):
                try:
                    shutil.copy2(side, target + suffix)
                except OSError:
                    pass
        return target
    except OSError:
        shutil.rmtree(directory, ignore_errors=True)
        return None


def _read_sqlite_snapshot(db_path, sql, params=()):
    """Run one query against a snapshot copy; returns rows or []."""
    snapshot = _sqlite_snapshot_copy(db_path)
    if snapshot is None:
        return []
    try:
        conn = sqlite3.connect(snapshot)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()
    except Exception:
        return []
    finally:
        shutil.rmtree(snapshot and os.path.dirname(snapshot),
                      ignore_errors=True)


def _lsng_value_text(value, conversion=0, compression=0):
    """Decode one LSNG value from its conversion/compression metadata."""
    if isinstance(value, str):
        return value
    if value is None:
        return None
    blob = bytes(value)
    if not blob:
        return ""
    if compression not in (0, 1, None):
        compression = 0
    if compression == 1:
        blob = _snappy_uncompress(blob)
        if not blob:
            return ""

    def utf16():
        try:
            return blob.decode("utf-16-le")
        except UnicodeDecodeError:
            return ""

    def utf8():
        try:
            return blob.decode("utf-8")
        except UnicodeDecodeError:
            return blob.decode("utf-8", "replace")

    if conversion == 1:  # UTF16_UTF8: bytes are UTF-8 text
        return utf8()
    if conversion == 0:  # NONE: bytes are raw UTF-16LE code units
        text = utf16()
        if text:
            return text
        return utf8()
    # Unknown conversion: sniff - BLOBs with NUL padding are UTF-16LE.
    if len(blob) >= 4 and blob[1] == 0 and blob[3] == 0:
        return utf16() or utf8()
    return utf8() or utf16()


def _read_firefox_rows(db_path, sql):
    """Run one query via snapshot copy, falling back to immutable open."""
    rows = _read_sqlite_snapshot(db_path, sql)
    if rows:
        return rows
    try:
        conn = sqlite3.connect("file:%s?immutable=1" % db_path, uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def _read_firefox_data_sqlite(db_path):
    """Read one LSNG per-origin data.sqlite / ls-archive.sqlite database."""
    results = {}
    rows = _read_firefox_rows(
        db_path,
        "SELECT key, value, conversion_type, compression_type FROM data")
    for key, value, conversion, compression in rows:
        if not isinstance(key, str):
            key = str(key)
        results[key] = _lsng_value_text(value, conversion or 0,
                                        compression or 0)
    return results


def _read_firefox_storage(profile_path):
    """Read localStorage from a Firefox profile across all known layouts."""
    results = {}
    legacy = {}
    # 1) LSNG per-origin data.sqlite files anywhere under this profile.
    for root, dirs, files in os.walk(profile_path):
        dirs[:] = [d for d in dirs if d not in (
            "cache2", "Cache", "Cache2", "startupCache", "thumbnails",
            "shader-cache", "Sanitizes", "crashes", "minidumps")]
        if "data.sqlite" in files:
            db_path = os.path.join(root, "data.sqlite")
            data = _read_firefox_data_sqlite(db_path)
            if data:
                origin_hint = os.path.basename(os.path.dirname(root))
                for key, value in data.items():
                    results["%s/%s" % (origin_hint, key)] = value
                    _RECENCY_BUCKETS[value] = _RECENCY_LIVE
    # 2) ls-archive.sqlite cold store (storage root may be profile itself).
    for name in ("ls-archive.sqlite", os.path.join("storage", "ls-archive.sqlite")):
        db_path = os.path.join(profile_path, name)
        if os.path.exists(db_path):
            rows = _read_firefox_rows(
                db_path,
                "SELECT originAttributes, originKey, key, value, "
                "conversion_type, compression_type FROM data")
            for origin_attrs, origin_key, key, value, conversion, compression in rows:
                origin_text = _lsng_value_text(origin_key)
                if not isinstance(key, str):
                    key = str(key)
                value = _lsng_value_text(value, conversion or 0,
                                         compression or 0)
                results["%s/%s" % (origin_text, key)] = value
                _RECENCY_BUCKETS[value] = _RECENCY_LDB
    # 3) Legacy/shadow webappsstore.sqlite (webappsstore2 schema). This copy
    # is only written while Firefox is fully closed, so it holds STALE token
    # history. Collect it separately and never let it overwrite fresher
    # LSNG rows for the same storage key.
    db_path = os.path.join(profile_path, "webappsstore.sqlite")
    if os.path.exists(db_path):
        rows = _read_firefox_rows(
            db_path,
            "SELECT originAttributes, originKey, scope, key, value "
            "FROM webappsstore2")
        for _attrs, origin_key, _scope, key, value in rows:
            origin_text = _lsng_value_text(origin_key)
            if not isinstance(key, str):
                key = str(key)
            value = _lsng_value_text(value)
            legacy["%s/%s" % (origin_text, key)] = value
            _RECENCY_BUCKETS[value] = _RECENCY_LEGACY
    for key, value in legacy.items():
        results.setdefault(key, value)
    return results


def _lz4_block_decompress(data, expected_size=0):
    """Decode one raw LZ4 block (pure Python); return b"" on malformed input."""
    out = bytearray()
    i = 0
    n = len(data)
    while i < n:
        token = data[i]
        i += 1
        literal_len = token >> 4
        if literal_len == 15:
            while i < n:
                extra = data[i]
                i += 1
                literal_len += extra
                if extra != 255:
                    break
        if i + literal_len > n:
            return b""
        out += data[i:i + literal_len]
        i += literal_len
        if i >= n:
            break
        if i + 2 > n:
            return b""
        offset = data[i] | (data[i + 1] << 8)
        i += 2
        if offset == 0 or offset > len(out):
            return b""
        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while i < n:
                extra = data[i]
                i += 1
                match_len += extra
                if extra != 255:
                    break
        start = len(out) - offset
        for j in range(match_len):
            out.append(out[start + j])
        if expected_size and len(out) >= expected_size:
            break
    if expected_size:
        return bytes(out[:expected_size])
    return bytes(out)


def _read_mozlz4(path):
    """Read a Mozilla mozLZ4 file (magic mozLz40\0) as bytes; None otherwise."""
    try:
        with open(path, "rb") as handle:
            blob = handle.read()
    except OSError:
        return None
    if blob[:8] != b"mozLz40\x00" or len(blob) < 12:
        return None
    size = int.from_bytes(blob[8:12], "little")
    if size <= 0 or size > 64 * 1024 * 1024:
        return None
    return _lz4_block_decompress(blob[12:], size)


def _harvest_sessionstore_origins(node, results):
    """Collect "origin/key" pairs from a sessionstore JSON tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            if (isinstance(key, str) and _origin_matches(key)
                    and isinstance(value, dict)):
                for name, item in value.items():
                    if isinstance(item, str):
                        text = item
                    elif isinstance(item, (dict, list)):
                        text = json.dumps(item, ensure_ascii=False)
                    elif item is None:
                        text = ""
                    else:
                        text = str(item)
                    if text:
                        results["%s/%s" % (key, name)] = text
            else:
                _harvest_sessionstore_origins(value, results)
    elif isinstance(node, list):
        for item in node:
            _harvest_sessionstore_origins(item, results)


def _read_firefox_session_storage(profile_path):
    """Best-effort read of DOM sessionStorage from Firefox sessionstore files."""
    results = {}
    for rel in ("sessionstore-backups/recovery.jsonlz4",
                "sessionstore-backups/previous.jsonlz4",
                "sessionstore-backups/recovery.baklz4",
                "sessionstore.jsonlz4"):
        path = os.path.join(profile_path, rel)
        if not os.path.exists(path):
            continue
        raw = _read_mozlz4(path)
        if not raw:
            continue
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        _harvest_sessionstore_origins(payload, results)
    return results


def _read_firefox_cookies(profile_path):
    """Read Tolino-relevant cookie names/values from cookies.sqlite."""
    results = {}
    db_path = os.path.join(profile_path, "cookies.sqlite")
    if not os.path.exists(db_path):
        return results
    for row in _read_sqlite_snapshot(
            db_path, "SELECT host, name, value FROM moz_cookies"):
        host, name, value = (row + (None, None, None))[:3]
        if not host or not name:
            continue
        if not _origin_matches(host):
            continue
        results["cookie:%s/%s" % (host, name)] = _lsng_value_text(value) or ""
    return results


def _scan_chromium_indexeddb(idb_dir, notes):
    """Scan a Chromium IndexedDB level directory for Tolino records."""
    found = {}
    utf16_marker = _TOLINO_ORIGIN_MARKER.encode("utf-16-le")
    for file_name in sorted(os.listdir(idb_dir)):
        if not file_name.endswith((".log", ".ldb")):
            continue
        file_path = os.path.join(idb_dir, file_name)
        try:
            with open(file_path, "rb") as handle:
                data = handle.read()
        except OSError:
            continue
        if _TOLINO_ORIGIN_MARKER not in data and utf16_marker not in data:
            continue
        if file_name.endswith(".log"):
            iterator = _leveldb_records_from_log(data)
        else:
            iterator = _leveldb_records_from_ldb(data)
        for key, value in iterator:
            if (_TOLINO_ORIGIN_MARKER not in key
                    and utf16_marker not in key
                    and _TOLINO_ORIGIN_MARKER not in value
                    and utf16_marker not in value):
                continue
            text = _decode_leveldb_text(key)
            if not text and value:
                text = _decode_leveldb_text(value[:64])
            value_text = _leveldb_value_text(value) or _decode_leveldb_text(value)
            if value_text:
                found["idb:%s/%s" % (text[:120], idb_dir[-40:])] = value_text
    if found:
        notes.append("Chromium-IndexedDB: %s" % idb_dir)
    return found


def _diagnose_storage_keys(storage):
    """Redacted key overview from one storage snapshot (no values)."""
    keys = []
    for key in sorted(storage, key=str):
        text = str(key)
        if _origin_matches(text):
            keys.append("%s = <wert geschwärzt>" % text)
    return keys


def _collect_storage_under(path, notes):
    """Walk a candidate root and harvest any Tolino storage entries found."""
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
                    _CHROMIUM_STORAGE_DIRS.append(root)
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
                    found.update(_read_firefox_cookies(root))
            # Chromium IndexedDB: origin dirs like
            # https_webreader.mytolino.com_0.indexeddb.leveldb (may also be
            # UTF-16LE encoded on disk); scan every .leveldb below.
            if os.path.basename(root) == "IndexedDB":
                try:
                    entries = sorted(os.listdir(root))
                except OSError:
                    entries = []
                for entry in entries:
                    try:
                        decoded = entry.encode("latin-1", "ignore").decode("utf-16-le", "ignore")
                    except Exception:
                        decoded = ""
                    if _origin_matches(entry) or _origin_matches(decoded):
                        inner = os.path.join(root, entry)
                        if os.path.isdir(inner):
                            for sub in sorted(os.listdir(inner)):
                                if sub.endswith(".leveldb"):
                                    found.update(_scan_chromium_indexeddb(
                                        os.path.join(inner, sub), notes))
            # Skip heavy noise dirs that never hold web storage
            dirs[:] = [d for d in dirs if d not in (
                "Cache", "Cache2", "Code Cache", "GPUCache", "DawnCache",
                "GrShaderCache", "ShaderCache", "Service Worker", "blob_storage",
                "Sessions", "Crashpad", "thumbnails")]
    except OSError:
        return found
    return found


def _extract_tokens_from_storage(storage_data):
    """Extract refresh_token and hardware_id from a storage snapshot."""
    refresh_token = None
    hardware_id = None

    def value_text(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", "replace")
        return str(value)

    def plaintexts(text):
        """Yield decrypted plaintexts first, then the raw value itself."""
        if "Salted__" in text or (text and text.startswith("U2FsdGVk")):
            for phrase in READER_AES_PHRASES:
                decrypted = cryptojs_decrypt(text, phrase)
                if decrypted:
                    yield decrypted
        yield text

    def usable(text, mode="refresh"):
        """Pick the best credential text among raw and decrypted candidates."""
        names = TOKEN_VALUE_KEYS if mode == "refresh" else HARDWARE_VALUE_KEYS
        fallback = None
        for plain in plaintexts(text):
            plain = (plain or "").strip()
            if not plain:
                continue
            try:
                parsed = json.loads(plain)
            except ValueError:
                if fallback is None:
                    fallback = plain
                continue
            if not isinstance(parsed, dict):
                continue
            for name in names:
                nested = parsed.get(name)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
            # userToken: {"refresh": "<CryptoJS b64>", "expireTime": ...}
            if mode == "refresh":
                nested = parsed.get("refresh")
                if isinstance(nested, str) and nested.strip():
                    for plain_inner in plaintexts(nested):
                        plain_inner = (plain_inner or "").strip()
                        if plain_inner and not plain_inner.startswith("U2FsdGVk"):
                            return plain_inner
            if fallback is None:
                fallback = plain
        return fallback

    def first_candidate(value, mode="refresh"):
        try:
            text = usable(value_text(value), mode)
        except Exception:
            return None
        if not text:
            return None
        text = text.strip()
        # Never hand back undecrypted JSON bundles or ciphertext.
        if text.startswith("{") or text.startswith("U2FsdGVk"):
            return None
        return text

    for key, value in storage_data.items():
        key_lower = str(key).casefold()
        if refresh_token is None:
            if any(name in key_lower for name in ("refresh", "t_auth",
                                                 "usertoken")):
                refresh_token = first_candidate(value)
        if hardware_id is None:
            if any(name in key_lower for name in ("hardware", "device",
                                                 "userinfos")):
                hardware_id = first_candidate(value, "hardware")

    if refresh_token is None:
        for value in storage_data.values():
            candidate = first_candidate(value)
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

# Passphrases the web reader used for its CryptoJS blobs (VERSION.PHRASE in
# src/config.json). Currently an empty string; keep candidates so a future
# change on the reader side keeps the extractor working.
READER_AES_PHRASES = ("",)


# --- Kandidaten-Sortierung --------------------------------------------------
# Storage haelt alle historischen Kopien; Scan-Reihenfolge ist nicht die
# Aktualitaet. Der aktuelle Token steht im juengsten Write (Chromium:
# write-ahead .log, Firefox: LSNG-WAL) -- dort wird zuerst gesucht.

_RECENCY_LIVE = 0    # newest write: Chromium .log / Firefox LSNG WAL
_RECENCY_LDB = 1     # Chromium .ldb / any Firefox row
_RECENCY_LEGACY = 2  # legacy/shadow stores only (old Firefox webappsstore)

_RECENCY_BUCKETS = {}

# Chromium storage directories discovered during a scan; a second, live-only
# pass reads their newest records (write-ahead .log) so the reader's current
# token beats stale history in candidate ordering.
_CHROMIUM_STORAGE_DIRS = []


def _recency_tier(value):
    """Recency tier recorded for a harvested value (unknown -> .ldb tier)."""
    return _RECENCY_BUCKETS.get(str(value), _RECENCY_LDB)


def _sort_candidates_by_recency(candidates):
    """Return candidates sorted freshest-first, deduplicated, order kept."""
    ordered = []
    for candidate in candidates:
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    def _rank(value):
        issued = _refresh_token_iat(value)
        if issued is not None:
            # Newest token first; negate so bigger iat sorts first.
            return (0, -issued, _recency_tier(value))
        return (1, 0, _recency_tier(value))

    return sorted(ordered, key=_rank)


def _sort_hardware_candidates(candidates):
    """Hardware IDs: prefer UUID-shaped ones, then freshest first."""
    ordered = []
    for candidate in candidates:
        if candidate and candidate not in ordered:
            ordered.append(candidate)

    def _uuid_rank(value):
        text = str(value)
        if re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text):
            return 0
        if re.fullmatch(r"[0-9a-fA-F]{32}", text):
            return 1
        return 2

    return sorted(
        ordered,
        key=lambda value: (_uuid_rank(value), _recency_tier(value)),
    )


def _jwt_payload(token):
    """Decode the unverified payload of a JWT-style token (or None)."""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3 or not parts[1]:
            return None
        seg = parts[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg.encode("ascii")))
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def _refresh_token_iat(token):
    """Issue time (unix seconds) of a JWT refresh token; None if unknown."""
    payload = _jwt_payload(token)
    if not payload:
        return None
    try:
        value = payload.get("iat")
        value = float(value)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return value


def _jwt_shaped(text):
    """True when the text itself looks like a JWT (three base64url parts)."""
    text = str(text or "")
    parts = text.split(".")
    if len(parts) != 3 or len(text) < 80:
        return False
    try:
        return all(part and re.fullmatch(r"[A-Za-z0-9_\-]+", part)
                   for part in parts)
    except TypeError:
        return False


def _candidate_age_text(token, now=None):
    """Human-readable age of a JWT refresh token ('gerade geschrieben')."""
    issued = _refresh_token_iat(token)
    if issued is None:
        return "Alter unbekannt (kein JWT)"
    if now is None:
        now = time.time()
    minutes = int(max(0, now - issued) // 60)
    if minutes <= 0:
        return "gerade geschrieben"
    if minutes < 60:
        return "vor %d min" % minutes
    hours = minutes // 60
    if hours < 24:
        return "vor %d h %d min" % (hours, minutes % 60)
    return "vor %d Tagen" % (hours // 24)


def _keycloak_verdict(text):
    """True when an error message carries a definitive Keycloak rejection."""
    folded = str(text).casefold()
    return ("invalid_grant" in folded or "not active" in folded
            or "reuse exceeded" in folded
            or "reused or invalid" in folded)


def _jwt_claim(token, name):
    """One claim from a JWT refresh token's unencrypted payload; None if absent."""
    payload = _jwt_payload(token) or {}
    value = payload.get(name)
    return str(value) if value is not None else None


def _candidate_sid(token):
    """Keycloak session id ('sid' claim) of a refresh token; None if absent."""
    return _jwt_claim(token, "sid")


def _select_candidate_round(refreshes, seen_spent):
    """Pick the best untried candidates for one validation round."""
    by_sid = {}
    order = []
    for token in refreshes or ():
        if not token or token in seen_spent:
            continue
        sid = _candidate_sid(token)
        key = sid if sid is not None else ("token", token)
        if key not in by_sid:
            by_sid[key] = []
            order.append(key)
        by_sid[key].append(token)
    spent_sids = {sid for sid in (_candidate_sid(t) for t in seen_spent)
                  if sid is not None}
    picked = []
    for key in order:
        group = by_sid[key]
        if isinstance(key, tuple) and key[0] == "token":
            picked.append(group[0])
            continue
        if key in spent_sids:
            # A sibling of this session was definitively rejected: the
            # session is dead, replaying its newest token is pointless
            # and risks tripping the reuse protection again.
            continue
        picked.append(max(group, key=_refresh_token_iat_sort_key))
    return picked


def _validate_one(partner_id, hardware_id, candidate):
    """Validate one scraped refresh token against the token endpoint.

    Returns ("ok", rotated_refresh, hardware) when the endpoint accepted
    the grant, ("spent", None, None) after a definitive Keycloak rejection
    ("invalid_grant" / "Session not active": this token can never come
    back), and ("unclear", None, None) for network/protection/5xx errors
    that prove nothing about the token itself -- callers should retry
    those later instead of discarding a possibly-live token.
    """
    candidate = (candidate or "").strip()
    if not candidate:
        return ("spent", None, None)
    client = TolinoClient(partner_id, hardware_id or "")
    client.refresh = candidate
    try:
        client._login()
    except TolinoAuthError as exc:
        # Nur ein eindeutiger Keycloak-Verdacht markiert einen Kandidaten als
        # verbraucht -- Transportfehler duerfen niemals einen Live-Token brennen.
        if _keycloak_verdict(exc):
            return ("spent", None, None)
        return ("unclear", None, None)
    except (TolinoApiError, OSError, ValueError, TypeError) as exc:
        # Same rule for failures surfacing through other exception types:
        # a Keycloak verdict inside the message is definitive, everything
        # else stays retryable.
        if _keycloak_verdict(exc):
            return ("spent", None, None)
        return ("unclear", None, None)
    return ("ok", client.refresh or candidate,
            client.hardware or hardware_id)


def validate_refresh_candidates(partner_id, hardware_id, candidates):
    """Live-validate scraped refresh tokens; return the fresh pair or None."""
    for candidate in _select_candidate_round(candidates, set()):
        verdict, rotated, hw = _validate_one(partner_id, hardware_id,
                                             candidate)
        if verdict == "ok":
            return rotated, hw
    return None


def _extract_all_tokens_from_storage(storage_data, recency=_RECENCY_LDB):
    """Return (refresh_candidates, hardware_candidates) as ordered lists."""
    refresh_candidates = []
    hardware_candidates = []

    def remember(value, bucket):
        if value and value not in bucket:
            bucket.append(value)
            if value not in _RECENCY_BUCKETS:
                _RECENCY_BUCKETS[value] = recency

    def value_text(value):
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf-8", "replace")
        return str(value)

    def candidates_for(value, mode):
        names = TOKEN_VALUE_KEYS if mode == "refresh" else HARDWARE_VALUE_KEYS
        found = []
        try:
            text = value_text(value)
        except Exception:
            return found
        if "Salted__" in text or (text and text.startswith("U2FsdGVk")):
            for phrase in READER_AES_PHRASES:
                decrypted = cryptojs_decrypt(text, phrase)
                if decrypted and decrypted.strip():
                    found.append(decrypted.strip())
        found.append(text.strip())
        return found

    def shaped(value, mode, trusted):
        """Best non-ciphertext token text for a storage value (or None)."""
        names = TOKEN_VALUE_KEYS if mode == "refresh" else HARDWARE_VALUE_KEYS
        for plain in candidates_for(value, mode):
            if not plain:
                continue
            try:
                parsed = json.loads(plain)
            except ValueError:
                if trusted:
                    if not plain.startswith("{") and not plain.startswith("U2FsdGVk"):
                        return plain
                    continue
                if _jwt_shaped(plain):
                    return plain
                continue
            if not isinstance(parsed, dict):
                continue
            for name in names:
                nested = parsed.get(name)
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
            if mode == "refresh":
                nested = parsed.get("refresh")
                if isinstance(nested, str) and nested.strip():
                    for plain_inner in candidates_for(nested, mode):
                        if plain_inner and not plain_inner.startswith("U2FsdGVk"):
                            return plain_inner
        return None

    # Jeder Storage-Wert wird auf Credential-Formen geprueft, egal welcher
    # Schluessel: der Live-Token liegt im opaken "oidc.user:..."-Blob.
    for key, value in storage_data.items():
        key_lower = str(key).casefold()
        refresh_trusted = any(name in key_lower
                              for name in ("refresh", "t_auth", "usertoken"))
        hw_trusted = any(name in key_lower
                         for name in ("hardware", "device", "userinfos"))
        candidate = shaped(value, "refresh", refresh_trusted)
        if candidate:
            remember(candidate, refresh_candidates)
        hw_candidate = shaped(value, "hardware", hw_trusted)
        if hw_candidate:
            remember(hw_candidate, hardware_candidates)
    return refresh_candidates, hardware_candidates


def scrape_browser_tokens(diagnose=False, all_candidates=False):
    """Read Tolino refresh_token/hardware_id from installed browsers."""
    notes = []
    checked = []
    all_keys = {}
    all_refresh = []
    all_hardware = []
    # Fresh scan: never mix recency state or storage dirs from a previous
    # call into this one's candidate ordering.
    _RECENCY_BUCKETS.clear()
    del _CHROMIUM_STORAGE_DIRS[:]

    def remember_pair(storage):
        if all_candidates:
            # List mode: keep scanning every profile and merge everything.
            refreshes, hardwares = _extract_all_tokens_from_storage(storage)
            for value in refreshes:
                if value not in all_refresh:
                    all_refresh.append(value)
            for value in hardwares:
                if value not in all_hardware:
                    all_hardware.append(value)
            return None
        refresh_token, hardware_id = _extract_tokens_from_storage(storage)
        if refresh_token and hardware_id:
            return (refresh_token, hardware_id, notes) if diagnose \
                else (refresh_token, hardware_id)
        return None

    for path in _find_browser_storage_paths():
        exists = os.path.isdir(path)
        checked.append("%s%s" % (path, "" if exists else "  (fehlt)"))
        if not exists:
            continue
        storage = _collect_storage_under(path, notes)
        if storage:
            all_keys.update(storage)
            result = remember_pair(storage)
            if result is not None:
                return result
    # Second, live-only pass: read only the write-ahead .log of every
    # Chromium storage dir found. These hold the browser's newest writes,
    # so the reader's CURRENT token outranks compacted .ldb history even
    # when the first complete pair found in file order was already spent.
    live_storage = {}
    for db_dir in list(_CHROMIUM_STORAGE_DIRS):
        try:
            live_storage.update(_scan_chromium_leveldb(
                db_dir, recency_hint=_RECENCY_LIVE))
        except OSError:
            continue
    if live_storage:
        all_keys.update(live_storage)
        result = remember_pair(live_storage)
        if result is not None:
            return result
    if all_candidates:
        if not all_refresh or not all_hardware:
            # Tokens may be split across browser profiles; merge everything.
            refreshes, hardwares = _extract_all_tokens_from_storage(all_keys)
            for value in refreshes:
                if value not in all_refresh:
                    all_refresh.append(value)
            for value in hardwares:
                if value not in all_hardware:
                    all_hardware.append(value)
        if all_refresh or all_hardware:
            notes.append(
                "%d Refresh-Kandidat(en) und %d Hardware-Kandidat(en) "
                "gefunden; einer nach dem anderen wird jetzt gegen den "
                "Token-Endpunkt geprueft." % (len(all_refresh),
                                              len(all_hardware)))
            return (_sort_candidates_by_recency(all_refresh),
                    _sort_hardware_candidates(all_hardware), notes)
    else:
        # Tokens may be split across browser profiles; try the combined set.
        combined = _extract_tokens_from_storage(all_keys)
        if combined[0] and combined[1]:
            return (combined[0], combined[1], notes) if diagnose \
                else (combined[0], combined[1])
    if diagnose:
        if all_keys:
            keys = _diagnose_storage_keys(all_keys)
            if keys:
                notes.append("Tolino-Schlüssel gefunden (Werte geschwärzt):")
                notes.extend(keys)
                notes.append("-> Es fehlt ein Wert mit Token-/Hardware-Form; "
                             "Web Reader einmal vollständig laden.")
            else:
                notes.append("%d Storage-Quelle(n) gelesen, aber keine "
                             "Tolino-Origin-Einträge darin – im Web Reader "
                             "(Bibliothek) anmelden, dann erneut versuchen:" %
                             len(notes))
        elif checked:
            notes.append("Kein Chromium- (Local Storage/leveldb) oder "
                         "Firefox-Storage (webappsstore.sqlite) gefunden. "
                         "Geprüfte Orte:")
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
        raise TolinoAuthError("Browser-Anmeldung: ung\u00fcltige Callback-Daten.")
    values = query.get(name)
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        return values
    if isinstance(values, (tuple, list)):
        return next(iter(values), None)
    raise TolinoAuthError("Browser-Anmeldung: ung\u00fcltige %s-Daten." % name)


def hardware_id():
    """Return a fresh hardware ID in the web reader's UUID format."""
    return str(uuid.uuid4())


def normalize_hardware_id(value):
    """Bring a hardware ID into the web reader's 8-4-4-4-12 UUID shape."""
    text = str(value or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        formatted = "%s-%s-%s-%s-%s" % (
            compact[0:8], compact[8:12], compact[12:16],
            compact[16:20], compact[20:32])
        return formatted.lower()
    return text


def callback_redirect_uri(port):
    return "http://127.0.0.1:%d/callback" % int(port)


def validate_callback(query, expected_state, created_at, now=None):
    """Validate one OAuth callback without accepting tokens from the URL."""
    now = time.time() if now is None else now
    if now - created_at > OAUTH_STATE_TTL:
        raise TolinoAuthError("Browser-Anmeldung abgelaufen. Bitte erneut versuchen.")
    if _query_value(query, "state") != expected_state:
        raise TolinoAuthError("Browser-Anmeldung: Zustand stimmt nicht \u00fcberein. Bitte erneut versuchen.")
    if _query_value(query, "error"):
        raise TolinoAuthError("Die Browser-Anmeldung wurde vom Buchh\u00e4ndler abgelehnt.")
    code = _query_value(query, "code")
    if not code:
        raise TolinoAuthError("Browser-Anmeldung lieferte keinen Autorisierungscode.")
    return code


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.query = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Login received. You can return to Calibre.")

    def log_message(self, *_args):
        return


# Reseller, deren OAuth-Endpunkt keinen localhost-Redirect akzeptiert
# (reseller 8, Orell Fuessli): registriert sind nur Web-Reader-Redirects.
LOCAL_CALLBACK_UNSUPPORTED_RESELLERS = ("8",)


def _refresh_token_iat_sort_key(value):
    issued = _refresh_token_iat(value)
    return issued if issued is not None else 0.0


def _candidate_ages_summary(candidates, now=None):
    """Compact, redacted age overview for diagnostics ('2x vor 3 h ...')."""
    if not candidates:
        return "keine"
    ages = []
    for token in candidates:
        try:
            ages.append(_candidate_age_text(token, now))
        except Exception:
            ages.append("Alter unbekannt")
    counts = {}
    order = []
    for age in ages:
        if age not in counts:
            order.append(age)
            counts[age] = 0
        counts[age] += 1
    return ", ".join("%dx %s" % (counts[a], a) for a in order)


def _undatable_shape(token):
    """Structural fingerprint of an undatable candidate -- never its value."""
    text = str(token or "")
    parts = text.split(".")
    shape = "%d %s, %d Zeichen" % (
        len(parts), "Teil" if len(parts) == 1 else "Teile", len(text))
    if len(parts) == 3:
        shape += (", Payload ohne iat" if _jwt_payload(text)
                  else ", Payload unlesbar")
    return shape


def _candidate_age_note(candidates):
    """Redaktionsfreier Alters-Hinweis fuer Fehlermeldungen."""
    best = None
    for token in candidates or ():
        issued = _refresh_token_iat(token)
        if issued is not None and (best is None or issued > best):
            best = issued
    if best is None:
        # candidates kann eine Menge (tried) sein -- nie indexieren.
        token = next(iter(candidates or ()), None)
        if token is None:
            return ""
        return ("Refresh-Kandidat ohne datierbares JWT-alter (%s)"
                % _undatable_shape(token))
    return "Refresh-Kandidat %d Sekunden alt" % max(
        0, int(time.time() - best))


def _filter_live_candidates(candidates, max_age_seconds=1800):
    """Keep only JWT candidates issued within the last minutes."""
    now = time.time()
    kept = []
    for token in candidates or ():
        issued = _refresh_token_iat(token)
        if issued is None or now - issued <= max_age_seconds:
            kept.append(token)
    return kept


def _peel_candidate_wrappers(value):
    """One unwrapping step for a stored candidate; returns peel results."""
    out = []
    stripped = value.strip()
    if (len(stripped) >= 2 and stripped.startswith('"')
            and stripped.endswith('"')):
        try:
            unquoted = json.loads(stripped)
        except ValueError:
            unquoted = None
        if isinstance(unquoted, str) and unquoted:
            out.append(unquoted)
    if "%" in value:
        decoded = _unquote(value)
        if decoded and decoded != value:
            out.append(decoded)
    compact = "".join(value.split())
    if len(compact) >= 24 and re.fullmatch(
            r"[A-Za-z0-9+/\-_]+={0,2}", compact):
        padded = compact + "=" * (-len(compact) % 4)
        try:
            decoded = base64.urlsafe_b64decode(
                padded.encode("ascii")).decode("utf-8")
        except Exception:
            decoded = ""
        if decoded:
            out.append(decoded)
    return out


def _decrypt_reader_blob(value):
    """Plaintexts of a CryptoJS "Salted__" reader blob ([] when none)."""
    text = str(value or "")
    if not text or ("Salted__" not in text
                    and not text.lstrip().startswith("U2FsdGVk")):
        return []
    out = []
    for phrase in READER_AES_PHRASES:
        try:
            plain = cryptojs_decrypt(text, phrase)
        except Exception:
            plain = ""
        plain = (plain or "").strip()
        if plain and plain not in out:
            out.append(plain)
    return out


def _refresh_field_text(json_text):
    """The refresh-ish string field of a decrypted reader JSON bundle."""
    try:
        parsed = json.loads(json_text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    for name in ("refresh",) + TOKEN_VALUE_KEYS:
        nested = parsed.get(name)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return None


def _hardware_from_reader_blob(value):
    """hardwareId from a decrypted userInfos blob; '' when there is none."""
    for plain in _decrypt_reader_blob(value):
        try:
            parsed = json.loads(plain)
        except ValueError:
            continue
        if not isinstance(parsed, dict):
            continue
        for name in HARDWARE_VALUE_KEYS:
            nested = parsed.get(name)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _unwrap_refresh_candidate(token, max_depth=3):
    """Recover the JWT behind a wrapped, undatable candidate (0.9.33)."""
    text = str(token or "")
    if not text or _jwt_shaped(text):
        return text
    frontier = [text]
    seen = {text}
    for _depth in range(max_depth):
        nxt = []
        for value in frontier:
            # (a) CryptoJS/OpenSSL AES layer (reader userToken/userInfos)
            #     -- the 0.9.34 field candidate, a Salted__ ciphertext.
            for plain in _decrypt_reader_blob(value):
                plain = plain.strip()
                if not plain:
                    continue
                if _jwt_shaped(plain):
                    # Trusted plaintext under a refresh-ish key: the
                    # real token even if its payload lacks an iat.
                    return plain
                if plain.startswith("U2FsdGVk"):
                    # Nested blob inside a decrypted userToken bundle.
                    if plain not in seen:
                        seen.add(plain)
                        nxt.append(plain)
                    continue
                if plain.startswith(("{", "[")):
                    if plain.startswith("{"):
                        nested = _refresh_field_text(plain)
                        if nested:
                            if _jwt_shaped(nested):
                                return nested
                            if nested.startswith("U2FsdGVk"):
                                if nested not in seen:
                                    seen.add(nested)
                                    nxt.append(nested)
                            elif (len(nested) > 20
                                  and nested.isprintable()
                                  and not nested.startswith(("{", "["))):
                                return nested
                    continue
                # Trusted raw plaintext: an opaque real token beats the
                # ciphertext every time.
                if len(plain) > 20 and plain.isprintable():
                    return plain
            # (b) structural wrappers (JSON quotes / percent / base64)
            for peeled in _peel_candidate_wrappers(value):
                if peeled in seen:
                    continue
                if _jwt_shaped(peeled) and _jwt_payload(peeled):
                    return peeled
                seen.add(peeled)
                nxt.append(peeled)
        if not nxt:
            break
        frontier = nxt
    return text


def _normalize_live_grab(grabbed):
    """Unwrap the refresh candidates of ONE grab result (0.9.33)."""
    if not isinstance(grabbed, dict):
        return grabbed
    refresh = []
    for token in grabbed.get("refresh") or ():
        unwrapped = _unwrap_refresh_candidate(token)
        if unwrapped and unwrapped not in refresh:
            refresh.append(unwrapped)
    hardware = []
    for value in grabbed.get("hardware") or ():
        value = str(value or "").strip()
        if value.startswith("U2FsdGVk") or "Salted__" in value:
            value = _hardware_from_reader_blob(value)
        if value and value not in hardware:
            hardware.append(value)
    if (refresh == (grabbed.get("refresh") or [])
            and hardware == (grabbed.get("hardware") or [])):
        return grabbed
    updated = dict(grabbed)
    updated["refresh"] = refresh
    updated["hardware"] = hardware
    return updated


def _exchange_fresh_response(partner_id, hardware, ws_url, token_url,
                             candidate, form, data):
    """Exchange via in-page fetch and apply the response with full logic."""
    client = TolinoClient(partner_id, hardware or "")
    client.refresh = candidate
    client._apply_token_response(data)
    return client.refresh or candidate, client.hardware or hardware


def _exchange_grabbed_token(partner_id, hardware, grabbed, fresh):
    """Exchange the freshest grabbed candidate; returns (refresh, hardware)."""
    # Keycloak-Refresh-JWTs tragen "typ": "Refresh"; Access-Tokens sind
    # kurzlebig und haben denselben JWT-Aufbau -- ein Access-Token am
    # Token-Endpunkt "zu verbrauchen" hilft niemandem und der eigentliche
    # Refresh-Token wird dann womoeglich gar nicht erst probiert.
    def _is_refresh_typ(token):
        payload = _jwt_payload(token) or {}
        return str(payload.get("typ", "")).casefold() == "refresh"

    ordered = sorted(fresh, key=_refresh_token_iat_sort_key, reverse=True)
    ordered.sort(key=lambda token: 0 if _is_refresh_typ(token) else 1)
    candidate = ordered[0]
    hardware_candidates = [normalize_hardware_id(value)
                           for value in (grabbed.get("hardware") or ())]
    hardware_candidates = [value for value in hardware_candidates if value]
    hardware_id = hardware_candidates[0] if hardware_candidates else hardware
    partner = PARTNERS[int(partner_id)]

    # Grant zuerst IN der Reader-Seite (in-page fetch): dieselben TLS-/Header-
    # Fingerprints wie der Reader selbst -- der eigene POST wird vom
    # Bot-Schutz gelegentlich mit WAF-403 abgewiesen.
    try:
        from . import cdp as cdp_module
        ws_url = cdp_module.reader_ws_url()
    except Exception:
        cdp_module = None
        ws_url = None
    if ws_url:
        form = urlencode({
            "client_id": partner["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": candidate,
            "scope": partner["scope"],
        })
        status, body = cdp_module.exchange_refresh_in_browser(
            ws_url, partner["token_url"], form)
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("access_token"):
                return _exchange_fresh_response(
                    partner_id, hardware_id, ws_url, partner["token_url"],
                    candidate, form, data)
        browser_note = "Browser-Tausch HTTP %s: %s" % (
            status, _compact_error_text(sanitize_error(body))[:200])
    else:
        browser_note = "kein Anmeldefenster offen"

    # Fallback: Plugin-eigener POST (alter Weg, WAF-anfaellig).
    client = TolinoClient(partner_id, hardware_id or "")
    client.refresh = candidate
    try:
        client._login()
    except TolinoAuthError as exc:
        # The exchange consumed one grant; a retry would only replay.
        raise TolinoAuthError(
            "Der live gelesene Token wurde am Token-Endpunkt abgelehnt "
            "(%s; Browser-Tausch: %s). Bitte die Browser-Anmeldung "
            "erneut starten -- dabei wird der aktuelle Token direkt aus "
            "dem ge\u00f6ffneten Web Reader frisch gelesen."
            % (exc, browser_note)) from exc
    return client.refresh or candidate, client.hardware or hardware_id


# Nach Ablehnung wird auf eine NEU geschriebene Kopie gewartet: der Reader
# rotiert selbst alle 40-60 s. Lesen, was der Reader schreibt -- kein
# erzwungener Reload, keine Netzwerk-Interception.
_NEW_TOKEN_ATTEMPTS = 30
_NEW_TOKEN_INTERVAL = 3
# Wartezeit richtet sich nach dem Fensterzustand:
# * Endpunkt stumm: nach _WINDOW_MISSES_BEFORE_QUIT abbrechen;
# * Fenster offen ohne Reader-Tab (Sitzung tot):
#   _LOGIN_PAGE_EXTRA_ATTEMPTS weitere Versuche -- dort kann neu
#   angemeldet und das Fenster wiederverwendet werden.
_LOGIN_PAGE_EXTRA_ATTEMPTS = 30
_WINDOW_MISSES_BEFORE_QUIT = 3


def grab_live_refresh(partner_id, hardware, timeout=300, progress=None):
    """Read the Web Reader's CURRENT token via a private CDP window.

    Returns (rotated_refresh, hardware_id). Raises TolinoAuthError when
    no Chromium browser is installed -- callers fall back to the
    historical disk-scrape flow in that case.
    """
    from . import cdp as cdp_module
    grabbed = _normalize_live_grab(cdp_module.grab_live_tokens(
        partner_id, hardware, timeout=timeout, progress=progress))
    tried = set()
    first_error = ""
    attempt = 0
    endpoint_misses = 0
    at_login_page = False
    while True:
        attempt += 1
        fresh = [token for token in _filter_live_candidates(
            (grabbed or {}).get("refresh") or ()) if token not in tried]
        if fresh:
            if progress:
                progress("Tausche den gelesenen Token am Token-Endpunkt ...")
            try:
                result = _exchange_grabbed_token(partner_id, hardware,
                                                 grabbed, fresh)
            except TolinoAuthError as exc:
                # Endgueltige Ablehnung: diese Kopie und ihre Geschwister
                # derselben Sitzung sind verbraucht und werden nie wieder
                # probiert (Reuse-Schutz) -- stattdessen wird unten auf
                # eine NEU geschriebene Kopie gewartet.
                first_error = str(exc)
                tried.update((grabbed or {}).get("refresh") or ())
                if progress:
                    progress("Token am Token-Endpunkt abgelehnt -- warte "
                             "auf eine frische Rotation des Web Readers ...")
            else:
                # Der Token gehoert ab jetzt Calibre: ein offener Reader rotiert weiter
                # und wuerde die uebernommene Kopie verbrennen (Reuse-Schutz).
                if progress:
                    progress("Token übernommen -- das Anmeldefenster "
                             "wird geschlossen.")
                cdp_module.close_grabber_window()
                return result
        limit = _NEW_TOKEN_ATTEMPTS + (
            _LOGIN_PAGE_EXTRA_ATTEMPTS if at_login_page else 0)
        if attempt >= limit:
            break
        if progress:
            if at_login_page:
                progress("Das Anmeldefenster ist offen, zeigt aber keine "
                         "angemeldete Web-Reader-Seite -- bitte dort JETZT "
                         "im Web Reader neu anmelden (bis die Bücherliste "
                         "lädt); der frische Token wird dann automatisch "
                         "übernommen (Versuch %d) ..." % attempt)
            else:
                progress("Warte auf einen neu geschriebenen Token im Seiten-"
                         "Speicher (Versuch %d von %d; im Fenster angemeldet "
                         "bleiben) ..." % (attempt, _NEW_TOKEN_ATTEMPTS))
        time.sleep(_NEW_TOKEN_INTERVAL)
        more = _normalize_live_grab(
            cdp_module.grab_once_from_grabber())
        if more and more.get("refresh"):
            grabbed = more
            continue
        # Nichts lesbar -- WARUM? Fenster weg (Endpunkt stumm, nach drei
        # Fehlschlaegen abbrechen) vs. Fenster offen ohne Reader-Tab (Sitzung
        # tot: weiter warten und Neu-Anmeldung vor Ort anfordern).
        try:
            window = cdp_module.grabber_window_state()
        except Exception:
            window = None
        if window is None:
            continue
        if window.get("window"):
            endpoint_misses = 0
            at_login_page = not window.get("reader_tab")
            continue
        endpoint_misses += 1
        if endpoint_misses >= _WINDOW_MISSES_BEFORE_QUIT:
            break
    try:
        state = cdp_module.describe_grab_state()
    except Exception:
        state = "Zustand der Seite nicht lesbar"
    try:
        window = cdp_module.grabber_window_state()
    except Exception:
        window = {"window": False, "reader_tab": False}
    if window.get("window") and not window.get("reader_tab"):
        # Fenster auf der Anmeldeseite: NICHT schliessen -- dort kann neu
        # angemeldet werden; launch_reader_window wiederverwendet es.
        action = ("Das Anmeldefenster ist noch offen und zeigt gerade "
                  "keine angemeldete Web-Reader-Seite: bitte dort im Web "
                  "Reader neu anmelden, bis die Bücherliste lädt, und den "
                  "Knopf erneut drücken -- das Fenster bleibt offen und "
                  "wird wiederverwendet.")
    else:
        # Nichts Tauschbares im Fenster: offen zu lassen wuerde den
        # naechsten Versuch wieder zum Loop machen (dieselbe tote Kopie,
        # dieselbe Ablehnung). Schliessen -> naechster Start mit frischem
        # Profil und echter Anmeldeseite statt gecachter Buecherliste.
        cdp_module.close_grabber_window()
        action = ("Das Anmeldefenster wurde geschlossen, weil dort nur ein "
                  "verbrauchter Token liegt: die Browser-Anmeldung erneut "
                  "starten und im neuen Fenster anmelden, bis die "
                  "Bücherliste lädt.")
    age_note = _candidate_age_note(
        tried or (grabbed or {}).get("refresh") or ())
    raise TolinoAuthError(
        "Im Web Reader wurde weder ein frischer Token im Seiten-"
        "Speicher gefunden noch innerhalb der Wartezeit nachgeschoben "
        "(%s). %s"
        % (state, action)
        + (" Letzter Fehler: %s" % first_error if first_error else "")
        + (" %s." % age_note if age_note else ""))


def try_live_grab_first(partner_id, hardware, timeout=12, single_attempt=True):
    """Read the current token from a running grabber window, if any.

    Returns (refresh, hardware) when a grabber window is alive AND its
    page yielded an exchangeable token; the window is closed first --
    the plugin owns the session now, and a reader left running would
    rotate the adopted copy away (reuse protection kills the session).
    Returns None when no grabber window is running -- the caller should
    then use the historical disk-scrape flow.
    """
    from . import cdp as cdp_module
    if not cdp_module.devtools_port_alive():
        return None
    grabbed = _normalize_live_grab(
        cdp_module.grab_once_from_grabber(timeout=timeout))
    state = cdp_module.describe_grab_state(timeout=timeout)
    fresh = _filter_live_candidates((grabbed or {}).get("refresh") or [])
    first_error = ""
    if fresh:
        try:
            result = _exchange_grabbed_token(partner_id, hardware, grabbed,
                                             fresh)
        except TolinoAuthError as exc:
            # Endgueltig abgelehnte Kopie: nie erneut probieren, und den
            # GUI-Thread nicht mit einer Lauschphase blockieren -- die
            # Fehlermeldung unten sagt F5 + Knopf erneut druecken.
            first_error = str(exc)
        else:
            # Token gehoert ab jetzt Calibre -- Fenster zu, damit der
            # Reader die uebernommene Kopie nicht weiter rotiert.
            cdp_module.close_grabber_window()
            return result
    age_note = _candidate_age_note((grabbed or {}).get("refresh") or ())
    raise TolinoAuthError(
        "Im Anmeldefenster war kein tauschbarer Token zu gewinnen: "
        "Kandidaten \u00e4lter als 30 Minuten oder am Endpoint "
        "abgelehnt (%s). Bitte im Fenster einmal neu anmelden (F5 "
        "gen\u00fcgt oft) und den Knopf direkt danach erneut "
        "dr\u00fccken."
        % (state + ("; Letzter Fehler: %s" % first_error
                    if first_error else ""))
        + (" %s." % age_note if age_note else ""))


def in_flatpak_sandbox():
    """True, wenn dieses Prozess in einer Flatpak-Sandbox läuft."""
    return os.path.exists("/.flatpak-info")


def _run_open_tool(argv, timeout=20):
    """Öffner-argv synchron starten; True bei Exit-Code 0."""
    try:
        proc = subprocess.run(argv, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _run_open_chain(url):
    """Öffnerkette für sandboxige Umgebungen; True wenn einer startet."""
    for argv in (["xdg-open"], ["gio", "open"], ["gnome-open"],
                 ["kde-open"], ["x-www-browser"], ["open"]):
        if _run_open_tool(list(argv) + [url]):
            return True
    if in_flatpak_sandbox() and shutil.which("flatpak-spawn"):
        return _run_open_tool(["flatpak-spawn", "--host", "xdg-open", url])
    return False


def open_system_browser(url):
    """URL im System-Browser öffnen; True, wenn ein Öffner gestartet ist."""
    sandbox = in_flatpak_sandbox()
    if sandbox and _run_open_chain(url):
        return True
    raised = False
    try:
        if webbrowser.open(url):
            return True
    except Exception:
        raised = True
    if sandbox or raised:
        return _run_open_chain(url)
    return False


def open_login_browser(url):
    """Anmeldelink im Browser öffnen; True wenn ein Öffner gestartet ist."""
    from . import cdp as cdp_module
    try:
        option = cdp_module.pick_chromium()
    except Exception:
        option = None
    if option:
        try:
            subprocess.Popen(list(option[0]) + [url],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True
        except OSError:
            pass  # kaputter erster Kandidat: System-Browser als Notfall
    return open_system_browser(url)


def _system_browser_open_error():
    """Fehler für "kein Browser geöffnet" -- in der Sandbox mit Fix dazu."""
    base = "Der System-Browser konnte nicht geöffnet werden."
    if not in_flatpak_sandbox():
        return base
    return base + (
        " Calibre läuft als Flatpak (Flathub): aus der Sandbox heraus"
        " sind die Browser des Rechners nicht erreichbar. Einmalig die"
        " Freigabe erteilen -- bitte zur Installation passend -- und"
        " Calibre danach VOLLSTÄNDIG neu starten (laufende Fenster"
        " behalten die alten Rechte):"
        "\nUser-Installation: flatpak override --user"
        " --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre"
        "\nSystem-Installation (flatpak info zeigt \"Installation:"
        " system\"): sudo flatpak override --talk-name=org.freedesktop."
        "Flatpak com.calibre_ebook.calibre"
        "\nWirksam ist die Freigabe, wenn flatpak info"
        " --show-permissions com.calibre_ebook.calibre"
        " org.freedesktop.Flatpak enthält."
        "\nOhne diese Freigabe alternativ den Refresh-Token manuell aus"
        " einem laufenden Web Reader übernehmen (siehe README).")


def chromium_available():
    """True, wenn das Plugin ein eigenes Chromium-Fenster öffnen kann."""
    from . import cdp as cdp_module
    try:
        return cdp_module.pick_chromium() is not None
    except Exception:
        return False


def spent_token_advice():
    """Handlungsempfehlung für den "alle Kandidaten verbraucht"-Dialog."""
    if chromium_available():
        return (
            "Besser: \u201eIm Browser anmelden\u201c benutzen -- das "
            "öffnet ein eigenes Chromium-Fenster, liest den Token live "
            "aus der Seite und schließt NUR dieses Fenster; Ihr Browser "
            "(z. B. Firefox) bleibt geöffnet.")
    text = (
        "Ohne Chrome/Chromium/Brave/Edge kann das Plugin kein eigenes "
        "Fenster öffnen und greift nur auf die Festplatten-Kopien zu. "
        "Einen dieser Browser installieren und dann \u201eIm Browser "
        "anmelden\u201c benutzen. Oder ALLE Browserfenster schließen "
        "(der Reader schreibt dann seinen letzten Token auf die "
        "Festplatte) und diesen Knopf erneut drücken.")
    if in_flatpak_sandbox():
        text += (
            " Calibre als Flatpak (Flathub): evtl. sind die Browser des "
            "Rechners ohne Freigabe unsichtbar -- einmalig die Freigabe "
            "erteilen (User-Installation: flatpak override --user "
            "--talk-name=org.freedesktop.Flatpak com.calibre_ebook."
            "calibre; System-Installation: sudo flatpak override "
            "--talk-name=org.freedesktop.Flatpak com.calibre_ebook."
            "calibre) und Calibre danach vollständig neu starten.")
    return text


def _keycloak_assisted_login(partner_id, hardware, timeout=OAUTH_STATE_TTL,
                             progress=None):
    """Guided browser sign-in for Keycloak partners without local callback."""
    try:
        return grab_live_refresh(partner_id, hardware, timeout=timeout,
                                 progress=progress)
    except TolinoAuthError as exc:
        if "Kein Chromium-Browser gefunden" not in str(exc):
            raise
        if in_flatpak_sandbox():
            # In der Flatpak-Sandbox kein stiller Firefox-Fallback: der Fehler
            # mit dem override-Befehl wird stattdessen sichtbar gemacht.
            raise
        if progress:
            progress(
                "Kein Chromium-Browser gefunden -- öffne den "
                "Standardbrowser als Notfall. Für das eigene "
                "Anmeldefenster Chrome/Chromium/Brave/Edge installieren.")
        return _disk_assisted_login(partner_id, hardware, timeout)


def _disk_assisted_login(partner_id, hardware, timeout=OAUTH_STATE_TTL):
    """Disk-scrape fallback for Keycloak partners (no Chromium browser)."""
    partner = PARTNERS[partner_id]
    # The Web Reader's own login flow uses the partner's registered redirect
    # URIs, so it can never hit the Keycloak redirect_uri error.
    auth_url = partner.get("reader_url")
    if not auth_url:
        params = {
            "client_id": partner["client_id"],
            "response_type": "code",
            "scope": partner["scope"],
        }
        for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
            if partner.get(key):
                params[key] = partner[key]
        auth_url = partner["auth_url"]
        if "?" not in auth_url:
            auth_url = auth_url + "?" + urlencode(params)
    if not open_login_browser(auth_url):
        raise TolinoAuthError(_system_browser_open_error())

    deadline = time.time() + max(30, timeout)
    poll_seconds = 2
    # Der Reader rotiert alle 40-60 s; auf "Stabilitaet" zu warten
    # gewinnt gegen ein offenes Fenster nie. Statt Blockade: pro Runde
    # ernten, abgelehnte Kopien (invalid_grant) merken und weiterscannen,
    # bis ein NEU geschriebener Kandidat erscheint (meist nach Login oder
    # Tab-Schluss, wenn Chromium den Speicher flush-t).
    seen_spent = set()   # definitively rejected: never retried
    unclear_at = {}      # candidate -> last unclear attempt (time.time)
    tried = 0
    last_note = ""
    while time.time() < deadline:
        time.sleep(poll_seconds)
        refreshes, hardwares, notes = scrape_browser_tokens(
            diagnose=True, all_candidates=True)
        if notes:
            last_note = notes[-1]
        if not refreshes:
            continue
        # Maximal EIN Endpunkt-POST pro Kandidat: der Tausch braucht keine
        # Hardware-ID -- Replays wuerden nur den Reuse-Schutz triggern.
        now = time.time()
        pending = []
        for candidate in _select_candidate_round(refreshes, seen_spent):
            if candidate in unclear_at and now - unclear_at[candidate] < 30:
                continue  # unclear verdict: retry at most every 30 s
            pending.append(candidate)
        for candidate in pending:
            verdict, rotated, adopted_hw = _validate_one(
                partner_id, hardware, candidate)
            if verdict == "ok":
                return rotated, adopted_hw or hardware
            if verdict == "spent":
                seen_spent.add(candidate)
                tried += 1
            else:
                # Network/protection/server trouble: keep the candidate
                # alive for a later round instead of declaring it dead.
                unclear_at[candidate] = time.time()
        # Candidates exhausted (or none new yet): keep polling. A storage
        # that keeps changing means the reader is still active or was just
        # closed; its next background refresh (or the close-flush) writes
        # a fresh token that a later iteration picks up automatically.
    if tried:
        raise TolinoAuthError(
            "%d gefundene(n) Refresh-Token wurden gepr\u00fcft, aber keiner "
            "war g\u00fcltig (genau ein Versuch pro Kandidat; nur der "
            "neueste Token einer Sitzung tauscht). Pr\u00fcfe: "
            "1) WEB READER ge\u00f6ffnet? (B\u00fccherliste sichtbar, nicht "
            "nur der Shop). 2) Web Reader neu laden (F5) und die "
            "Browser-Anmeldung direkt danach starten. 3) Falls n\u00f6tig "
            "den Browser einmal vollst\u00e4ndig SCHLIESSEN und erneut "
            "versuchen -- Chromium schreibt den Storage oft erst beim "
            "Schlie\u00dfen. Kandidaten nach Alter: %s (letzte Meldung: %s)."
            % (tried,
               _candidate_ages_summary(sorted(
                   seen_spent, key=_refresh_token_iat_sort_key), None),
               last_note or "kein Browser-Storage gelesen")
        )
    raise TolinoAuthError(
        "Nach dem Anmelden im Browser wurde kein frischer Tolino-"
        "Refresh-Token gefunden (letzte Meldung: %s). Melde dich im "
        "ge\u00f6ffneten Web Reader (Bibliothek, Buecherliste geladen) "
        "an, lade ihn einmal neu (F5) und starte die Browser-Anmeldung "
        "erneut."
        % (last_note or "kein Browser-Storage gelesen")
    )


# --- Token Keep-alive --------------------------------------------------------
# Keycloak-Refresh-Tokens sterben nach ~1 h Leerlauf; der Keep-alive
# rotiert sie im Intervall still und speichert sie ueber den Callback.
_KEEPALIVE_LOCK = threading.Lock()
_KEEPALIVE_STOP = threading.Event()


def keepalive_refresh(get_credentials, persist):
    """One silent keep-alive login; returns the rotated token or None."""
    if not _KEEPALIVE_LOCK.acquire(blocking=False):
        return None
    try:
        creds = get_credentials() or {}
        token = str(creds.get("refresh_token") or "").strip()
        if not token:
            return None
        last = {"value": token}

        def _persist(value):
            """Persist rotations once; login may emit the same value twice."""
            if value and value != last["value"]:
                last["value"] = value
                if persist is not None:
                    persist(value)

        client = TolinoClient(
            creds.get("partner_id"), creds.get("hardware_id") or "", token,
            creds.get("username"), creds.get("password"),
            token_callback=_persist)
        client.login()
        _persist(client.refresh)
        return client.refresh
    except Exception:
        return None
    finally:
        _KEEPALIVE_LOCK.release()


def start_token_keepalive(get_credentials, persist, interval=45 * 60):
    """Rotate the stored refresh token every `interval` seconds."""
    if getattr(start_token_keepalive, "_started", False):
        return None
    start_token_keepalive._started = True

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            keepalive_refresh(get_credentials, persist)

    thread = threading.Thread(target=loop, name="tolino-token-keepalive",
                              daemon=True)
    thread.start()
    return thread


def browser_login(partner_id, hardware, timeout=OAUTH_STATE_TTL,
                  progress=None):
    """Sign in via the partner's OAuth authorization endpoint."""
    partner = PARTNERS.get(int(partner_id))

    if not partner or not partner.get("auth_url"):
        raise TolinoAuthError(
            "F\u00fcr diesen Buchh\u00e4ndler ist keine OAuth-Anmeldeseite "
            "hinterlegt. Alternativ ein Refresh-Token aus dem Web Reader "
            "im Dialog eintragen."
        )
    if not partner.get("token_url"):
        raise TolinoAuthError(
            "F\u00fcr diesen Buchh\u00e4ndler ist kein Token-Endpunkt "
            "hinterlegt. Alternativ ein Refresh-Token aus dem Web Reader "
            "im Dialog eintragen."
        )

    # Gemeinsamer Web Reader: ein Weg fuer alle Shops, die dort liegen
    # (Thalia, Hugendubel, eBook.de, buecher.de, Osiander, Orell
    # Fuessli) -- der localhost-Callback kann bei ihnen nie ankommen,
    # weil deren OAuth-Redirect auf webreader.mytolino.com zeigt.
    if (uses_shared_webreader(partner)
            or str(partner.get("reseller_id"))
            in LOCAL_CALLBACK_UNSUPPORTED_RESELLERS):
        return _keycloak_assisted_login(int(partner_id), hardware, timeout,
                                        progress=progress)

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

    if not open_login_browser(partner["auth_url"] + "?" + urlencode(params)):
        server.server_close()
        raise TolinoAuthError(_system_browser_open_error())
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
        "scope": partner["scope"],
    }
    for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
        if partner.get(key):
            payload[key] = partner[key]
    data = client._request(partner["token_url"], "POST", payload,
                           form=True, authenticated=False)
    if not data.get("access_token") or not data.get("refresh_token"):
        raise TolinoAuthError("Browser-Anmeldung lieferte eine unvollst\u00e4ndige Token-Antwort.")
    return data["refresh_token"], client.hardware


# --- Optionaler curl-Transport -----------------------------------------------
# Der Token-Endpunkt fingerprintet TLS; curl/curl_cffi impersonieren
# einen Browser, urllib bleibt Fallback ohne Abhaengigkeiten.

CURL_BINARIES = ("curl",)

# The token endpoint sits behind bot protection that also inspects the
# User-Agent; a plain "Calibre-Tolino-Plugin" UA is an easy flag.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def _browser_sec_headers():
    """Browser-consistent Sec-Fetch/Client-Hints headers for token requests."""
    match = re.search(r"Chrome/(\d+)", BROWSER_USER_AGENT)
    major = match.group(1) if match else "153"
    return {
        "Accept": "*/*",
        "Accept-Language": "de-DE,de;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-CH-UA": '"Chromium";v="%s", "Not_A Brand";v="8"' % major,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-GPC": "1",
    }


def _compact_error_text(detail):
    """Collapse HTML bot-check pages to readable text for error messages."""
    text = re.sub(r"<[^>]+>", " ", detail or "")
    return re.sub(r"\s+", " ", text).strip()


_BOT_CHECK_MARKERS = (
    "zugriff geblockt", "access denied", "captcha", "cloudfront",
    "request blocked", "<!doctype html",
)

def _bot_check_detected(detail):
    """True when the response looks like a bot-protection page, not OAuth."""
    return any(marker in str(detail or "").casefold()
               for marker in _BOT_CHECK_MARKERS)


def _waf_block_response(status, raw):
    """True for a 403 bot-protection page at the token endpoint."""
    if status != 403:
        return False
    text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
            else str(raw or ""))
    if not text.strip():
        return True  # leere 403 ohne OAuth-Body: WAF, nicht Keycloak
    return _bot_check_detected(text)

def _curl_binary():
    """Return a usable curl binary path, or None if curl is not installed."""
    for name in CURL_BINARIES:
        path = shutil.which(name)
        if path:
            return path
    return None


# curl_cffi (if installed) impersonates Chrome's TLS fingerprint exactly like
# the pytolino reference client; plain urllib/curl handshakes are rejected by
# the bot protection in front of some partner token endpoints (HTTP 403
# without an OAuth error body).
def _impersonate_session():
    """Return a curl_cffi session that impersonates Chrome, or None."""
    try:
        from curl_cffi.requests import Session
    except Exception:
        return None
    try:
        session = Session(impersonate="chrome")
        if not callable(getattr(session, "post", None)):
            return None
        return session
    except Exception:
        return None


def _http_post_via_curl_cffi(session, url, body, headers, timeout):
    """POST via a curl_cffi session; returns (status:int, raw_bytes)."""
    response = session.post(
        url,
        data=body.decode("utf-8", "replace") if isinstance(body, bytes) else body,
        headers=dict(headers),
        timeout=timeout,
        allow_redirects=True,
    )
    return int(response.status_code), response.content or b""

def _http_post_via_curl(url, body, headers, timeout):
    """POST via curl; returns (status:int, raw_bytes). Raises RuntimeError."""
    binary = _curl_binary()
    if not binary:
        raise RuntimeError("curl not available")
    argv = [
        binary,
        "--silent",
        "--show-error",
        "--location",           # follow redirects like a browser would
        "--max-time", str(int(timeout)),
        "--write-out", "\n%{http_code}",
        "--url", url,
        "--request", "POST",
        "--data-binary",
        body.decode("utf-8", "replace") if isinstance(body, bytes) else body,
    ]
    for key, value in headers.items():
        argv.extend(["--header", "%s: %s" % (key, value)])
    completed = subprocess.run(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout + 5)
    if completed.returncode != 0 and not completed.stdout:
        raise RuntimeError("curl failed: %s" %
                           completed.stderr.decode("utf-8", "replace")[:300])
    output = completed.stdout
    # Split off the trailing status code written by --write-out.
    body_part, _, status_part = output.rpartition(b"\n")
    try:
        status = int(status_part.strip() or 0)
    except ValueError:
        status = 0
    if not status:
        status = 200 if completed.returncode == 0 else 0
    return status, body_part

class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, username=None,
                 secret=None, timeout=45, token_callback=None,
                 hardware_callback=None):
        partner_id = resolve_partner_id(partner_id)
        if partner_id not in PARTNERS:
            raise TolinoError("Unsupported Tolino partner ID: %s" % partner_id)
        self.partner_id = int(partner_id)
        self.partner = PARTNERS[self.partner_id]
        self.hardware = normalize_hardware_id(hardware) or hardware_id()
        self.refresh, self.token_diagnostics = normalize_refresh_token(refresh)
        self.username = username
        self.password = secret
        self.timeout = timeout
        self.access = None
        self.expires_at = 0
        self.refresh_expires_at = 0
        self.last_http_status = None
        self.last_error_text = None
        self.last_transport = None
        self.token_callback = token_callback
        self.hardware_callback = hardware_callback
        self._in_device_recovery = False
        self._hw_registered_now = False
        self._login_lock = threading.Lock()

    def auth_diagnostics(self):
        """Return safe authentication context for the debug report."""
        return {
            "transport": getattr(self, "last_transport", None),
            "curl_cffi": _impersonate_session() is not None,
            "partner_id": self.partner_id,
            "partner_name": self.partner["name"],
            "client_id": self.partner.get("client_id"),
            "scope": self.partner.get("scope"),
            "token_url": self.partner.get("token_url"),
            "grant_type": "refresh_token",
            "hardware_id": self.hardware,
            "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
            "refresh_expires_in": self.refresh_expires_in,
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

    def _apply_token_response(self, data):
        """Store the token endpoint response; returns the rotated refresh."""
        if not data.get("access_token"):
            raise TolinoAuthError("Tolino-Token-Antwort enthielt kein access_token.")
        previous_refresh = self.refresh
        self.access = data["access_token"]
        new_refresh = data.get("refresh_token", self.refresh)
        try:
            refresh_expires_in = int(data.get("refresh_expires_in", 0))
        except (TypeError, ValueError):
            refresh_expires_in = 0
        self.refresh_expires_at = (
            time.time() + refresh_expires_in if refresh_expires_in > 0 else 0)

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

    def _login(self):
        if self.refresh:
            if not self.partner.get("token_url"):
                raise TolinoAuthError(
                    "F\u00fcr diesen Buchh\u00e4ndler ist kein gepr\u00fcfter "
                    "Refresh-Token-Endpunkt hinterlegt."
                )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh,
                "scope": self.partner["scope"],
            }
        elif self.username and self.password:
            raise TolinoAuthError(
                "Benutzername/Passwort erfordert die OAuth-Anmeldung des "
                "Buchh\u00e4ndlers. F\u00fcr dieses Plugin ein Refresh-Token "
                "aus dem Web Reader verwenden."
            )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": self.partner["scope"],
            }
        else:
            raise TolinoAuthError("Bitte zuerst ein Refresh-Token konfigurieren.")
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except (TolinoApiError, TolinoAuthError) as exc:
            detail = str(exc)
            folded = detail.casefold()
            if "not active" in folded or "session not active" in folded:
                # Keycloak answers "invalid_grant / Session not active" when
                # the account's SSO session ended (logged out, expired, or
                # another device rotated the token). The token value itself
                # is not the problem here.
                self.refresh = None
                self.access = None
                self.expires_at = 0
                raise TolinoAuthError(
                    "Die Tolino-Sitzung dieses Refresh-Tokens ist beendet "
                    "(Session not active): Der Account wurde im Web Reader "
                    "abgemeldet oder die Sitzung ist abgelaufen. Melde dich "
                    "im Web Reader (Bibliothek) neu an und starte danach "
                    "die Browser-Anmeldung erneut."
                ) from exc
            if "invalid_grant" in folded or "reuse exceeded" in folded:
                # Invalidate the current refresh token to prevent reuse
                self.refresh = None
                self.access = None
                self.expires_at = 0
                raise TolinoAuthError(
                    "Tolino hat diesen Refresh-Token abgelehnt (verbraucht oder "
                    "widerrufen). Melde dich im Web Reader neu an und "
                    "verwende den neuen Token nicht mehrfach."
                ) from exc
            raise TolinoAuthError("Tolino authentication failed: %s" % exc)
        return self._apply_token_response(data)

    @property
    def refresh_expires_in(self):
        """Seconds until the (rotated) refresh token becomes invalid; 0 = unknown."""
        if not self.refresh_expires_at:
            return 0
        return max(0, int(self.refresh_expires_at - time.time()))

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _retry=True,
                 _extra_headers=None):
        if authenticated and (not self.access or time.time() >= self.expires_at):
            self.login()
        body = None
        headers = {"User-Agent": BROWSER_USER_AGENT}
        if authenticated:
            headers.update({
                "t_auth_token": self.access,
                "hardware_id": self.hardware,
                "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
            })
            for key, shared in (("client_type", WEBREADER_CLIENT_TYPE),
                                ("client_version", WEBREADER_CLIENT_VERSION)):
                value = self.partner.get(key) or (
                    shared if uses_shared_webreader(self.partner) else None)
                if value:
                    headers[key] = value
        elif url == self.partner.get("token_url"):
            # Look like the web reader's own token POST: full Sec-Fetch /
            # Client-Hints set first, Origin/Referer of the shared Web
            # Reader last -- jeder Partner mit dem gemeinsamen Reader
            # bekommt damit exakt die Kopfzeilen von Orell Fuessli.
            headers.update(_browser_sec_headers())
            headers.update(self.partner.get("token_headers")
                           or (WEBREADER_TOKEN_HEADERS
                               if uses_shared_webreader(self.partner)
                               else {}))
        if _extra_headers:
            headers.update(_extra_headers)
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

        def _decode(raw):
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise TolinoApiError("Tolino returned invalid JSON.")

        def _error(exc, raw_detail):
            self.last_http_status = exc.code
            try:
                payload = json.loads(raw_detail)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                safe_detail = {
                    key: payload[key] for key in
                    ("error", "error_description", "message")
                    if key in payload
                }
                response_info = payload.get("ResponseInfo")
                if (isinstance(response_info, dict)
                        and response_info.get("message")):
                    # BOSH services report failures as ResponseInfo.message;
                    # filtering that out showed field reports a mute "{}".
                    safe_detail.setdefault(
                        "message", str(response_info["message"])[:300])
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
                                     content_type, _retry=False,
                                     _extra_headers=_extra_headers)
            if (exc.code == 400 and authenticated
                    and url != self.partner.get("token_url")):
                # 400 am authentifizierten BOSH-Call = unbekannte Hardware-ID (Antwort:
                # leeres "{}"): Geraet uebernehmen/registrieren und nach kurzem Warten
                # (registerhw braucht einen Moment) wiederholen.
                if _retry:
                    self._hw_registered_now = False
                    try:
                        recovered = self._recover_device_registration()
                    except Exception:
                        recovered = False
                    if recovered:
                        if self._hw_registered_now:
                            time.sleep(_REGISTER_SETTLE_SECONDS)
                        return self._request(url, method, data, form, True,
                                             content_type, _retry=False,
                                             _extra_headers=_extra_headers)
                elif self._hw_registered_now:
                    # Exactly one more attempt after the registration wait.
                    self._hw_registered_now = False
                    time.sleep(_REGISTER_SETTLE_SECONDS)
                    return self._request(url, method, data, form, True,
                                         content_type, _retry=False,
                                         _extra_headers=_extra_headers)
            if exc.code in (401, 403):
                if (exc.code == 403
                        and url == self.partner.get("token_url")
                        and _retry
                        and _bot_check_detected(self.last_error_text)):
                    # Kurz warten und den Transport-Wechsel erneut
                    # probieren (zwei Warteversuche, danach sichtbarer
                    # Fehler).
                    time.sleep(_WAF_RETRY_SECONDS[0] if _retry is True
                               else _WAF_RETRY_SECONDS[1])
                    return self._request(
                        url, method, data, form, authenticated, content_type,
                        _retry=1 if _retry is True else False,
                        _extra_headers=_extra_headers)
                self.access = None
                message = (_compact_error_text(self.last_error_text)
                           or "no response detail")
                raise TolinoAuthError(
                    "Tolino hat die Anmeldung abgelehnt (%s): %s" % (exc.code, message)
                )
            raise TolinoApiError("Tolino HTTP %s: %s" % (exc.code, self.last_error_text))

        # Token-Endpunkt bevorzugt ueber curl_cffi (impersonate="chrome") --
        # der Bot-Schutz mag Chrome-TLS lieber; curl und urllib bleiben
        # Fallbacks ohne optionale Abhaengigkeiten.
        if data is not None and url == self.partner.get("token_url"):
            session = _impersonate_session()
            if session is not None:
                self.last_transport = "curl_cffi"
                try:
                    status, raw = _http_post_via_curl_cffi(
                        session, url, body or b"", headers, self.timeout)
                except Exception:
                    status, raw = None, None  # fall through to curl/urllib
                else:
                    self.last_http_status = status
                    if status and status < 400:
                        self.last_error_text = None
                        return _decode(raw)
                    if not _waf_block_response(status, raw):
                        return _error(
                            HTTPError(url, status or 400,
                                      "HTTP Error %s" % (status or "?"), {}, None),
                            (raw or b"").decode("utf-8", "replace"))
                    # Bot-Schutz-Block: naechster Transport statt Abbruch
                    # (Feldbefund 0.9.38: System-curl laeuft dauerhaft in
                    # die 403, urllib mit den Browser-Headern nicht).
            if _curl_binary() is not None:
                self.last_transport = "curl"
                try:
                    status, raw = _http_post_via_curl(
                        url, body or b"", headers, self.timeout)
                except (RuntimeError, OSError, subprocess.TimeoutExpired):
                    status, raw = None, None  # fall back to urllib below
                else:
                    self.last_http_status = status
                    if status and status < 400:
                        self.last_error_text = None
                        return _decode(raw)
                    if not _waf_block_response(status, raw):
                        return _error(
                            HTTPError(url, status or 400,
                                      "HTTP Error %s" % (status or "?"), {}, None),
                            (raw or b"").decode("utf-8", "replace"))
                    # Bot-Schutz-Block: naechster Transport statt Abbruch
                    # (Feldbefund 0.9.38: System-curl laeuft dauerhaft in
                    # die 403, urllib mit den Browser-Headern nicht).

        request = Request(url, data=body, headers=headers, method=method)
        self.last_transport = "urllib"
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self.last_http_status = response.status
                self.last_error_text = None
                raw = response.read()
        except HTTPError as exc:
            return _error(exc, exc.read().decode("utf-8", "replace"))
        except (URLError, OSError) as exc:
            raise TolinoApiError("Tolino request failed: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)))
        return _decode(raw)

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

    # --- In-place metadata (pytolino/darkphoenix-derived /meta flow) -------
    META_URL = BASE_URL + "/meta"

    def update_metadata(self, deliverable_id, fields):
        """Update title/author/isbn on an existing book instead of
        re-uploading the file (0.9.45).

        GET /meta?deliverableId=... returns the book with its "metadata"
        dict; the merged dict goes back via PUT as {"uploadMetaData": ...}.
        """
        if not isinstance(fields, dict) or not fields:
            raise TolinoApiError("Metadata update requires at least one field.")
        url = self.META_URL + "/?deliverableId=" + _url_quote(str(deliverable_id))
        book = self._request(url)
        metadata = book.get("metadata") if isinstance(book, dict) else None
        if not isinstance(metadata, dict):
            raise TolinoApiError(
                "Tolino metadata response had no metadata for %s."
                % deliverable_id)
        metadata.update(fields)
        self._request(url, "PUT", {"uploadMetaData": metadata},
                      content_type="application/json")
        return metadata

    # --- Device list (pytolino-derived): resolve the real hardware ID ------

    DEVICES_URL = "https://bosh.pageplace.de/bosh/rest/handshake/devices/list"

    def fetch_hardware_id(self):
        """Return the most recently used hardware ID registered for the account."""
        payload = {
            "deviceListRequest": {
                "accounts": [{
                    "auth_token": self.access,
                    "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
                }],
            },
        }
        data = self._request(self.DEVICES_URL, "POST", payload,
                             content_type="application/json")
        devices = (data.get("deviceListResponse") or {}).get("devices") or []
        devices = [item for item in devices if isinstance(item, dict)]
        if not devices:
            raise TolinoApiError("Tolino device list returned no devices.")
        devices.sort(key=lambda item: str(item.get("deviceLastUsage", "")))
        hardware = devices[-1].get("deviceId")
        if not hardware:
            raise TolinoApiError("Tolino device list entry has no deviceId.")
        return str(hardware)

    def _notify_hardware(self):
        """Tell the UI about an adopted hardware id; never raises."""
        callback = getattr(self, "hardware_callback", None)
        if callback:
            try:
                callback(self.hardware)
            except Exception:
                pass

    def _register_hardware(self):
        """Register this hardware id at the BOSH service (reference flow)."""
        payload = {"hardware_name": "other"}
        extra = {
            "hardware_type": "HTML5",
            "client_type": (self.partner.get("client_type")
                            or "TOLINO_WEBREADER"),
            "client_version": self.partner.get("client_version") or "5.2.0",
        }
        for url in (BASE_URL + "/v2/registerhw", BASE_URL + "/registerhw"):
            try:
                self._request(url, "POST", payload,
                              content_type="application/json",
                              _extra_headers=extra)
                return True
            except Exception:
                continue
        return False

    def _recover_device_registration(self):
        """Adopt or register a hardware id the BOSH service accepts (0.9.35)."""
        if self._in_device_recovery:
            return False
        self._in_device_recovery = True
        try:
            try:
                registered = normalize_hardware_id(self.fetch_hardware_id())
            except Exception:
                registered = ""
            if registered:
                if registered == normalize_hardware_id(self.hardware):
                    # Already THE registered device: the 400 has another
                    # cause -- do not mask it behind a pointless retry.
                    return False
                self.hardware = registered
                self._notify_hardware()
                return True
            registered_ok = self._register_hardware()
            self._hw_registered_now = bool(registered_ok)
            return registered_ok
        finally:
            self._in_device_recovery = False

    # --- Download (pytolino-derived): fetch an uploaded ebook back ---------

    def download(self, deliverable_id):
        """Download one book from the cloud; returns (content_bytes, metadata)."""
        info_url = (BASE_URL + "/cloud/downloadinfo/%s/%s/type/external-download"
                    % (_url_quote(str(deliverable_id)),
                       _url_quote(str(deliverable_id))))
        info = self._request(info_url)
        content_url = (info.get("DownloadInfo") or {}).get("contentUrl")
        if not content_url:
            raise TolinoApiError("DownloadInfo response had no contentUrl.")
        request = Request(content_url, headers={
            "User-Agent": BROWSER_USER_AGENT,
            "t_auth_token": self.access,
            "hardware_id": self.hardware,
            "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
                content = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise TolinoApiError("Tolino download failed: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)))
        # Metadata comes from the inventory record.
        for item in self.inventory():
            if isinstance(item, dict) and str(
                    item.get("deliverableId") or item.get("deliverable_id") or ""
                    ) == str(deliverable_id):
                return content, item
        return content, {}

    # --- Collections & read state (pytolino-derived sync-data patches) -----

    SYNC_DATA_URL = BASE_URL + "/sync-data?paths=publications"

    def _sync_patch(self, payload):
        data = self._request(self.SYNC_DATA_URL, "PATCH", payload,
                             content_type="application/json")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _tag_patch(op, book_id, name, category, revision=None):
        value = {"modified": int(time.time() * 1000),
                 "name": name, "category": category}
        if revision is not None:
            value["revision"] = revision
        return {"op": op, "value": value,
                "path": "/publications/%s/tags" % book_id}

    def get_sync_data(self):
        """Return (revision, patches) describing tags/collections state."""
        data = self._sync_patch({"revision": None, "patches": []})
        return data.get("revision"), data.get("patches") or []

    def add_to_collection(self, book_id, collection_name):
        """Add an uploaded/purchased book to a named collection."""
        patch = self._tag_patch("add", book_id, collection_name, "collection")
        data = self._sync_patch({"revision": None, "patches": [patch]})
        return data.get("revision")

    def remove_from_collection(self, book_id, collection_name):
        """Remove a book from a named collection (the book itself stays)."""
        revision, patches = self.get_sync_data()
        existing = next((
            item for item in patches
            if isinstance(item, dict)
            and "/publications/%s/tags" % book_id in str(item.get("path", ""))
            and isinstance(item.get("value"), dict)
            and item["value"].get("category") == "collection"
            and item["value"].get("name") == collection_name
        ), None)
        if existing is None:
            raise TolinoApiError(
                "Book %s is not part of collection %r." % (book_id, collection_name))
        patch = self._tag_patch(
            "remove", book_id, collection_name, "collection",
            revision=existing["value"].get("revision"))
        data = self._sync_patch({"revision": revision, "patches": [patch]})
        return data.get("revision")

    def mark_read(self, book_id, finished=True):
        """Mark a book as finished (or unmark it) via the system tag patch."""
        name = "collection_finished_readings_name"
        if finished:
            patch = self._tag_patch("add", book_id, name, "system")
            data = self._sync_patch({"revision": None, "patches": [patch]})
            return data.get("revision")
        revision, patches = self.get_sync_data()
        existing = next((
            item for item in patches
            if isinstance(item, dict)
            and "/publications/%s/tags" % book_id in str(item.get("path", ""))
            and isinstance(item.get("value"), dict)
            and item["value"].get("category") == "system"
            and item["value"].get("name") == name
        ), None)
        if existing is None:
            raise TolinoApiError("Book %s is not marked as finished." % book_id)
        patch = self._tag_patch(
            "remove", book_id, name, "system",
            revision=existing["value"].get("revision"))
        data = self._sync_patch({"revision": revision, "patches": [patch]})
        return data.get("revision")

    def collection_pairs(self):
        """Return the {(deliverable, collection)} pairs set in the cloud.

        Reads sync-data once so the series reconcile (0.9.45) only has to
        PATCH genuinely missing assignments.
        """
        _revision, patches = self.get_sync_data()
        pairs = set()
        for item in patches:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, dict) or value.get("category") != "collection":
                continue
            parts = str(item.get("path") or "").split("/")
            if (len(parts) != 4 or parts[1] != "publications"
                    or parts[3] != "tags" or not parts[2]):
                continue
            pair = (str(parts[2]), str(value.get("name") or ""))
            if str(item.get("op") or "add") == "remove":
                pairs.discard(pair)
            else:
                pairs.add(pair)
        return pairs

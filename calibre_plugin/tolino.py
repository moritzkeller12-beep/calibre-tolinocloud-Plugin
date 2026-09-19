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


# --- crypto-js-compatible AES (stdlib only) --------------------------------#
#
# The Tolino web reader encrypts its refresh token / user infos with
# CryptoJS.AES.encrypt(value, VERSION.PHRASE) and stores the Base64 result in
# IndexedDB (keys "userToken" / "userInfos"). CryptoJS uses the OpenSSL
# "Salted__" KDF: EVP_BytesToKey with MD5 and a random 8-byte salt, AES-256-CBC.
# The passphrase (PHRASE) is currently an empty string; we accept any phrase
# so a future change on the reader side keeps working.

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


def _aes_encrypt_block(block, words, rounds):
    """Encrypt one 16-byte block with AES."""
    return _state_to_block(_aes_rounds(_state_from_block(block), words, rounds))


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
    """Decrypt a CryptoJS.AES.encrypt Base64 blob; return "" on any mismatch.

    CryptoJS output is OpenSSL-formatted: base64("Salted__" + salt(8) +
    AES-256-CBC(PKCS7, key/iv = EVP_BytesToKey(MD5, passphrase, salt))).
    """
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
    """Yield (key, value) pairs from an .ldb/.sst table file.

    .ldb table files store records in blocks that are usually Snappy-
    compressed (a raw scan would miss nearly everything). We parse the footer,
    decompress each data block and walk the key/value prefixes.
    """
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
    """Yield (key, value) pairs from one decompressed .ldb data block.

    Entries: shared(varint) non_shared(varint) value_len(varint)
    then delta-key bytes and the value. We reconstruct keys with a shared-
    prefix buffer and tolerate restart-array noise at block end.
    """
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


def _sqlite_snapshot_copy(db_path):
    """Copy db + WAL/journal next to it so a live Firefox can be read.

    Reading the main file with immutable=1 ignores the write-ahead log, so
    recently written rows are invisible. A temporary snapshot of db+WAL makes
    them visible without locking or touching the original.
    """
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


def _snappy_uncompress_wrapper(data):
    return _snappy_uncompress(data) if data else b""


def _lsng_value_text(value, conversion=0, compression=0):
    """Decode one LSNG value from its conversion/compression metadata.

    The modern Firefox schema stores the value encoding in two integer
    columns, not in a prefix byte:
    - conversion_type: 0 = the BLOB holds raw UTF-16LE code units,
      1 (UTF16_UTF8) = the BLOB holds UTF-8 text,
    - compression_type: 0 = uncompressed, 1 = Snappy-compressed.
    Legacy databases sometimes carry a 0x01/0x02 prefix byte inside the
    value instead; those prefixes are handled as a fallback.
    """
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
    """Read one LSNG per-origin data.sqlite / ls-archive.sqlite database.

    Schema (Firefox dom/localstorage/ActorsParent.cpp, CreateDataTable):
      data(key TEXT PRIMARY KEY, utf16_length INTEGER, conversion_type INTEGER,
           compression_type INTEGER, last_access_time INTEGER, value BLOB)
    conversion_type 0 = raw UTF-16LE units, 1 = UTF-8; compression_type
    1 = Snappy. Keys are stored without the origin (the file IS per-origin).
    """
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
    """Read localStorage from a Firefox profile across all known layouts.

    Modern Firefox (LSNG) keeps localStorage in per-origin databases at
    storage/default/<origin>/ls/data.sqlite plus a cold store ls-archive.sqlite
    in the storage root; the legacy webappsstore.sqlite only survives as a
    disabled-by-default shadow database. Its real schema is
    webappsstore2(originAttributes, originKey, scope, key, value).
    All reads use snapshot copies, so the browser may keep running.
    """
    results = {}
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
                results["%s/%s" % (origin_text, key)] = _lsng_value_text(
                    value, conversion or 0, compression or 0)
    # 3) Legacy/shadow webappsstore.sqlite (webappsstore2 schema).
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
            results["%s/%s" % (origin_text, key)] = _lsng_value_text(value)
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
    """Collect "origin/key" pairs from a sessionstore JSON tree.

    SessionStore serializes DOM sessionStorage as nested mappings keyed by
    origin (any depth), whose values are flat dicts of name -> value.
    """
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
    """Best-effort read of DOM sessionStorage from Firefox sessionstore files.

    Firefox keeps sessionStorage inside the mozLZ4-compressed sessionstore
    JSON (sessionstore-backups/recovery.jsonlz4 and friends), not in a
    SQLite database. Values are keyed "origin/key"; nothing outside Tolino
    origins is returned.
    """
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
    """Read Tolino-relevant cookie names/values from cookies.sqlite.

    Values of non-Tolino hosts are never returned. Chromium cookie values are
    encrypted at rest and are therefore not read here.
    """
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
    """Scan a Chromium IndexedDB level directory for Tolino records.

    The web reader keeps userToken/userInfos in the IndexedDB database
    "tolino-user" (origin webreader.mytolino.com), stored by Chromium in
    .ldb/.log LevelDB files with the origin encoded as UTF-16LE inside the
    keys. We therefore search both UTF-8 and UTF-16LE origin encodings,
    collect every record whose raw bytes carry a Tolino origin and let the
    token matcher inspect the (possibly AES-encrypted) values.
    """
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
    """Extract refresh_token and hardware_id from a storage snapshot.

    Handles plain strings, JSON bundles and the Tolino web reader's real
    format: CryptoJS-AES-encrypted "userToken"/{"refresh":...} and
    "userInfos"/JSON({userId, devKey, hardwareId}) blobs (passphrase from
    the reader's src/config.json, currently the empty string).
    Never logs token values.
    """
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
    third-party module. Files are read raw (no database locks), so the browser
    may still be running. When ``diagnose`` is true, returns a third element:
    a redacted list describing what was scanned (no token values).
    """
    notes = []
    checked = []
    all_keys = {}
    refresh_token = None
    hardware_id = None
    for path in _find_browser_storage_paths():
        exists = os.path.isdir(path)
        checked.append("%s%s" % (path, "" if exists else "  (fehlt)"))
        if not exists:
            continue
        storage = _collect_storage_under(path, notes)
        if storage:
            all_keys.update(storage)
            refresh_token, hardware_id = _extract_tokens_from_storage(storage)
            if refresh_token and hardware_id:
                return (refresh_token, hardware_id, notes) if diagnose \
                    else (refresh_token, hardware_id)
    if not refresh_token or not hardware_id:
        # Tokens may be split across browser profiles; try the combined set.
        refresh_token, hardware_id = _extract_tokens_from_storage(all_keys)
        if refresh_token and hardware_id:
            return (refresh_token, hardware_id, notes) if diagnose \
                else (refresh_token, hardware_id)
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
        "scope": partner["scope"],
    }
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
                "scope": self.partner["scope"],
            }
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

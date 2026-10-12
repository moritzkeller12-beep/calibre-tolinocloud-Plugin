import json
import mimetypes
import os
import re
import shutil
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


# Werte-Namen, unter denen der Web Reader Zugang und Geraet ablegt.
TOKEN_VALUE_KEYS = ("refresh_token", "t_auth_token", "refreshToken", "refresh-token")
HARDWARE_VALUE_KEYS = ("hardware_id", "hardwareId", "device_id", "deviceId")

# Passphrases the web reader used for its CryptoJS blobs (VERSION.PHRASE in
# src/config.json). Currently an empty string; keep candidates so a future
# change on the reader side keeps the extractor working.
READER_AES_PHRASES = ("",)


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


def _refresh_token_iat_sort_key(value):
    issued = _refresh_token_iat(value)
    return issued if issued is not None else 0.0


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


def _exchange_fresh_response(partner_id, hardware, candidate, data):
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
                    partner_id, hardware_id, candidate, data)
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


def try_live_grab_first(partner_id, hardware, timeout=12):
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


def _keycloak_assisted_login(partner_id, hardware, timeout=OAUTH_STATE_TTL,
                             progress=None):
    """Browser sign-in for partners on the shared Web Reader.

    Reads the reader's CURRENT token out of the plugin's own Chromium
    window. Der Tausch passiert in der Seite selbst, weil der Token-Endpunkt
    den eigenen POST des Plugins mit Bot-Schutz (WAF 403) abweist.
    """
    return grab_live_refresh(partner_id, hardware, timeout=timeout,
                             progress=progress)


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


# curl_cffi (if installed) impersonates Chrome's TLS fingerprint; plain
# urllib/curl handshakes are rejected by the bot protection in front of some
# partner token endpoints (HTTP 403 without an OAuth error body).
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
        # Ein Lesevorgang der sync-data reicht, bis etwas geschrieben wird:
        # Lese-Status und Sammlungen brauchen dieselben Daten (0.15.0).
        self._sync_cache = None

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
                    "im Web Reader (Bücherliste) neu an und starte danach "
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
        fallthrough = object()

        def _try_transport(transport, post):
            """Run one optional transport, falling through on failure.

            Returns the decoded payload or the `fallthrough` sentinel when
            the next transport -- and finally urllib -- should be tried.
            A 403 bot-protection page also falls through; a real OAuth/API
            error is raised/handled by _error.
            """
            self.last_transport = transport
            try:
                status, raw = post()
            except Exception:
                return fallthrough
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
            # (Feldbefund 0.9.38: System-curl laeuft dauerhaft in die 403,
            # urllib mit den Browser-Headern nicht).
            return fallthrough

        if data is not None and url == self.partner.get("token_url"):
            session = _impersonate_session()
            if session is not None:
                outcome = _try_transport(
                    "curl_cffi",
                    lambda: _http_post_via_curl_cffi(
                        session, url, body or b"", headers, self.timeout))
                if outcome is not fallthrough:
                    return outcome
            if _curl_binary() is not None:
                outcome = _try_transport(
                    "curl",
                    lambda: _http_post_via_curl(
                        url, body or b"", headers, self.timeout))
                if outcome is not fallthrough:
                    return outcome

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

    # --- In-place metadata: update an existing book via /meta -----------
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

    # --- Device list: resolve the real hardware ID ----------------------

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

    # --- Download: fetch an uploaded ebook back -------------------------

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

    # --- Collections & read state via sync-data patches -----------------

    SYNC_DATA_URL = BASE_URL + "/sync-data?paths=publications"

    def _sync_patch(self, payload):
        # Jeder Schreibvorgang macht den Zwischenspeicher ungültig: danach
        # wird wieder gelesen, damit keine veraltete Revision weitergereicht
        # wird.
        self._sync_cache = None
        data = self._request(self.SYNC_DATA_URL, "PATCH", payload,
                             content_type="application/json")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _tag_patch(op, book_id, name, category, revision=None, tag_id=None):
        """Ein Marken-Patch -- beim Entfernen MIT Marken-ID im Pfad.

        Feldbefund 0.15.3: „tolino http 400 missing mandatory unitid
        fragment inside property path for remove“. Der Server identifiziert
        die zu löschende Marke über ihre ID, nicht über den Namen:
        ``/publications/<Buch-ID>/tags/<Marken-ID>``. Beim Anlegen vergibt
        er die ID selbst, dort bleibt der Pfad ohne ID.
        """
        value = {"modified": int(time.time() * 1000),
                 "name": name, "category": category}
        if revision is not None:
            value["revision"] = revision
        path = "/publications/%s/tags" % book_id
        if op == "remove":
            if not str(tag_id or "").strip():
                raise TolinoApiError(
                    "Zum Entfernen fehlt die Marken-ID der Cloud-Marke "
                    "(%s / %s)." % (book_id, name))
            path = "%s/%s" % (path, tag_id)
        return {"op": op, "value": value, "path": path}

    @classmethod
    def _tag_id_from_patch(cls, item):
        """Marken-ID aus einem gelesenen Patch -- leer, wenn der Server
        keine mitgeschickt hat."""
        parts = cls._tag_path_parts((item or {}).get("path"))
        return parts[1] if parts else ""

    def get_sync_data(self):
        """Return (revision, patches) describing tags/collections state."""
        data = self._sync_patch({"revision": None, "patches": []})
        return data.get("revision"), data.get("patches") or []

    def sync_snapshot(self):
        """(revision, patches) aus EINEM Lesevorgang, mehrfach nutzbar.

        Sammlungen und Lese-Status liegen in denselben sync-data-Daten;
        ohne Zwischenspeicher würde derselbe Stand zweimal geholt. Jeder
        Schreibvorgang verwirft ihn wieder (siehe `_sync_patch`).
        """
        if self._sync_cache is None:
            data = self._sync_patch({"revision": None, "patches": []})
            self._sync_cache = (data.get("revision"),
                                data.get("patches") or [])
        return self._sync_cache

    @staticmethod
    def _tag_path_parts(path):
        """(Buch-ID, Marken-ID) aus einem Tag-Pfad -- sonst None.

        Der Server schickt Marken als
        ``/publications/<Buch-ID>/tags/<Marken-ID>`` (die Marken-ID
        vergibt der Server), der Plugin schreibt beim Setzen
        ``/publications/<Buch-ID>/tags``. Beide Formen müssen gelesen
        werden können -- sonst findet der Bestandsvergleich keine
        Gelesen-Marke in der Cloud (0.15.1).
        """
        parts = str(path or "").split("/")
        if (len(parts) < 4 or parts[1] != "publications"
                or parts[3] != "tags" or not parts[2]):
            return None
        return str(parts[2]), str(parts[4] if len(parts) > 4 else "")

    @classmethod
    def _tag_pairs(cls, patches, category, name=None):
        """{(Buch-ID, Markenname)} aus einer sync-data-Patchliste.

        `remove`-Patches nehmen das Paar wieder heraus -- der letzte
        Eintrag einer Buch/Marken-Kombination entscheidet.
        """
        pairs = set()
        for item in patches:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, dict) or value.get("category") != category:
                continue
            if name is not None and value.get("name") != name:
                continue
            parts = cls._tag_path_parts(item.get("path"))
            if parts is None:
                continue
            pair = (parts[0], str(value.get("name") or ""))
            if str(item.get("op") or "add") == "remove":
                pairs.discard(pair)
            else:
                pairs.add(pair)
        return pairs

    def read_ids(self):
        """Bücher, die im Konto als gelesen markiert sind (0.15.0).

        Quelle ist die System-Marke des Web Readers -- dieselbe, die
        `mark_read_many` setzt. Damit kann der Bestandsvergleich die
        Spalte „Gelesen“ vorbelegen.
        """
        _revision, patches = self.sync_snapshot()
        return {book_id for book_id, _name
                in self._tag_pairs(patches, "system", self.READ_TAG)}

    READ_TAG = "collection_finished_readings_name"

    @staticmethod
    def _find_tag(patches, book_id, name, category):
        """Find one existing tag of a book in a sync-data patch list."""
        needle = "/publications/%s/tags" % book_id
        for item in patches:
            if not isinstance(item, dict):
                continue
            value = item.get("value")
            if not isinstance(value, dict):
                continue
            if (needle in str(item.get("path") or "")
                    and value.get("category") == category
                    and value.get("name") == name):
                return item
        return None

    def _current_revision(self, revision):
        """Revision fuer einen Schreibvorgang: die aktuelle, nie None.

        Ein PATCH ohne Revision kann den vorhandenen Stand ersetzen und
        damit fremde Marken (Lese-Status, andere Sammlungen) verlieren.
        Die Revision wird deshalb gelesen und mitgeschickt.
        """
        if revision is not None:
            return revision
        revision, _patches = self.sync_snapshot()
        return revision

    def add_to_collection(self, book_id, collection_name, revision=None):
        """Add an uploaded/purchased book to a named collection.

        `revision` kann aus dem vorigen Aufruf weitergereicht werden, damit
        mehrere Zuordnungen nur eine Cloud-Anfrage zum Lesen brauchen.
        """
        patch = self._tag_patch("add", book_id, collection_name, "collection")
        data = self._sync_patch(
            {"revision": self._current_revision(revision), "patches": [patch]})
        return data.get("revision")

    def remove_from_collection(self, book_id, collection_name):
        """Remove a book from a named collection (the book itself stays)."""
        revision, patches = self.get_sync_data()
        existing = self._find_tag(patches, book_id, collection_name, "collection")
        if existing is None:
            raise TolinoApiError(
                "Book %s is not part of collection %r." % (book_id, collection_name))
        patch = self._tag_patch(
            "remove", book_id, collection_name, "collection",
            revision=existing["value"].get("revision"),
            tag_id=self._tag_id_from_patch(existing))
        data = self._sync_patch({"revision": revision, "patches": [patch]})
        return data.get("revision")

    def mark_read(self, book_id, finished=True):
        """Mark a book as finished (or unmark it) via the system tag patch.

        Es wird nur die eine System-Marke gesetzt bzw. entfernt -- der
        restliche Cloud-Stand (Sammlungen, andere Marken) bleibt, wie er ist.
        """
        return self.mark_read_many([book_id], finished=finished)

    def mark_read_many(self, book_ids, finished=True):
        """Set (or clear) the finished mark for several books at once.

        Ein Lesevorgang, ein PATCH: alle Bücher bekommen dieselbe Revision.
        Bereits gesetzte Marken werden nicht erneut geschrieben, fehlende
        beim Entfernen übersprungen.
        """
        ids = [str(book_id) for book_id in book_ids or [] if str(book_id or "").strip()]
        if not ids:
            raise TolinoApiError("Marking as read requires at least one book id.")
        revision, patches = self.sync_snapshot()
        updates = []
        for book_id in ids:
            existing = self._find_tag(patches, book_id, self.READ_TAG, "system")
            if finished:
                if existing is None:
                    updates.append(self._tag_patch(
                        "add", book_id, self.READ_TAG, "system"))
            elif existing is not None:
                updates.append(self._tag_patch(
                    "remove", book_id, self.READ_TAG, "system",
                    revision=existing["value"].get("revision"),
                    tag_id=self._tag_id_from_patch(existing)))
        if not updates:
            return revision
        data = self._sync_patch({"revision": revision, "patches": updates})
        return data.get("revision")

    def collection_pairs(self):
        """Return the {(deliverable, collection)} pairs set in the cloud.

        Reads sync-data once so the series reconcile (0.9.45) only has to
        PATCH genuinely missing assignments.
        """
        _revision, patches = self.sync_snapshot()
        return self._tag_pairs(patches, "collection")

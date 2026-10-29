"""Decode and normalize token candidates read from Tolino's Web Reader."""
import base64
import functools
import json
import re
import time
from urllib.parse import unquote as _unquote

from .browser_crypto import cryptojs_decrypt

# Values used by the Web Reader for credentials and device identity.
TOKEN_VALUE_KEYS = ("refresh_token", "t_auth_token", "refreshToken", "refresh-token")
HARDWARE_VALUE_KEYS = ("hardware_id", "hardwareId", "device_id", "deviceId")

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
        value = float(payload.get("iat"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _jwt_shaped(text):
    """True when the text itself looks like a JWT (three base64url parts)."""
    text = str(text or "")
    parts = text.split(".")
    if len(parts) != 3 or len(text) < 80:
        return False
    return all(part and re.fullmatch(r"[A-Za-z0-9_\-]+", part)
               for part in parts)


def _undatable_shape(token):
    """Structural fingerprint of an undatable candidate – never its value."""
    text = str(token or "")
    parts = text.split(".")
    shape = "%d %s, %d Zeichen" % (
        len(parts), "Teil" if len(parts) == 1 else "Teile", len(text))
    if len(parts) == 3:
        shape += (", Payload ohne iat" if _jwt_payload(text)
                  else ", Payload unlesbar")
    return shape


def _candidate_age_note(candidates):
    """Return a redacted age/shape note for candidate error messages."""
    best = None
    for token in candidates or ():
        issued = _refresh_token_iat(token)
        if issued is not None and (best is None or issued > best):
            best = issued
    if best is None:
        token = next(iter(candidates or ()), None)
        if token is None:
            return ""
        return ("Alter des gefundenen Zugangs unbekannt (%s)"
                % _undatable_shape(token))
    return "Der gefundene Zugang war %d Sekunden alt." % max(
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
    try:
        plain = cryptojs_decrypt(text, "")
    except Exception:
        return []
    plain = (plain or "").strip()
    return [plain] if plain else []


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


@functools.lru_cache(maxsize=256)
def _unwrap_refresh_candidate(token, max_depth=3):
    """Recover the JWT behind a wrapped, undatable candidate.

    Die Poll-Schleife sieht dieselben Blobs alle paar Sekunden erneut; die
    Entschlüsselung ist rein und darf deshalb zwischengespeichert werden.
    """
    text = str(token or "")
    if not text or _jwt_shaped(text):
        return text
    frontier = [text]
    seen = {text}
    for _depth in range(max_depth):
        nxt = []
        for value in frontier:
            # CryptoJS/OpenSSL AES layer (reader userToken/userInfos).
            for plain in _decrypt_reader_blob(value):
                plain = plain.strip()
                if not plain:
                    continue
                if _jwt_shaped(plain):
                    # Trusted plaintext under a refresh-ish key, even without iat.
                    return plain
                if plain.startswith("U2FsdGVk"):
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
                # Trusted raw plaintext: an opaque real token beats ciphertext.
                if len(plain) > 20 and plain.isprintable():
                    return plain
            # Structural wrappers (JSON quotes / percent / base64).
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
    """Unwrap the refresh candidates of ONE grab result."""
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

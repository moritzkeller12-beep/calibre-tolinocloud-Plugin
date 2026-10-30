"""Decode and normalize token candidates read from Tolino's Web Reader."""
import base64
import json
import re
import time

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


def _decrypt_reader_blob(value):
    """Plaintext of a CryptoJS "Salted__" reader blob ("" when none)."""
    text = str(value or "")
    if not text or ("Salted__" not in text
                    and not text.lstrip().startswith("U2FsdGVk")):
        return ""
    try:
        return (cryptojs_decrypt(text, "") or "").strip()
    except Exception:
        return ""


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
    plain = _decrypt_reader_blob(value)
    if not plain:
        return ""
    try:
        parsed = json.loads(plain)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for name in HARDWARE_VALUE_KEYS:
        nested = parsed.get(name)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _unwrap_refresh_candidate(token, max_depth=3):
    """Recover the JWT behind an encrypted, undatable candidate."""
    text = str(token or "")
    if not text or _jwt_shaped(text):
        return text
    current = text
    for _depth in range(max_depth):
        plain = _decrypt_reader_blob(current)
        if not plain:
            break
        if _jwt_shaped(plain):
            # Trusted plaintext under a refresh-ish key, even without iat.
            return plain
        if plain.startswith("U2FsdGVk"):
            current = plain
            continue
        if plain.startswith("{"):
            nested = _refresh_field_text(plain)
            if not nested:
                break
            if _jwt_shaped(nested):
                return nested
            if nested.startswith("U2FsdGVk"):
                current = nested
                continue
            if (len(nested) > 20 and nested.isprintable()
                    and not nested.startswith(("{", "["))):
                return nested
            break
        if plain.startswith("["):
            break
        # Trusted raw plaintext: an opaque real token beats ciphertext.
        if len(plain) > 20 and plain.isprintable():
            return plain
        break
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

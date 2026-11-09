"""Decode and normalize token candidates read from Tolino's Web Reader."""
import base64
import json
import re
import time

from .browser_crypto import cryptojs_decrypt
from .errors import TolinoAuthError

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
    except ValueError:
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
    if not text or not text.lstrip().startswith("U2FsdGVk"):
        return ""
    return cryptojs_decrypt(text).strip()


def _json_field_text(value, names):
    """First non-empty string field from a JSON object; '' when none."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for name in names:
        nested = parsed.get(name)
        if isinstance(nested, str) and nested.strip():
            return nested.strip()
    return ""


def _refresh_field_text(json_text):
    """The refresh-ish string field of a decrypted reader JSON bundle."""
    return _json_field_text(json_text, ("refresh",) + TOKEN_VALUE_KEYS) or None


def _hardware_from_reader_blob(value):
    """hardwareId from a decrypted userInfos blob; '' when there is none."""
    plain = _decrypt_reader_blob(value)
    return _json_field_text(plain, HARDWARE_VALUE_KEYS) if plain else ""


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
        if plain.startswith("{"):
            plain = _refresh_field_text(plain) or ""
        if _jwt_shaped(plain):
            return plain
        if plain.startswith("U2FsdGVk"):
            current = plain
            continue
        if (len(plain) > 20 and plain.isprintable()
                and not plain.startswith(("{", "["))):
            return plain
        break
    return text


def _normalize_live_grab(grabbed):
    """Unwrap the refresh candidates of ONE grab result.

    Fehlt PyCryptodome, ist nur ein verschlüsselter Kandidat unlesbar.
    Ein daneben gefundener Klartext-Token bleibt brauchbar; die
    Hardware-ID ist ohnehin optional. Der Fehler wird deshalb erst
    gemeldet, wenn kein lesbarer Refresh-Kandidat übrig bleibt.
    """
    if not isinstance(grabbed, dict):
        return grabbed
    refresh = []
    decrypt_error = None
    for token in grabbed.get("refresh") or ():
        try:
            unwrapped = _unwrap_refresh_candidate(token)
        except TolinoAuthError as exc:
            decrypt_error = decrypt_error or exc
            continue
        if unwrapped and unwrapped not in refresh:
            refresh.append(unwrapped)
    if decrypt_error is not None and not refresh:
        raise decrypt_error
    hardware = []
    for value in grabbed.get("hardware") or ():
        value = str(value or "").strip()
        if value.startswith("U2FsdGVk"):
            try:
                value = _hardware_from_reader_blob(value)
            except TolinoAuthError:
                # Die Geräte-ID ist optional; ohne Decoder bleibt sie leer.
                continue
        if value and value not in hardware:
            hardware.append(value)
    return dict(grabbed, refresh=refresh, hardware=hardware)

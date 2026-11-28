"""Payload und Zeiten eines JWT lesen -- ohne Signaturprüfung.

Gebraucht werden nur ``iss`` (Buchhändler) und ``iat``/``exp`` (Alter und
Laufzeit eines Tokens). Der Token selbst wird nie verändert oder geloggt.
"""
import base64
import json


def payload(token):
    """Decode the unverified payload of a JWT-style token (or None)."""
    try:
        parts = str(token or "").split(".")
        if len(parts) != 3 or not parts[1]:
            return None
        segment = parts[1]
        segment += "=" * (-len(segment) % 4)
        data = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
        return data if isinstance(data, dict) else None
    except ValueError:
        return None


def _claim_time(token, name):
    """Numerischer Zeit-Claim (``iat``/``exp``) oder None."""
    value = (payload(token) or {}).get(name)
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def issued_at(token):
    """Ausstellungszeit des Tokens (unix seconds) oder None."""
    return _claim_time(token, "iat")


def expires_at(token):
    """Ablaufzeit des Tokens (unix seconds) oder None."""
    return _claim_time(token, "exp")


def lifetime_seconds(token):
    """Laufzeit aus ``exp - iat``; None, wenn unbrauchbar.

    Beide Werte stammen vom Server -- die Differenz ist damit unabhängig
    von der lokalen Uhr, anders als ``exp`` allein.
    """
    expires, issued = expires_at(token), issued_at(token)
    if expires is None or issued is None or expires <= issued:
        return None
    return expires - issued

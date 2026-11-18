"""Payload eines JWT lesen -- ohne Signaturprüfung.

Gebraucht werden nur ``iss`` (Buchhändler) und ``iat`` (Alter eines
Refresh-Tokens). Der Token selbst wird nie verändert oder geloggt.
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

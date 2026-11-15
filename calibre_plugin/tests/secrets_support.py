"""Zugangsdaten fuer echte Laeufe kommen ausschliesslich aus der Umgebung.

Die Project-Secrets heissen ``refresh_token`` und ``hardware_id`` (klein
geschrieben). Dieses Modul loest sie auf und meldet fehlende oder
offensichtlich unbrauchbare Werte mit einer klaren Meldung. Die Werte
selbst tauchen in keiner Meldung und keinem Log auf.
"""
import os

from ..errors import TolinoAuthError

TOKEN_ENV = "refresh_token"
HARDWARE_ENV = "hardware_id"

# Kuerzere Werte sind kein tolino-Token, sondern ein Platzhalter.
MIN_TOKEN_LENGTH = 20


def credentials_from_env(environ=None):
    """``(refresh_token, hardware_id)`` aus der Umgebung.

    Raises ``TolinoAuthError`` with a user-facing message when a secret is
    missing or obviously unusable -- never with the secret itself.
    """
    values = os.environ if environ is None else environ
    token = str(values.get(TOKEN_ENV) or "").strip()
    hardware = str(values.get(HARDWARE_ENV) or "").strip()
    if not token:
        raise TolinoAuthError(
            "Kein tolino-Zugang in der Umgebung: das Secret «%s» fehlt. "
            "Bitte in den Project-Secrets hinterlegen und den Lauf neu "
            "starten." % TOKEN_ENV, code="A01")
    if len(token) < MIN_TOKEN_LENGTH or any(part.isspace() for part in token):
        raise TolinoAuthError(
            "Das Secret «%s» sieht nicht wie ein tolino-Token aus: zu kurz "
            "oder mit Leerzeichen." % TOKEN_ENV, code="A01",
            detail="Laenge %d" % len(token))
    if hardware and not all(part.isalnum() or part == "-" for part in hardware):
        raise TolinoAuthError(
            "Das Secret «%s» enthaelt unerwartete Zeichen." % HARDWARE_ENV,
            code="A01")
    return token, hardware

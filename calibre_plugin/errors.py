"""Gemeinsame Fehlerklassen fuer Tolino-Aufrufe.

Eigenes Modul, damit ``sync_data`` dieselben Klassen nutzen kann wie
``tolino``, ohne von ihm zu importieren.
"""


class TolinoError(Exception):
    """Fehler mit Code (z. B. A01), kurzem Nutzertext und Detail fürs Log.

    str(exc) bleibt der vollständige Text (auch Serverantwort) und dient
    der internen Auswertung; der Dialog zeigt nur `short` (falls gesetzt)
    plus Code. Alles Technische landet über report_error() im Calibre-Log.
    """
    code = "E99"

    def __init__(self, message="", *args, code=None, short=None, detail=""):
        super().__init__(message, *args)
        if code:
            self.code = code
        self.short = short
        self.detail = detail


class TolinoAuthError(TolinoError):
    pass


class TolinoApiError(TolinoError):
    pass

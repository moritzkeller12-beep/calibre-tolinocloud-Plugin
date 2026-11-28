"""Tolino shops and their stable partner-ID mappings."""
from urllib.parse import urlsplit

from .jwt import payload

WEBREADER_URL = "https://webreader.mytolino.com/library/"
WEBREADER_TOKEN_HEADERS = {
    "Origin": "https://webreader.mytolino.com",
    "Referer": "https://webreader.mytolino.com/",
}

WEBREADER_CLIENT_TYPE = "TOLINO_WEBREADER"
WEBREADER_CLIENT_VERSION = "5.2.0"

def _shop(name, reseller_id, token_url, client_id="webreader"):
    """Ein Buchhaendler-Datensatz; "reseller_id" ist Tolinos eigene Kennung."""
    return {"name": name, "reseller_id": reseller_id,
            "client_id": client_id, "token_url": token_url}


PARTNERS = {
    1: _shop("Thalia.de", "3",
             "https://www.thalia.de/auth/oauth2/token"),
    2: _shop("Thalia.at", "4",
             "https://www.thalia.at/auth/oauth2/token"),
    4: _shop("Books.ch / orellfuessli.ch", "8",
             "https://www.orellfuessli.ch/auth/oauth2/token"),
    5: _shop("Hugendubel.de", "13",
             "https://www.hugendubel.de/oauth/token",
             "4c20de744aa8b83b79b692524c7ec6ae"),
    6: _shop("Osiander.de", "23",
             "https://www.osiander.de/auth/oauth2/token"),
    7: _shop("Buecher.de", "30",
             "https://www.buecher.de/auth/oauth2/token"),
    8: _shop("eBook.de", "81",
             "https://www.ebook.de/oauth/token",
             "ebookde0501html5readerV0001"),
}


def _host(value):
    """Host aus einer URL oder einem blossen Hostnamen, ohne ``www.``."""
    text = str(value or "").strip()
    return (urlsplit(text if "//" in text else "//" + text).hostname
            or "").casefold().removeprefix("www.")


def partner_from_token(token):
    """Buchhändler aus dem ``iss``-Claim eines Tokens (None = unbekannt).

    Ein Issuer, den ``urlsplit()`` nicht parsen kann (etwa kaputte
    IPv6-Klammern), ist kein Buchhändler: der manuelle Rückfall bleibt so
    erreichbar, statt den Aufrufer mit einem ``ValueError`` zu verlassen.
    """
    try:
        host = _host((payload(token) or {}).get("iss"))
    except ValueError:
        return None
    return next((partner_id for partner_id, values in PARTNERS.items()
                 if host and _host(values["token_url"]) == host), None)


# Gemeinsame Meldung für Browser-Anmeldung und Handeingabe (Code B03).
UNKNOWN_SHOP = ("Im Zugang ist kein Buchhändler erkennbar. Bitte in den "
                "Einstellungen den Buchhändler wählen und den Zugang von Hand "
                "eintragen. ")

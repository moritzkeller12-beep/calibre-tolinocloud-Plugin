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

# "reseller_id" is sent to Tolino and is independent of this plugin's IDs.
PARTNERS = {
    1: {
        "name": "Thalia.de", "reseller_id": "3",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.de/auth/oauth2/token",
    },
    2: {
        "name": "Thalia.at", "reseller_id": "4",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/auth/oauth2/token",
    },
    4: {
        "name": "Books.ch / orellfuessli.ch",
        "reseller_id": "8", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.orellfuessli.ch/auth/oauth2/token",
    },
    5: {
        "name": "Hugendubel.de", "reseller_id": "13",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://www.hugendubel.de/oauth/token",
    },
    6: {
        "name": "Osiander.de", "reseller_id": "23",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token",
    },
    7: {
        "name": "Buecher.de", "reseller_id": "30",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.buecher.de/auth/oauth2/token",
    },
    8: {
        "name": "eBook.de", "reseller_id": "81",
        "client_id": "ebookde0501html5readerV0001",
        "scope": "e-publishing",
        "token_url": "https://www.ebook.de/oauth/token",
    },
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

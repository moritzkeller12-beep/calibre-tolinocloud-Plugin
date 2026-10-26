"""Tolino shops and their stable partner-ID mappings."""

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
        "reader_url": WEBREADER_URL,
    },
    2: {
        "name": "Thalia.at", "reseller_id": "4",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/auth/oauth2/token",
        "reader_url": WEBREADER_URL,
    },
    4: {
        "name": "Books.ch / orellfuessli.ch",
        "reseller_id": "8", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.orellfuessli.ch/auth/oauth2/token",
        "reader_url": WEBREADER_URL,
    },
    5: {
        "name": "Hugendubel.de", "reseller_id": "13",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://www.hugendubel.de/oauth/token",
        "reader_url": WEBREADER_URL,
    },
    6: {
        "name": "Osiander.de", "reseller_id": "23",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token",
        "reader_url": WEBREADER_URL,
    },
    7: {
        "name": "Buecher.de", "reseller_id": "30",
        "client_id": "webreader", "scope": "SCOPE_BOSH",
        "token_url": "https://www.buecher.de/auth/oauth2/token",
        "reader_url": WEBREADER_URL,
    },
    8: {
        "name": "eBook.de", "reseller_id": "81",
        "client_id": "ebookde0501html5readerV0001",
        "scope": "e-publishing",
        "token_url": "https://www.ebook.de/oauth/token",
        "reader_url": WEBREADER_URL,
    },
}

"""Browser-like request headers used by Tolino's urllib transport."""

CHROME_MAJOR = "153"

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36" % CHROME_MAJOR
)


def _browser_sec_headers():
    """Browser-consistent Sec-Fetch and Client-Hints headers."""
    return {
        "Accept": "*/*",
        "Accept-Language": "de-DE,de;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-CH-UA": '"Chromium";v="%s", "Not_A Brand";v="8"' % CHROME_MAJOR,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-GPC": "1",
    }

"""Browser-like request headers used by Tolino's urllib transport."""
import re


BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def _browser_sec_headers():
    """Browser-consistent Sec-Fetch and Client-Hints headers."""
    match = re.search(r"Chrome/(\d+)", BROWSER_USER_AGENT)
    major = match.group(1) if match else "153"
    return {
        "Accept": "*/*",
        "Accept-Language": "de-DE,de;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-CH-UA": '"Chromium";v="%s", "Not_A Brand";v="8"' % major,
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-GPC": "1",
    }

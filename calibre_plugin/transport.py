"""Optional HTTP transports and browser-like token request headers."""
import re
import shutil
import subprocess

# --- Optionaler curl-Transport -----------------------------------------------
# Der Token-Endpunkt fingerprintet TLS; curl/curl_cffi impersonieren
# einen Browser, urllib bleibt Fallback ohne Abhaengigkeiten.

# The token endpoint sits behind bot protection that also inspects the
# User-Agent; a plain "Calibre-Tolino-Plugin" UA is an easy flag.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36"
)


def _browser_sec_headers():
    """Browser-consistent Sec-Fetch/Client-Hints headers for token requests."""
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


def _compact_error_text(detail):
    """Collapse HTML bot-check pages to readable text for error messages."""
    text = re.sub(r"<[^>]+>", " ", detail or "")
    return re.sub(r"\s+", " ", text).strip()


_BOT_CHECK_MARKERS = (
    "zugriff geblockt", "access denied", "captcha", "cloudfront",
    "request blocked", "<!doctype html",
)

def _bot_check_detected(detail):
    """True when the response looks like a bot-protection page, not OAuth."""
    return any(marker in str(detail or "").casefold()
               for marker in _BOT_CHECK_MARKERS)


def _waf_block_response(status, raw):
    """True for a 403 bot-protection page at the token endpoint."""
    if status != 403:
        return False
    text = (raw.decode("utf-8", "replace") if isinstance(raw, bytes)
            else str(raw or ""))
    if not text.strip():
        return True  # leere 403 ohne OAuth-Body: WAF, nicht Keycloak
    return _bot_check_detected(text)

def _curl_binary():
    """Return a usable curl binary path, or None if curl is not installed."""
    return shutil.which("curl")


# curl_cffi (if installed) impersonates Chrome's TLS fingerprint; plain
# urllib/curl handshakes are rejected by the bot protection in front of some
# partner token endpoints (HTTP 403 without an OAuth error body).
def _impersonate_session():
    """Return a curl_cffi session that impersonates Chrome, or None."""
    try:
        from curl_cffi.requests import Session
    except Exception:
        return None
    try:
        session = Session(impersonate="chrome")
        if not callable(getattr(session, "post", None)):
            return None
        return session
    except Exception:
        return None


def _http_post_via_curl_cffi(session, url, body, headers, timeout):
    """POST via a curl_cffi session; returns (status:int, raw_bytes)."""
    response = session.post(
        url,
        data=body if isinstance(body, bytes) else str(body).encode("utf-8"),
        headers=dict(headers),
        timeout=timeout,
        allow_redirects=True,
    )
    return int(response.status_code), response.content or b""

def _http_post_via_curl(url, body, headers, timeout):
    """POST via curl stdin; returns (status:int, raw_bytes)."""
    binary = _curl_binary()
    if not binary:
        raise RuntimeError("curl not available")
    argv = [
        binary,
        "--silent",
        "--show-error",
        "--location",           # follow redirects like a browser would
        "--max-time", str(int(timeout)),
        "--write-out", "\n%{http_code}",
        "--url", url,
        "--request", "POST",
        "--data-binary", "@-",
    ]
    for key, value in headers.items():
        argv.extend(["--header", "%s: %s" % (key, value)])
    body_bytes = body if isinstance(body, bytes) else str(body).encode("utf-8")
    completed = subprocess.run(
        argv, input=body_bytes, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout + 5)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode, argv, output=completed.stdout,
            stderr=completed.stderr)
    output = completed.stdout
    # Split off the trailing status code written by --write-out.
    body_part, _, status_part = output.rpartition(b"\n")
    try:
        status = int(status_part.strip() or 0)
    except ValueError:
        status = 0
    if not status:
        status = 200 if completed.returncode == 0 else 0
    return status, body_part


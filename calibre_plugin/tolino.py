import json
import mimetypes
import os
import platform
import re
import sqlite3
import struct
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen


class TolinoError(Exception):
    pass


class TolinoAuthError(TolinoError):
    pass


class TolinoApiError(TolinoError):
    pass


PARTNERS = {
    3: {
        "name": "Thalia.de",
        "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.de/auth/oauth2/token",
        "auth_url": "https://www.thalia.de/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles",
    },
    4: {"name": "Thalia.at", "client_id": "webshop01",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.thalia.at/de.buch.appservices/api/4004/oauth2/token",
        "auth_url": "https://www.thalia.at/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles"},
    6: {"name": "Buch.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE"},
    8: {"name": "Books.ch / orellfuessli.ch", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.orellfuessli.ch/auth/oauth2/token",
        "auth_url": "https://www.orellfuessli.ch/auth/oauth2/autologin",
        "reader_url": "https://webreader.mytolino.com/library/",
        "token_headers": {
            "Origin": "https://webreader.mytolino.com",
            "Referer": "https://webreader.mytolino.com/",
        },
        "x_buchde.mandant_id": "37",
        "x_buchde.skin_id": "17",
        "client_type": "TOLINO_WEBREADER",
        "client_version": "5.2.0"},
    13: {
        "name": "Hugendubel.de",
        "client_id": "4c20de744aa8b83b79b692524c7ec6ae",
        "scope": "ebook_library",
        "token_url": "https://api.hugendubel.de/rest/oauth2/token",
        "auth_url": "https://www.hugendubel.de/oauth/authorize",
        "reader_url": "https://webreader.hugendubel.de/library/index.html",
    },
    23: {"name": "Osiander.de", "client_id": "webreader",
        "scope": "SCOPE_BOSH",
        "token_url": "https://www.osiander.de/auth/oauth2/token",
        "auth_url": "https://www.osiander.de/de.thalia.ecp.authservice.application/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/index.html#/mybooks/titles"},
    30: {"name": "Buecher.de", "client_id": "webshop01",
        "scope": "SCOPE_BOSH SCOPE_BUCHDE",
        "token_url": "https://www.buecher.de/oauth2/token",
        "auth_url": "https://www.buecher.de/oauth2/authorize",
        "reader_url": "https://webreader.mytolino.com/library/"},
}

BASE_URL = "https://bosh.pageplace.de/bosh/rest"
OAUTH_STATE_TTL = 300
TOLINO_READER_ORIGIN = "https://webreader.mytolino.com"


_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


def _find_browser_storage_paths():
    """Find browser storage directories for Chrome, Edge, Firefox on current platform."""
    paths = []
    system = platform.system()
    
    if system == "Windows":
        app_data = os.environ.get("LOCALAPPDATA", os.path.expanduser("~/AppData/Local"))
        chrome_paths = [
            os.path.join(app_data, "Google", "Chrome", "User Data", "Default"),
            os.path.join(app_data, "Microsoft", "Edge", "User Data", "Default"),
            os.path.join(app_data, "BraveSoftware", "Brave-Browser", "User Data", "Default"),
            os.path.join(app_data, "Opera Software", "Opera Stable"),
        ]
        firefox_paths = [
            os.path.join(os.environ.get("APPDATA", os.path.expanduser("~/AppData/Roaming")), "Mozilla", "Firefox", "Profiles"),
        ]
        paths.extend(chrome_paths)
        paths.extend(firefox_paths)
    
    elif system == "Darwin":  # macOS
        chrome_paths = [
            os.path.expanduser("~/Library/Application Support/Google/Chrome/Default"),
            os.path.expanduser("~/Library/Application Support/Microsoft Edge/Default"),
            os.path.expanduser("~/Library/Application Support/BraveSoftware/Brave-Browser/Default"),
        ]
        firefox_paths = [
            os.path.expanduser("~/Library/Application Support/Firefox/Profiles"),
        ]
        paths.extend(chrome_paths)
        paths.extend(firefox_paths)
    
    else:  # Linux and others
        chrome_paths = [
            os.path.expanduser("~/.config/google-chrome/Default"),
            os.path.expanduser("~/.config/chromium/Default"),
            os.path.expanduser("~/.config/microsoft-edge/Default"),
            os.path.expanduser("~/.config/brave/Default"),
            os.path.expanduser("~/.config/opera"),
        ]
        firefox_paths = [
            os.path.expanduser("~/.mozilla/firefox"),
            os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),
        ]
        paths.extend(chrome_paths)
        paths.extend(firefox_paths)
    
    return paths


def _read_chromium_local_storage(storage_path):
    """Read Local Storage data from Chromium-based browser (LevelDB format)."""
    try:
        import leveldb
        db_path = os.path.join(storage_path, "Local Storage", "leveldb")
        if not os.path.exists(db_path):
            return {}
        db = leveldb.LevelDB(db_path)
        results = {}
        for key, value in db.RangeIter():
            try:
                key_str = key.decode('utf-8')
                value_str = value.decode('utf-8')
                if key_str.startswith("https://webreader.mytolino.com"):
                    results[key_str] = value_str
            except (UnicodeDecodeError, AttributeError):
                continue
        return results
    except ImportError:
        pass
    except Exception:
        pass
    return {}


def _read_chromium_local_storage_sqlite(storage_path):
    """Read Local Storage from Chromium SQLite backup (fallback method)."""
    try:
        db_path = os.path.join(storage_path, "Local Storage", "https_webreader.mytolino.com_0.localstorage")
        if not os.path.exists(db_path):
            db_path = os.path.join(storage_path, "Local Storage", "https_webreader.mytolino.com_0.localstorage-journal")
        if not os.path.exists(db_path):
            return {}
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        results = {}
        cursor.execute("SELECT key, value FROM ItemTable")
        for row in cursor.fetchall():
            if row and len(row) >= 2:
                results[row[0]] = row[1]
        conn.close()
        return results
    except Exception:
        return {}


def _read_firefox_local_storage(profile_path):
    """Read Local Storage from Firefox (SQLite format)."""
    try:
        db_path = os.path.join(profile_path, "webappsstore.sqlite")
        if not os.path.exists(db_path):
            return {}
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        results = {}
        cursor.execute("SELECT origin, key, value FROM webappsstore2")
        for row in cursor.fetchall():
            if row and len(row) >= 3:
                origin, key, value = row[0], row[1], row[2]
                if "webreader.mytolino.com" in origin:
                    results[f"{origin}/{key}"] = value
        conn.close()
        return results
    except Exception:
        return {}


def _extract_tokens_from_storage(storage_data):
    """Extract refresh_token and hardware_id from browser storage data."""
    refresh_token = None
    hardware_id = None
    
    for key, value in storage_data.items():
        try:
            if isinstance(value, str):
                if "refresh_token" in key:
                    refresh_token = value
                elif "hardware" in key.lower() or "hardware_id" in key.lower():
                    hardware_id = value
        except Exception:
            continue
    
    return refresh_token, hardware_id


def scrape_browser_tokens():
    """
    Scrape refresh_token and hardware_id from browser local storage.
    Searches Chrome, Edge, Firefox and other Chromium-based browsers.
    
    Returns:
        tuple: (refresh_token, hardware_id) or (None, None) if not found
    """
    storage_paths = _find_browser_storage_paths()
    
    for path in storage_paths:
        if not os.path.exists(path):
            continue
        
        storage_data = {}
        
        # Try Chromium LevelDB method
        storage_data.update(_read_chromium_local_storage(path))
        if storage_data:
            refresh_token, hardware_id = _extract_tokens_from_storage(storage_data)
            if refresh_token and hardware_id:
                return refresh_token, hardware_id
        
        # Try Chromium SQLite fallback
        storage_data.update(_read_chromium_local_storage_sqlite(path))
        if storage_data:
            refresh_token, hardware_id = _extract_tokens_from_storage(storage_data)
            if refresh_token and hardware_id:
                return refresh_token, hardware_id
        
        # Try Firefox method
        if "firefox" in path.lower() or "mozilla" in path.lower():
            profile_dirs = [path] if os.path.isdir(path) else []
            if os.path.isdir(path):
                for profile in os.listdir(path):
                    profile_path = os.path.join(path, profile)
                    if os.path.isdir(profile_path):
                        storage_data.update(_read_firefox_local_storage(profile_path))
                        if storage_data:
                            refresh_token, hardware_id = _extract_tokens_from_storage(storage_data)
                            if refresh_token and hardware_id:
                                return refresh_token, hardware_id
    
    return None, None
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|authorization|"
    r"t_auth_token|password|secret)\b\s*[:=]\s*"
    r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^,\s;}&]+)"
)


def sanitize_error(value, secrets=()):
    """Remove configured credentials and token-shaped values from any output."""
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if (
                "token" in str(key).casefold()
                or str(key).casefold() in {
                    "authorization", "password", "secret", "t_auth_token"
                }
            ) else sanitize_error(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [sanitize_error(item, secrets) for item in value]
    if isinstance(value, bytes):
        value = value.decode("utf-8", "replace")
    text = str(value)
    candidates = set()
    for secret in secrets:
        if secret is None:
            continue
        raw = str(secret)
        trimmed = raw.strip()
        if trimmed:
            candidates.update((raw, trimmed, '"' + trimmed + '"', "'" + trimmed + "'"))
            if len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'":
                normalized = trimmed[1:-1].strip()
                candidates.update((normalized, '"' + normalized + '"',
                                   "'" + normalized + "'"))
    for secret in sorted(candidates, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _CREDENTIAL_ASSIGNMENT.sub(
        lambda match: "%s=[REDACTED]" % match.group(1), text
    )
    text = _BEARER_PATTERN.sub("Bearer [REDACTED]", text)
    return _JWT_PATTERN.sub("[REDACTED]", text)


def normalize_refresh_token(value):
    """Normalize a token copied from a browser field without exposing it."""
    raw = "" if value is None else str(value)
    trimmed = raw.strip()
    whitespace_removed = trimmed != raw
    quote_removed = len(trimmed) >= 2 and trimmed[0] == trimmed[-1] and trimmed[0] in "\"'"
    if quote_removed:
        trimmed = trimmed[1:-1].strip()
        whitespace_removed = whitespace_removed or trimmed != raw.strip()[1:-1]
    return trimmed, {
        "token_category": "configured_refresh_token",
        "token_length": len(trimmed),
        "token_prefix": (trimmed[:4] + "...") if trimmed else "",
        "outer_quotes_removed": quote_removed,
        "surrounding_whitespace_removed": whitespace_removed,
    }


def _query_value(query, name):
    if not isinstance(query, dict):
        raise TolinoAuthError("Browser login returned invalid callback data.")
    values = query.get(name)
    if values is None:
        return None
    if isinstance(values, (str, bytes)):
        return values
    if isinstance(values, (tuple, list)):
        return next(iter(values), None)
    raise TolinoAuthError("Browser login returned invalid %s data." % name)


def hardware_id():
    os_id = {"Windows": "1", "Darwin": "2", "Linux": "3"}.get(platform.system(), "x")
    return "%sxxA-00BCD-EFGHI-JKLMN-OPQRh" % os_id


def callback_redirect_uri(port):
    return "http://127.0.0.1:%d/callback" % int(port)


def validate_callback(query, expected_state, created_at, now=None):
    """Validate one OAuth callback without accepting tokens from the URL."""
    now = time.time() if now is None else now
    if now - created_at > OAUTH_STATE_TTL:
        raise TolinoAuthError("Browser login expired. Please try again.")
    if _query_value(query, "state") != expected_state:
        raise TolinoAuthError("Browser login state did not match.")
    if _query_value(query, "error"):
        raise TolinoAuthError("Browser login was rejected by the partner.")
    code = _query_value(query, "code")
    if not code:
        raise TolinoAuthError("Browser login returned no authorization code.")
    return code


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.query = parse_qs(urlparse(self.path).query)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Login received. You can return to Calibre.")

    def log_message(self, *_args):
        return


def browser_login(partner_id, hardware, timeout=OAUTH_STATE_TTL):
    """Run an OAuth callback for all partners with valid auth_url."""
    partner = PARTNERS.get(int(partner_id))
    if not partner or not partner.get("auth_url"):
        raise TolinoAuthError(
            "This Tolino partner has no OAuth authorization URL configured. "
            "Use a Web Reader refresh token in the configuration as fallback."
        )
    if not partner.get("token_url"):
        raise TolinoAuthError(
            "This Tolino partner has no token endpoint configured. "
            "Use a Web Reader refresh token in the configuration as fallback."
        )
    state = uuid.uuid4().hex
    created_at = time.time()
    server = HTTPServer(("127.0.0.1", 0), _CallbackHandler)
    server.timeout = timeout
    redirect_uri = callback_redirect_uri(server.server_port)
    
    # Special handling for Orell Fussli (partner 8) - autologin endpoint
    # does NOT accept redirect_uri in query parameters
    if int(partner_id) == 8:
        params = {
            "client_id": partner["client_id"],
            "response_type": "code",
            "scope": partner["scope"],
            "state": state,
        }
        for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
            if partner.get(key):
                params[key] = partner[key]
        auth_url = partner["auth_url"]
    else:
        params = {
            "client_id": partner["client_id"],
            "response_type": "code",
            "scope": partner["scope"],
            "redirect_uri": redirect_uri,
            "state": state,
        }
        for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
            if partner.get(key):
                params[key] = partner[key]
        auth_url = partner["auth_url"]
    
    if not webbrowser.open(auth_url + "?" + urlencode(params)):
        server.server_close()
        raise TolinoAuthError("Could not open the system browser.")
    while not hasattr(server, "query") and time.time() - created_at < timeout:
        server.handle_request()
    query = getattr(server, "query", {})
    server.server_close()
    code = validate_callback(query, state, created_at)
    client = TolinoClient(partner_id, hardware)
    payload = {
        "client_id": partner["client_id"],
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
    }
    if partner_id != 8:
        payload["scope"] = partner["scope"]
    data = client._request(partner["token_url"], "POST", payload,
                           form=True, authenticated=False)
    if not data.get("access_token") or not data.get("refresh_token"):
        raise TolinoAuthError("Browser login returned an incomplete token response.")
    return data["refresh_token"], client.hardware


def redact_error_text(value):
    """Keep provider status text while removing credential-shaped values."""
    return sanitize_error(value)[:500]


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, username=None,
                 secret=None, timeout=45, token_callback=None):
        if partner_id not in PARTNERS:
            raise TolinoError("Unsupported Tolino partner ID: %s" % partner_id)
        self.partner_id = int(partner_id)
        self.partner = PARTNERS[self.partner_id]
        self.hardware = hardware or hardware_id()
        self.refresh, self.token_diagnostics = normalize_refresh_token(refresh)
        self.username = username
        self.password = secret
        self.timeout = timeout
        self.access = None
        self.expires_at = 0
        self.last_http_status = None
        self.last_error_text = None
        self.token_callback = token_callback
        self._login_lock = threading.Lock()

    def auth_diagnostics(self):
        """Return safe authentication context for the debug report."""
        return {
            "partner_id": self.partner_id,
            "partner_name": self.partner["name"],
            "client_id": self.partner.get("client_id"),
            "scope": self.partner.get("scope"),
            "token_url": self.partner.get("token_url"),
            "grant_type": "refresh_token",
            "hardware_id": self.hardware,
            "reseller_id": str(self.partner_id),
            "http_status": self.last_http_status,
            "error_text": sanitize_error(self.last_error_text,
                                         (self.refresh, self.access)),
            **self.token_diagnostics,
        }

    def login(self):
        # A valid access token avoids spending the rotating refresh token twice.
        if self.access and time.time() < self.expires_at:
            return self.refresh
        with self._login_lock:
            if self.access and time.time() < self.expires_at:
                return self.refresh
            return self._login()

    def _login(self):
        if self.refresh:
            if not self.partner.get("token_url"):
                raise TolinoAuthError(
                    "This partner has no verified refresh-token endpoint in the reference client."
                )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh,
            }
            if self.partner_id != 8:
                payload["scope"] = self.partner["scope"]
        elif self.username and self.password:
            raise TolinoAuthError(
                "Username/password login requires the partner's browser OAuth flow. "
                "Use a Web Reader refresh token for this plugin."
            )
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
                "scope": self.partner["scope"],
            }
        else:
            raise TolinoAuthError("Configure a refresh token or username/password.")
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except TolinoApiError as exc:
            detail = str(exc)
            if "invalid_grant" in detail.casefold() or "reuse exceeded" in detail.casefold():
                # Invalidate the current refresh token to prevent reuse
                self.refresh = None
                self.access = None
                self.expires_at = 0
                raise TolinoAuthError(
                    "Tolino rejected this refresh token because it was reused or invalid. "
                    "Sign in to the Web Reader again, copy its new refresh token, and "
                    "do not test this token repeatedly."
                ) from exc
            raise TolinoAuthError("Tolino authentication failed: %s" % exc)
        if not data.get("access_token"):
            raise TolinoAuthError("Tolino token response did not contain access_token.")
        previous_refresh = self.refresh
        self.access = data["access_token"]
        new_refresh = data.get("refresh_token", self.refresh)
        
        # CRITICAL: Save the new refresh token IMMEDIATELY before any further use
        # Tolino invalidates refresh tokens after single use, so we must persist
        # the new token before making any authenticated requests with it
        if new_refresh != previous_refresh:
            self.refresh = new_refresh
            if self.token_callback:
                self.token_callback(self.refresh)
        else:
            self.refresh = new_refresh
        
        self.expires_at = time.time() + max(0, int(data.get("expires_in", 3600)) - 60)
        return self.refresh

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _retry=True):
        if authenticated and (not self.access or time.time() >= self.expires_at):
            self.login()
        body = None
        headers = {"User-Agent": "Calibre-Tolino-Plugin/0.2"}
        if authenticated:
            headers.update({
                "t_auth_token": self.access,
                "hardware_id": self.hardware,
                "reseller_id": str(self.partner_id),
            })
            for key in ("client_type", "client_version"):
                if self.partner.get(key):
                    headers[key] = self.partner[key]
        elif url == self.partner.get("token_url"):
            headers.update(self.partner.get("token_headers", {}))
        if data is not None:
            if form:
                body = urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, bytes):
                body = data
                headers["Content-Type"] = content_type or "application/octet-stream"
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = content_type or "application/json"
        request = Request(url, data=body, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout) as response:
                self.last_http_status = response.status
                self.last_error_text = None
                raw = response.read()
        except HTTPError as exc:
            raw_detail = exc.read().decode("utf-8", "replace")
            self.last_http_status = exc.code
            try:
                payload = json.loads(raw_detail)
            except ValueError:
                payload = None
            if isinstance(payload, dict):
                safe_detail = {
                    key: payload[key] for key in ("error", "error_description")
                    if key in payload
                }
                detail = json.dumps(safe_detail, ensure_ascii=False, sort_keys=True)
            else:
                detail = raw_detail
            self.last_error_text = sanitize_error(
                detail, (self.refresh, self.access)
            )[:500]
            retryable_auth = (
                exc.code == 401 and authenticated and self.refresh and _retry
                and "invalid_grant" not in self.last_error_text.casefold()
                and "reuse exceeded" not in self.last_error_text.casefold()
            )
            if retryable_auth:
                self.access = None
                self.login()
                return self._request(url, method, data, form, True,
                                     content_type, _retry=False)
            if exc.code in (401, 403):
                self.access = None
                raise TolinoAuthError("Tolino rejected authentication (%s)." % exc.code)
            raise TolinoApiError("Tolino HTTP %s: %s" % (exc.code, self.last_error_text))
        except (URLError, OSError) as exc:
            raise TolinoApiError("Tolino request failed: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)))
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise TolinoApiError("Tolino returned invalid JSON.")

    def inventory(self):
        data = self._request(BASE_URL + "/inventory/delta?strip=true")
        inventory = data.get("PublicationInventory", {})
        if isinstance(inventory, dict):
            records = []
            for key in ("edata", "ebook"):
                value = inventory.get(key, [])
                records.extend(value if isinstance(value, list) else [value])
            return records
        return inventory if isinstance(inventory, list) else []

    def inventory_ids(self):
        ids = set()
        for item in self.inventory():
            if not isinstance(item, dict):
                continue
            value = item.get("deliverableId") or item.get("deliverable_id") or item.get("id")
            if not value:
                for container in ("deliverable", "metadata", "ebook", "edata"):
                    nested = item.get(container)
                    if isinstance(nested, dict):
                        value = (nested.get("deliverableId") or
                                 nested.get("deliverable_id") or nested.get("id"))
                        if value:
                            break
            if value:
                ids.add(str(value))
        return ids

    @staticmethod
    def _multipart(fields, file_path, field="file"):
        boundary = ("----CalibreTolino%s" % uuid.uuid4().hex).encode("ascii")
        chunks = []
        for key, value in fields.items():
            chunks.extend([b"--" + boundary + b"\r\n",
                           ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
                           str(value).encode(), b"\r\n"])
        name = os.path.basename(file_path)
        guessed_type = mimetypes.guess_type(name)
        mime = next(iter(guessed_type), None) or "application/octet-stream"
        chunks.extend([b"--" + boundary + b"\r\n",
                       ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, name)).encode(),
                       ("Content-Type: %s\r\n\r\n" % mime).encode(),
                       open(file_path, "rb").read(), b"\r\n",
                       b"--" + boundary + b"--\r\n"])
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary.decode()

    def upload(self, file_path):
        body, content_type = self._multipart({}, file_path)
        data = self._request(BASE_URL + "/upload", "POST", body,
                             content_type=content_type)
        deliverable = data.get("metadata", {}).get("deliverableId")
        if not deliverable:
            raise TolinoApiError("Upload response did not contain deliverableId.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, file_path):
        body, content_type = self._multipart({"deliverableId": deliverable_id}, file_path)
        self._request(BASE_URL + "/cover", "POST", body, content_type=content_type)

    def delete(self, deliverable_id):
        self._request(BASE_URL + "/deletecontent?deliverableId=" + str(deliverable_id))

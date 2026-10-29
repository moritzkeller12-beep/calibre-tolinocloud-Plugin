import json
import mimetypes
import os
import re
import threading
import time
import traceback
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote as _url_quote, urlsplit
from urllib.request import Request, urlopen

from .inventory import parse_inventory
from .partners import (PARTNERS, WEBREADER_CLIENT_TYPE,
                       WEBREADER_CLIENT_VERSION, WEBREADER_TOKEN_HEADERS)
from . import sync_data as _sync_data

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


class TolinoError(Exception):
    """Fehler mit Code (z. B. A02), kurzem Nutzertext und Detail fürs Log.

    str(exc) bleibt der vollständige Text (auch Serverantwort) und dient
    der internen Auswertung; der Dialog zeigt nur `short` (falls gesetzt)
    plus Code. Alles Technische landet über report_error() im Calibre-Log.
    """
    code = "E00"

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


# Immer derselbe Satz, wenn nur eine neue Anmeldung hilft.
RELOGIN = "Bitte bei tolino anmelden."
# Der Refresh-Token ist single-use: rotiert ihn gerade jemand anderes,
# darf diese Stelle ihn nicht ebenfalls einloesen (Keycloak bricht die
# Sitzung dann ab). Kurz warten, dann erneut versuchen.
BUSY_MESSAGE = "Die Anmeldung wird gerade erneuert, bitte kurz warten."
RELOGIN_THEN = "Danach bei tolino anmelden."
def _trace_text(exc):
    """Vollständiger Traceback samt Ursachenkette -- "" wenn es keinen gibt.

    Die Exception wird selbst formatiert (nicht sys.exc_info()), damit auch
    die Kette «Ursache -> Folgefehler» im Log landet und nicht der zufällig
    gerade behandelte Fehler.
    """
    if (exc.__traceback__ is None and exc.__cause__ is None
            and exc.__context__ is None):
        return ""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


def report_error(exc, context="", secrets=()):
    """Loggt Code, Text und Detail (ohne Token); gibt die Nutzermeldung zurück.

    Rückgabe: genau eine Zeile, «Kurztext (Code A02)». Der Dialog bekommt
    nur diese Zeile; alles Weitere (Serverantwort, Traceback, Ursachenkette)
    steht im Calibre-Log.
    """
    if isinstance(exc, TolinoError):
        code, shown = exc.code, exc.short or str(exc)
        detail = " | ".join(x for x in (str(exc) if exc.short else "",
                                        exc.detail) if x)
    else:
        code = "E99"
        shown = "Unerwarteter Fehler: %s" % str(exc)[:150]
        detail = type(exc).__name__
    shown = sanitize_error(shown, secrets)
    line = "[%s] %s" % (code, shown)
    if detail:
        line += " | " + sanitize_error(detail, secrets)
    if context:
        line += " (%s)" % context
    log_warning("tolino-Fehler " + line)
    trace = _trace_text(exc)
    if trace:
        log_warning("Tolino-Details " + sanitize_error(trace, secrets))
    return "%s (Code %s)" % (shown, code)


def log_info(message):
    """Schreibt eine Zeile ins Calibre-Log -- ohne Zugangsdaten.

    Fuer sichtbare Betriebsmeldungen (Keep-alive gestartet/erneuert), die
    keine Warnung sind, aber ohne Log unsichtbar blieben.
    """
    try:
        from calibre.utils.logging import default_log
        default_log.info(str(message))
    except Exception:
        pass


def log_warning(message):
    """Schreibt eine Warnung ins Calibre-Log -- ohne Zugangsdaten.

    Die Meldung darf NIE einen Token-Wert enthalten. Ausserhalb von Calibre
    (Tests, direkter Import) bleibt der Aufruf still: das ist die Naht zum
    Log und darf selbst nicht werfen.
    """
    try:
        from calibre.utils.logging import default_log
        default_log.warn(str(message))
    except Exception:
        pass


BASE_URL = "https://bosh.pageplace.de/bosh/rest"
# registerhw muss am BOSH-Dienst ankommen, bevor die ID akzeptiert wird
# (Feldbefund: ein Verbindungstest direkt nach der Registrierung
# warf noch 400, der naechste Versuch Sekunden spaeter lief) -- vor dem
# ersten und vor dem letzten Retry wird kurz gewartet.
_REGISTER_SETTLE_SECONDS = 1.5
# Feldbefund: der erste Token-Aufruf nach der Browser-Anmeldung traf
# die Bot-Schutz-Laufzeitbahn (403 + HTML-Fehlerseite), jeder spaetere Klick
# lief durch -- zwei Warteversuche, bevor der Fehler sichtbar wird.
_WAF_RETRY_SECONDS = (2.0, 5.0)
_BOT_CHECK_MARKERS = (
    "zugriff geblockt", "access denied", "captcha", "cloudfront",
    "request blocked", "<!doctype html",
)


def _bot_check_detected(detail):
    """Distinguish bot-protection responses from OAuth rejection bodies."""
    return any(marker in str(detail or "").casefold()
               for marker in _BOT_CHECK_MARKERS)


def _compact_error_text(detail):
    """Collapse HTML bot-check pages to readable text."""
    text = re.sub(r"<[^>]+>", " ", detail or "")
    return re.sub(r"\s+", " ", text).strip()


def _is_timeout(exc):
    """Recognize stdlib timeouts, including urllib's wrapped reason."""
    return (isinstance(exc, TimeoutError)
            or isinstance(getattr(exc, "reason", None), TimeoutError))


def _raise_transport_error(exc, secrets):
    """Raise a credential-safe error for the sole urllib transport."""
    if _is_timeout(exc):
        raise TolinoApiError(
            "Zeitüberschreitung bei der Verbindung zu tolino.", code="N01",
            short="Zeitüberschreitung bei der Verbindung.") from exc
    raise TolinoApiError(
        "Übertragung an tolino fehlgeschlagen: %s"
        % sanitize_error(exc, secrets), code="N01",
        short="Die Verbindung zu tolino ist fehlgeschlagen.") from exc


_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)
_BEARER_PATTERN = re.compile(
    r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+"
)


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
    text = "" if value is None else str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def hardware_id():
    """Return a fresh hardware ID in the web reader's UUID format."""
    return str(uuid.uuid4())


def normalize_hardware_id(value):
    """Bring a hardware ID into the web reader's 8-4-4-4-12 UUID shape."""
    text = str(value or "").strip()
    if not text:
        return ""
    compact = text.replace("-", "")
    if re.fullmatch(r"[0-9a-fA-F]{32}", compact):
        formatted = "%s-%s-%s-%s-%s" % (
            compact[0:8], compact[8:12], compact[12:16],
            compact[16:20], compact[20:32])
        return formatted.lower()
    return text


def _decode_json(raw):
    """Antwort-Body als JSON; ein leerer Body ist ein leeres Dict."""
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        raise TolinoApiError(
            "Ungültige Antwort von tolino (kein JSON).", code="N02",
            short="tolino hat eine ungültige Antwort geliefert.")


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, timeout=45,
                 token_callback=None, hardware_callback=None):
        try:
            partner_id = int(partner_id)
        except (TypeError, ValueError):
            raise TolinoError("Unbekannter tolino-Partner: %s" % partner_id)
        if partner_id not in PARTNERS:
            raise TolinoError("Unbekannter tolino-Partner: %s" % partner_id)
        self.partner_id = partner_id
        self.partner = PARTNERS[self.partner_id]
        self.hardware = normalize_hardware_id(hardware) or hardware_id()
        self.refresh = normalize_refresh_token(refresh)
        self.timeout = timeout
        self.access = None
        self.expires_at = 0
        self.last_error_text = None
        self.token_callback = token_callback
        self.hardware_callback = hardware_callback
        # Beide setzt der Token-Vault, sobald er diesen Client erzeugt;
        # ohne Vault meldet sich der Client direkt an.
        self.token_vault = None
        self.credential_store = None
        self._in_device_recovery = False
        self._hw_registered_now = False
        self._login_lock = threading.Lock()
        # Ein Lesevorgang der sync-data reicht, bis etwas geschrieben wird:
        # Lese-Status und Sammlungen brauchen dieselben Daten.
        self._sync_cache = None
        self.inventory_ok = True

    def login(self):
        # A valid access token avoids spending the rotating refresh token twice.
        if self.access and time.time() < self.expires_at:
            return self.refresh
        with self._login_lock:
            if self.access and time.time() < self.expires_at:
                return self.refresh
            if self.token_vault is None:
                return self._login()
            # Nach einer späten 401 zählt der zuletzt GESPEICHERTE Token:
            # der Keep-alive kann inzwischen rotiert haben.
            return self.token_vault.relogin(self)

    def _apply_token_response(self, data):
        """Store the token endpoint response; returns the rotated refresh."""
        if not data.get("access_token"):
            raise TolinoAuthError("Ungültige Anmelde-Antwort.", code="A07", detail="kein access_token")
        previous_refresh = self.refresh
        self.access = data["access_token"]
        new_refresh = data.get("refresh_token", self.refresh)
        # CRITICAL: Save the new refresh token IMMEDIATELY before any further use
        # Tolino invalidates refresh tokens after single use, so we must persist
        # the new token before making any authenticated requests with it
        self.refresh = new_refresh
        if new_refresh != previous_refresh and self.token_callback:
            self.token_callback(self.refresh)

        self.expires_at = time.time() + max(0, int(data.get("expires_in", 3600)) - 60)
        return self.refresh

    def _login(self):
        if self.refresh:
            payload = {
                "client_id": self.partner["client_id"],
                "grant_type": "refresh_token",
                "refresh_token": self.refresh,
                "scope": self.partner["scope"],
            }
        else:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN, code="A03")
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except (TolinoApiError, TolinoAuthError) as exc:
            detail = str(exc)
            folded = detail.casefold()
            session_ended = ("not active" in folded
                             or "session not active" in folded)
            token_rejected = ("invalid_grant" in folded
                              or "reuse exceeded" in folded)
            if session_ended or token_rejected:
                # A stale refresh token must never be tried again.
                self.refresh = self.access = None
                self.expires_at = 0
            if session_ended:
                raise TolinoAuthError(
                    "Die tolino-Sitzung ist beendet. " + RELOGIN,
                    code="A01", detail=detail) from exc
            if token_rejected:
                raise TolinoAuthError(
                    "tolino hat die gespeicherte Anmeldung abgelehnt. " + RELOGIN,
                    code="A02", detail=detail) from exc
            raise TolinoAuthError(
                "Anmeldung bei tolino fehlgeschlagen: %s" % exc, code="A06",
                short="Die Anmeldung bei tolino ist fehlgeschlagen.")

        return self._apply_token_response(data)

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _extra_headers=None):
        retry = {"auth": True, "hardware": True, "waf": 0}
        while True:
            if authenticated and (not self.access
                                  or time.time() >= self.expires_at):
                self.login()
            request = self._build_request(url, method, data, form,
                                          authenticated, content_type,
                                          _extra_headers)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self.last_error_text = None
                    raw = response.read()
            except HTTPError as exc:
                # Kehrt nur zurück, wenn ein weiterer Versuch erlaubt ist.
                self._handle_http_error(exc,
                                        exc.read().decode("utf-8", "replace"),
                                        url, authenticated, retry)
                continue
            except (URLError, OSError, TimeoutError) as exc:
                _raise_transport_error(exc, (self.refresh, self.access))
            return _decode_json(raw)

    def _build_request(self, url, method, data, form, authenticated,
                       content_type, extra_headers):
        """URL, Header und Body eines Aufrufs -- ohne Wiederholungslogik."""
        headers = {"User-Agent": BROWSER_USER_AGENT}
        if authenticated:
            headers.update({
                "t_auth_token": self.access,
                "hardware_id": self.hardware,
                "reseller_id": self.partner["reseller_id"],
                "client_type": WEBREADER_CLIENT_TYPE,
                "client_version": WEBREADER_CLIENT_VERSION,
            })
        elif url == self.partner.get("token_url"):
            headers.update(_browser_sec_headers())
            headers.update(WEBREADER_TOKEN_HEADERS)
        if extra_headers:
            headers.update(extra_headers)

        body = None
        if data is not None:
            if form:
                body = urlencode(data).encode("utf-8")
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif isinstance(data, bytes):
                body = data
                headers["Content-Type"] = (content_type
                                           or "application/octet-stream")
            else:
                body = json.dumps(data).encode("utf-8")
                headers["Content-Type"] = content_type or "application/json"
        return Request(url, data=body, headers=headers, method=method)

    def _handle_http_error(self, exc, raw_detail, url, authenticated, retry):
        """Fehlerdetail merken und entweder wiederholen lassen oder werfen.

        `retry` führt die drei Wiederholungsgründe: abgelaufener
        Access-Token, unbekannte Geräte-Registrierung, WAF-Block.
        """
        try:
            payload = json.loads(raw_detail)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            detail = {key: payload[key] for key in
                      ("error", "error_description", "message")
                      if key in payload}
            response_info = payload.get("ResponseInfo")
            if isinstance(response_info, dict) and response_info.get("message"):
                detail.setdefault("message",
                                  str(response_info["message"])[:300])
            detail = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        else:
            detail = raw_detail
        self.last_error_text = sanitize_error(
            detail, (self.refresh, self.access))[:500]

        if (exc.code == 401 and authenticated and self.refresh
                and retry["auth"]
                and "invalid_grant" not in self.last_error_text.casefold()
                and "reuse exceeded" not in self.last_error_text.casefold()):
            retry["auth"] = False
            self.access = None
            self.login()
            return

        if (exc.code == 400 and authenticated
                and url != self.partner.get("token_url")):
            if retry["hardware"]:
                self._hw_registered_now = False
                try:
                    recovered = self._recover_device_registration()
                except Exception:
                    recovered = False
                if recovered:
                    retry["hardware"] = False
                    if self._hw_registered_now:
                        time.sleep(_REGISTER_SETTLE_SECONDS)
                    return
            elif self._hw_registered_now:
                self._hw_registered_now = False
                time.sleep(_REGISTER_SETTLE_SECONDS)
                return

        if exc.code in (401, 403):
            is_waf_retry = (
                exc.code == 403
                and url == self.partner.get("token_url")
                and retry["waf"] < len(_WAF_RETRY_SECONDS)
                and _bot_check_detected(self.last_error_text))
            if is_waf_retry:
                time.sleep(_WAF_RETRY_SECONDS[retry["waf"]])
                retry["waf"] += 1
                return

            self.access = None

            message = (_compact_error_text(self.last_error_text)
                       or "no response detail")
            raise TolinoAuthError(
                "tolino hat die Anmeldung abgelehnt (%s): %s"
                % (exc.code, message), code="A06",
                short="tolino hat die Anmeldung abgelehnt.")
        raise TolinoApiError(
            "tolino-Fehler (HTTP %s): %s"
            % (exc.code, self.last_error_text), code="N03",
            short="tolino meldet einen Fehler (HTTP %s)." % exc.code)

        while True:
            if authenticated and (not self.access or time.time() >= self.expires_at):
                self.login()

            headers = {"User-Agent": BROWSER_USER_AGENT}
            if authenticated:
                headers.update({
                    "t_auth_token": self.access,
                    "hardware_id": self.hardware,
                    "reseller_id": self.partner["reseller_id"],
                })
                for key, shared in (("client_type", WEBREADER_CLIENT_TYPE),
                                    ("client_version", WEBREADER_CLIENT_VERSION)):
                    headers[key] = shared
            elif url == self.partner.get("token_url"):
                headers.update(_browser_sec_headers())
                headers.update(WEBREADER_TOKEN_HEADERS)
            if _extra_headers:
                headers.update(_extra_headers)

            body = None
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
                    self.last_error_text = None
                    raw = response.read()
            except HTTPError as exc:
                handle_error(exc, exc.read().decode("utf-8", "replace"))
                continue
            except (URLError, OSError, TimeoutError) as exc:
                _raise_transport_error(exc, (self.refresh, self.access))
            return decode(raw)

    def inventory(self):
        """Read the PublicationInventory form used by Tolino's API."""
        records, problems, self.inventory_ok = parse_inventory(
            self._request(BASE_URL + "/inventory/delta?strip=true"))
        if problems:
            log_warning("Tolino-Inventar: %s" % "; ".join(problems))
        return records

    @staticmethod
    def _multipart(fields, payload, filename, field="file"):
        """Build a multipart body in memory from bytes.

        Memory note: the returned body is one contiguous bytes object, so a
        large EPUB is held roughly twice while the chunks are joined. EPUBs
        are usually small (single-digit MB); keeping the stdlib-only
        transport (urllib) is worth that trade-off.
        """
        boundary = ("----CalibreTolino%s" % uuid.uuid4().hex).encode("ascii")
        chunks = []
        for key, value in fields.items():
            chunks.extend([b"--" + boundary + b"\r\n",
                           ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
                           str(value).encode(), b"\r\n"])
        name = os.path.basename(str(filename))
        guessed_type = mimetypes.guess_type(name)
        mime = next(iter(guessed_type), None) or "application/octet-stream"
        chunks.extend([b"--" + boundary + b"\r\n",
                       ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, name)).encode(),
                       ("Content-Type: %s\r\n\r\n" % mime).encode(),
                       payload, b"\r\n",
                       b"--" + boundary + b"--\r\n"])
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary.decode()

    def upload(self, file_path):
        with open(file_path, "rb") as source:
            payload = source.read()
        body, content_type = self._multipart({}, payload, file_path)
        data = self._request(BASE_URL + "/upload", "POST", body,
                             content_type=content_type)
        deliverable = (data.get("metadata") or {}).get("deliverableId")
        if not deliverable:
            raise TolinoApiError("Upload ohne deliverableId in der Antwort.", code="N04", short="tolino hat den Upload nicht bestätigt.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, data, suffix=".jpg"):
        body, content_type = self._multipart(
            {"deliverableId": deliverable_id}, data,
            "cover" + str(suffix or ""))
        self._request(BASE_URL + "/cover", "POST", body,
                      content_type=content_type)

    def delete(self, deliverable_id):
        self._request(BASE_URL + "/deletecontent?deliverableId=" + str(deliverable_id))

    # --- In-place metadata: update an existing book via /meta -----------
    META_URL = BASE_URL + "/meta"

    def update_metadata(self, deliverable_id, fields):
        """Update title/author/isbn on an existing book instead of
        re-uploading the file.

        GET /meta?deliverableId=... returns the book with its "metadata"
        dict; the merged dict goes back via PUT as {"uploadMetaData": …}.
        """
        if not isinstance(fields, dict) or not fields:
            raise TolinoApiError("Metadaten-Update ohne Felder.", code="S90", short="Es gibt nichts zu aktualisieren.")
        url = self.META_URL + "/?deliverableId=" + _url_quote(str(deliverable_id))
        book = self._request(url)
        metadata = book.get("metadata") if isinstance(book, dict) else None
        if not isinstance(metadata, dict):
            raise TolinoApiError(
                "Tolino hat keine Metadaten zu %s geliefert." % deliverable_id,
                code="N06", short="tolino hat keine Daten zu diesem Buch geliefert.")
        metadata.update(fields)
        self._request(url, "PUT", {"uploadMetaData": metadata},
                      content_type="application/json")
        # Tolino kann den PUT mit einer erfolgreichen HTTP-Antwort bestaetigen,
        # ohne die Aenderung tatsaechlich zu uebernehmen. Erst ein Read-back
        # bestaetigt, dass die Metadaten im Cloud-Datensatz stehen.
        verified = self._request(url)
        verified_metadata = (verified.get("metadata")
                             if isinstance(verified, dict) else None)
        if not isinstance(verified_metadata, dict) or any(
                verified_metadata.get(key) != value
                for key, value in fields.items()):
            raise TolinoApiError(
                "tolino hat das Metadaten-Update nicht bestaetigt.",
                code="N06",
                short="tolino hat die Metadaten nicht übernommen.")
        return verified_metadata

    # --- Device list: resolve the real hardware ID ----------------------

    DEVICES_URL = "https://bosh.pageplace.de/bosh/rest/handshake/devices/list"

    def fetch_hardware_id(self):
        """Return the most recently used hardware ID registered for the account."""
        payload = {
            "deviceListRequest": {
                "accounts": [{
                    "auth_token": self.access,
                    "reseller_id": self.partner["reseller_id"],
                }],
            },
        }
        data = self._request(self.DEVICES_URL, "POST", payload,
                             content_type="application/json")
        devices = (data.get("deviceListResponse") or {}).get("devices") or []
        devices = [item for item in devices if isinstance(item, dict)]
        if not devices:
            raise TolinoApiError("Geräteliste leer.", code="N06", short="tolino meldet kein registriertes Gerät.")
        devices.sort(key=lambda item: str(item.get("deviceLastUsage", "")))
        hardware = devices[-1].get("deviceId")
        if not hardware:
            raise TolinoApiError("Gerät ohne deviceId.", code="N06", short="Die Geräteliste von tolino ist unvollständig.")
        return str(hardware)

    def _notify_hardware(self):
        """Tell the UI about an adopted hardware id; never raises."""
        callback = getattr(self, "hardware_callback", None)
        if callback:
            try:
                callback(self.hardware)
            except Exception:
                pass

    def _register_hardware(self):
        """Register this hardware id at the BOSH service (reference flow)."""
        payload = {"hardware_name": "other"}
        extra = {
            "hardware_type": "HTML5",
            "client_type": WEBREADER_CLIENT_TYPE,
            "client_version": WEBREADER_CLIENT_VERSION,
        }
        for url in (BASE_URL + "/v2/registerhw", BASE_URL + "/registerhw"):
            try:
                self._request(url, "POST", payload,
                              content_type="application/json",
                              _extra_headers=extra)
                return True
            except Exception:
                continue
        return False

    def _recover_device_registration(self):
        """Adopt or register a hardware id the BOSH service accepts."""
        if self._in_device_recovery:
            return False
        self._in_device_recovery = True
        try:
            try:
                registered = normalize_hardware_id(self.fetch_hardware_id())
            except Exception:
                registered = ""
            if registered:
                if registered == normalize_hardware_id(self.hardware):
                    # Already THE registered device: the 400 has another
                    # cause -- do not mask it behind a pointless retry.
                    return False
                self.hardware = registered
                self._notify_hardware()
                return True
            registered_ok = self._register_hardware()
            self._hw_registered_now = bool(registered_ok)
            return registered_ok
        finally:
            self._in_device_recovery = False

    # --- Download: fetch an uploaded ebook back -------------------------

    def download(self, deliverable_id):
        """Download one book from the cloud as bytes."""
        info_url = (BASE_URL + "/cloud/downloadinfo/%s/%s/type/external-download"
                    % (_url_quote(str(deliverable_id)),
                       _url_quote(str(deliverable_id))))
        info = self._request(info_url)
        content_url = (info.get("DownloadInfo") or {}).get("contentUrl")
        if not content_url:
            raise TolinoApiError("Kein Download-Link in der Antwort.", code="N05", short="tolino hat keinen Download-Link geliefert.")
        try:
            parsed_content_url = urlsplit(str(content_url))
        except (TypeError, ValueError) as exc:
            raise TolinoApiError("Ungültiger Download-Link.", code="N05",
                                 short="tolino hat einen ungültigen Download-Link geliefert.") from exc
        if parsed_content_url.scheme.casefold() != "https" or not parsed_content_url.hostname:
            raise TolinoApiError("Unsicherer Download-Link.", code="N05",
                                 short="Der Download-Link verwendet keine sichere HTTPS-Verbindung.")
        headers = {"User-Agent": BROWSER_USER_AGENT}
        host = parsed_content_url.hostname.casefold().rstrip(".")
        authenticated_host = (
            host == "pageplace.de" or host.endswith(".pageplace.de"))
        request = Request(content_url, headers=headers)
        if authenticated_host:
            # urllib forwards regular Request headers over cross-host redirects.
            # Keep account credentials off redirect requests; a signed HTTPS
            # redirect to a CDN remains supported without disclosing the token.
            request.add_unredirected_header("t_auth_token", self.access)
            request.add_unredirected_header("hardware_id", self.hardware)
            request.add_unredirected_header(
                "reseller_id", self.partner["reseller_id"])
        try:
            with urlopen(request, timeout=self.timeout) as response:
                get_final_url = getattr(response, "geturl", None)
                final_url = urlsplit(get_final_url() if callable(get_final_url)
                                     else content_url)
                if final_url.scheme.casefold() != "https" or not final_url.hostname:
                    raise TolinoApiError(
                        "Der Download wurde auf eine unsichere URL umgeleitet.",
                        code="N05",
                        short="tolino hat einen unsicheren Download-Link geliefert.")
                content = response.read()
        except (HTTPError, URLError, OSError) as exc:
            raise TolinoApiError("Download von Tolino fehlgeschlagen: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)),
                                 code="N05", short="Der Download von Tolino ist fehlgeschlagen.")
        return content

    # --- Collections & read state via sync-data patches -----------------

    SYNC_DATA_URL = BASE_URL + "/sync-data?paths=publications"

    def _sync_patch(self, payload):
        # Jeder Schreibvorgang macht den Zwischenspeicher ungültig: danach
        # wird wieder gelesen, damit keine veraltete Revision weitergereicht
        # wird.
        self._sync_cache = None
        data = self._request(self.SYNC_DATA_URL, "PATCH", payload,
                             content_type="application/json")
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _tag_patch(op, book_id, name, category, revision=None, tag_id=None):
        """Kompatibler Einstieg zum gemeinsamen sync-data-Patch-Builder."""
        return _sync_data.tag_patch(
            op, book_id, name, category, TolinoApiError,
            revision=revision, tag_id=tag_id)

    def sync_snapshot(self):
        """(revision, patches) aus EINEM Lesevorgang, mehrfach nutzbar.

        Sammlungen und Lese-Status liegen in denselben sync-data-Daten;
        ohne Zwischenspeicher würde derselbe Stand zweimal geholt. Jeder
        Schreibvorgang verwirft ihn wieder (siehe `_sync_patch`).
        """
        if self._sync_cache is None:
            data = self._sync_patch({"revision": None, "patches": []})
            self._sync_cache = (data.get("revision"),
                                data.get("patches") or [])
        return self._sync_cache

    def read_ids(self):
        """Bücher, die im Konto als gelesen markiert sind.

        Quelle ist die System-Marke des Web Readers – dieselbe, die
        `mark_read_many` setzt. Damit kann der Bestandsvergleich die
        Spalte «Gelesen» vorbelegen.
        """
        _revision, patches = self.sync_snapshot()
        return _sync_data.read_ids(patches)

    def _current_revision(self, revision):
        """Revision fuer einen Schreibvorgang: die aktuelle, nie None.

        Ein PATCH ohne Revision kann den vorhandenen Stand ersetzen und
        damit fremde Marken (Lese-Status, andere Sammlungen) verlieren.
        Die Revision wird deshalb gelesen und mitgeschickt.
        """
        if revision is not None:
            return revision
        revision, _patches = self.sync_snapshot()
        return revision

    def add_to_collection(self, book_id, collection_name, revision=None):
        """Add an uploaded/purchased book to a named collection.

        `revision` kann aus dem vorigen Aufruf weitergereicht werden, damit
        mehrere Zuordnungen nur eine Cloud-Anfrage zum Lesen brauchen.
        """
        return _sync_data.add_to_collection(
            self, book_id, collection_name, revision)

    def mark_read_many(self, book_ids, finished=True):
        """Set (or clear) the finished mark for several books at once.

        Ein Lesevorgang, ein PATCH: alle Bücher bekommen dieselbe Revision.
        Bereits gesetzte Marken werden nicht erneut geschrieben, fehlende
        beim Entfernen übersprungen.
        """
        return _sync_data.mark_read_many(
            self, book_ids, finished, TolinoApiError)

    def collection_pairs(self):
        """Return the {(deliverable, collection)} pairs set in the cloud.

        Reads sync-data once so the series reconcile only has to
        PATCH genuinely missing assignments.
        """
        _revision, patches = self.sync_snapshot()
        return _sync_data.collection_pairs(patches)

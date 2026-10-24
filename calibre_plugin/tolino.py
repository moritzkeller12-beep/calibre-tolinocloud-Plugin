import json
import mimetypes
import os
import re
import subprocess
import threading
import time
import traceback
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, quote as _url_quote
from urllib.request import Request, urlopen

from .inventory import parse_inventory
from .partners import (PARTNERS, WEBREADER_CLIENT_TYPE,
                       WEBREADER_CLIENT_VERSION, WEBREADER_TOKEN_HEADERS,
                       uses_shared_webreader)
from .transport import (BROWSER_USER_AGENT, _browser_sec_headers,
                        _bot_check_detected, _compact_error_text,
                        _curl_binary, _http_post_via_curl,
                        _http_post_via_curl_cffi, _impersonate_session,
                        _waf_block_response)
from . import sync_data as _sync_data


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
_NO_AUTO_LOGIN = (
    "Für diesen Buchhändler gibt es keine automatische Anmeldung. "
    "Bitte den Zugang unter «Einstellungen …» → «Konten und Verbindung» "
    "eintragen.")

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


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, timeout=45,
                 token_callback=None,
                 hardware_callback=None, refresh_provider=None):
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
        # Liefert den FRISCHESTEN gespeicherten Zugang. Ein langlebiger
        # Client (Vergleichsfenster) kann einen Token halten, den der
        # Keep-alive inzwischen rotiert hat; vor dem Einloesen gilt der
        # gespeicherte Stand.
        self.refresh_provider = refresh_provider
        self._in_device_recovery = False
        self._hw_registered_now = False
        self._login_lock = threading.Lock()
        # Ein Lesevorgang der sync-data reicht, bis etwas geschrieben wird:
        # Lese-Status und Sammlungen brauchen dieselben Daten.
        self._sync_cache = None
        # Ergebnis der Inventar-Deutung: nichts geraten; unbekannte
        # Datensaetze verhindern riskante Loeschungen.
        self.inventory_problems = []
        self.inventory_envelope_ok = True
        # Kopien ohne Deliverable-Kennung: lesbar, aber fuer Loeschungen
        # kein Beweis.
        self.inventory_unidentified = 0
        self.inventory_ids_complete = True

    def login(self):
        # A valid access token avoids spending the rotating refresh token twice.
        if self.access and time.time() < self.expires_at:
            return self.refresh
        with self._login_lock:
            if self.access and time.time() < self.expires_at:
                return self.refresh
            return self._login()

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

    def _adopt_fresh_credentials(self):
        """Gespeicherten Zugang uebernehmen, bevor der Token eingeloest wird.

        Ein zwischengespeicherter Client darf nie mit einem bereits
        rotierten Refresh-Token erneut anmelden -- sonst beendet der
        Reuse-Schutz die Sitzung (A02). Was gespeichert ist, gewinnt.
        """
        if self.refresh_provider is None:
            return
        try:
            fresh = self.refresh_provider() or {}
        except Exception:
            return  # kein Zugang lesbar: mit dem eigenen Stand weiter
        token = str(fresh.get("refresh_token") or "").strip()
        if token and token != self.refresh:
            self.refresh = normalize_refresh_token(token)
        hardware = str(fresh.get("hardware_id") or "").strip()
        if hardware and hardware != self.hardware:
            self.hardware = hardware

    def _login(self):
        self._adopt_fresh_credentials()
        if self.refresh:
            if not self.partner.get("token_url"):
                raise TolinoAuthError(_NO_AUTO_LOGIN, code="A05", detail="token_url fehlt")
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
        retry_request = object()
        fallthrough = object()

        def decode(raw):
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except ValueError:
                raise TolinoApiError(
                    "Ungültige Antwort von tolino (kein JSON).", code="N02",
                    short="tolino hat eine ungültige Antwort geliefert.")

        def handle_error(exc, raw_detail):
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
                return retry_request

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
                        return retry_request
                elif self._hw_registered_now:
                    self._hw_registered_now = False
                    time.sleep(_REGISTER_SETTLE_SECONDS)
                    return retry_request

            if exc.code in (401, 403):
                is_waf_retry = (
                    exc.code == 403
                    and url == self.partner.get("token_url")
                    and retry["waf"] < len(_WAF_RETRY_SECONDS)
                    and _bot_check_detected(self.last_error_text))
                if is_waf_retry:
                    time.sleep(_WAF_RETRY_SECONDS[retry["waf"]])
                    retry["waf"] += 1
                    return retry_request
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
                    "reseller_id": self.partner.get(
                        "reseller_id", str(self.partner_id)),
                })
                for key, shared in (("client_type", WEBREADER_CLIENT_TYPE),
                                    ("client_version", WEBREADER_CLIENT_VERSION)):
                    value = self.partner.get(key) or (
                        shared if uses_shared_webreader(self.partner) else None)
                    if value:
                        headers[key] = value
            elif url == self.partner.get("token_url"):
                headers.update(_browser_sec_headers())
                headers.update(self.partner.get("token_headers")
                               or (WEBREADER_TOKEN_HEADERS
                                   if uses_shared_webreader(self.partner)
                                   else {}))
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

            def try_transport(name, post):
                try:
                    status, raw = post()
                except FileNotFoundError:
                    return fallthrough
                except TimeoutError as exc:
                    raise TolinoApiError(
                        "Zeitüberschreitung bei der Verbindung zu tolino: %s"
                        % sanitize_error(exc, (self.refresh, self.access)),
                        code="N01", short="Zeitüberschreitung bei der Verbindung.") from exc
                except Exception as exc:
                    # Nur Verbindungsfehler vor dem Senden erlauben einen
                    # anderen Transport. Timeout und Fehler nach dem Versand
                    # könnten den Token bereits verbraucht haben.
                    cause = exc
                    if isinstance(exc, subprocess.TimeoutExpired):
                        raise TolinoApiError(
                            "Zeitüberschreitung bei der Verbindung zu tolino.",
                            code="N01",
                            short="Zeitüberschreitung bei der Verbindung.") from exc
                    if isinstance(exc, subprocess.CalledProcessError):
                        if exc.returncode in (6, 7):
                            return fallthrough
                        raise TolinoApiError(
                            "curl konnte die Anfrage nicht sicher abschliessen "
                            "(Status %s)." % exc.returncode,
                            code="N01",
                            short="Die Verbindung zu tolino ist fehlgeschlagen.") from exc
                    if (isinstance(cause, OSError)
                            and getattr(cause, "errno", None) in (6, 7)):
                        return fallthrough
                    if (isinstance(cause, subprocess.CalledProcessError)
                            and cause.returncode in (6, 7)):
                        return fallthrough
                    if name == "curl_cffi":
                        return fallthrough
                    raise TolinoApiError(
                        "Übertragung an tolino fehlgeschlagen: %s"
                        % sanitize_error(exc, (self.refresh, self.access)),
                        code="N01", short="Die Verbindung zu tolino ist fehlgeschlagen.") from exc
                if status and status < 400:
                    self.last_error_text = None
                    return decode(raw)
                if _waf_block_response(status, raw):
                    return fallthrough
                error = HTTPError(url, status or 400,
                                  "HTTP Error %s" % (status or "?"), {}, None)
                return handle_error(error, (raw or b"").decode("utf-8", "replace"))

            if data is not None and url == self.partner.get("token_url"):
                session = _impersonate_session()
                transports = []
                if session is not None:
                    transports.append(("curl_cffi", lambda: _http_post_via_curl_cffi(
                        session, url, body or b"", headers, self.timeout)))
                if _curl_binary() is not None:
                    transports.append(("curl", lambda: _http_post_via_curl(
                        url, body or b"", headers, self.timeout)))
                outcome = fallthrough
                for name, post in transports:
                    outcome = try_transport(name, post)
                    if outcome is retry_request:
                        break
                    if outcome is not fallthrough:
                        return outcome
                if outcome is retry_request:
                    continue

            request = Request(url, data=body, headers=headers, method=method)
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    self.last_error_text = None
                    raw = response.read()
            except HTTPError as exc:
                outcome = handle_error(exc, exc.read().decode("utf-8", "replace"))
                if outcome is retry_request:
                    continue
                return outcome
            except (URLError, OSError, TimeoutError) as exc:
                timeout = (isinstance(exc, TimeoutError)
                           or isinstance(getattr(exc, "reason", None), TimeoutError))
                detail = ("Zeitüberschreitung bei der Verbindung zu tolino."
                          if timeout else
                          "Verbindung zu tolino fehlgeschlagen: %s" %
                          sanitize_error(exc, (self.refresh, self.access)))
                short = ("Zeitüberschreitung bei der Verbindung."
                         if timeout else "Keine Verbindung zu tolino.")
                raise TolinoApiError(detail, code="N01", short=short) from exc
            return decode(raw)

    def inventory(self):
        """Cloud-Bestand lesen -- als normalisierte Datensaetze.

        Eine leere Bibliothek und eine unbekannte Antwort sind NICHT
        dasselbe. Die Antwort wird deshalb gedeutet statt geraten:
        ``inventory_envelope_ok`` False heisst «gar kein Bestand in der
        Antwort» (der Vergleich scheitert), ``inventory_problems`` nennt
        einzelne und deutbare Datensaetze -- die sperren jede Loeschung.

        Jeder Datensatz hat die Form aus :func:`inventory.normalize_inventory_record`.
        """
        data = self._request(BASE_URL + "/inventory/delta?strip=true")
        records, problems, envelope_ok = parse_inventory(data)
        self.inventory_problems = list(problems)
        self.inventory_envelope_ok = bool(envelope_ok)
        # Datensaetze ohne Deliverable-Kennung: lesbar, aber fuer
        # Loeschentzcheidungen kein Beweis.
        self.inventory_unidentified = sum(
            1 for record in records if not record["has_deliverable_id"])
        self.inventory_ids_complete = not self.inventory_unidentified
        if problems:
            log_warning("Cloud-Bestand nur teilweise lesbar: %s"
                        % "; ".join(problems[:5]))
        return records

    def inventory_ids(self):
        """Deliverable-Kennungen des Bestands.

        Nur sie taugen fuer /deletecontent und /downloadinfo. Eine
        Ersatzkennung (``identifier``) wird bewusst NICHT zurueckgegeben:
        sie belegt keine bestimmte Kopie und wuerde eine Zuordnung sonst
        faelschlich als «noch vorhanden» oder «geloescht» erscheinen lassen.
        Ob der Bestand dafuer vollstaendig genug war, sagt
        ``inventory_ids_complete`` -- der SyncWorker raeumt bei False keine
        nicht gefundene Zuordnung auf.

        Undeutbare Datensaetze verweigern die Auskunft ganz (N02): eine
        unbekannte Kopie koennte sonst als «nicht mehr in der Cloud» gelten
.
        """
        records = self.inventory()
        problems = list(getattr(self, "inventory_problems", ()) or ())
        if problems:
            raise TolinoApiError(
                "Unerwartete Antwort der tolino-Cloud: %s."
                % "; ".join(problems[:3]),
                code="N02",
                short="Der Cloud-Bestand liess sich nicht vollständig lesen. "
                "Es wurde nichts gelöscht oder geändert.")
        ids = set()
        for record in records:
            if record["has_deliverable_id"] and record["tolino_id"]:
                ids.add(record["tolino_id"])
        return ids

    @staticmethod
    def _multipart(fields, file_path, field="file"):
        """Build a multipart body in memory.

        Memory note: the returned body is one contiguous bytes object, so a
        large EPUB is held roughly twice while the chunks are joined. EPUBs
        are usually small (single-digit MB); keeping the stdlib-only
        transport (urllib) is worth that trade-off. The upload is sent as a
        single request, and the temporary profile/cover data is cleaned up
        by the caller.
        """
        boundary = ("----CalibreTolino%s" % uuid.uuid4().hex).encode("ascii")
        chunks = []
        for key, value in fields.items():
            chunks.extend([b"--" + boundary + b"\r\n",
                           ("Content-Disposition: form-data; name=\"%s\"\r\n\r\n" % key).encode(),
                           str(value).encode(), b"\r\n"])
        name = os.path.basename(file_path)
        guessed_type = mimetypes.guess_type(name)
        mime = next(iter(guessed_type), None) or "application/octet-stream"
        with open(file_path, "rb") as source:
            payload = source.read()
        chunks.extend([b"--" + boundary + b"\r\n",
                       ('Content-Disposition: form-data; name="%s"; filename="%s"\r\n' % (field, name)).encode(),
                       ("Content-Type: %s\r\n\r\n" % mime).encode(),
                       payload, b"\r\n",
                       b"--" + boundary + b"--\r\n"])
        return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary.decode()

    def upload(self, file_path):
        body, content_type = self._multipart({}, file_path)
        data = self._request(BASE_URL + "/upload", "POST", body,
                             content_type=content_type)
        deliverable = data.get("metadata", {}).get("deliverableId")
        if not deliverable:
            raise TolinoApiError("Upload ohne deliverableId in der Antwort.", code="N04", short="tolino hat den Upload nicht bestätigt.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, file_path):
        body, content_type = self._multipart({"deliverableId": deliverable_id}, file_path)
        self._request(BASE_URL + "/cover", "POST", body, content_type=content_type)

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
        return metadata

    # --- Device list: resolve the real hardware ID ----------------------

    DEVICES_URL = "https://bosh.pageplace.de/bosh/rest/handshake/devices/list"

    def fetch_hardware_id(self):
        """Return the most recently used hardware ID registered for the account."""
        payload = {
            "deviceListRequest": {
                "accounts": [{
                    "auth_token": self.access,
                    "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
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
            "client_type": (self.partner.get("client_type")
                            or "TOLINO_WEBREADER"),
            "client_version": self.partner.get("client_version") or "5.2.0",
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
        request = Request(content_url, headers={
            "User-Agent": BROWSER_USER_AGENT,
            "t_auth_token": self.access,
            "hardware_id": self.hardware,
            "reseller_id": self.partner.get("reseller_id", str(self.partner_id)),
        })
        try:
            with urlopen(request, timeout=self.timeout) as response:
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

    READ_TAG = _sync_data.READ_TAG

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

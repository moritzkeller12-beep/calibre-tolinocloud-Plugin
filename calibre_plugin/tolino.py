import base64
import json
import mimetypes
import os
import re
import sys
import threading
import time
import traceback
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, quote as _url_quote, urlsplit
from urllib.request import (HTTPRedirectHandler, Request, build_opener,
                           urlopen)

from .errors import TolinoApiError, TolinoAuthError, TolinoError
from .inventory import parse_inventory
from .jwt import (expires_at as token_expires_at,
                  issued_at as token_issued_at,
                  lifetime_seconds as token_lifetime,
                  payload as token_payload)
from .partners import (PARTNERS, WEBREADER_CLIENT_TYPE,
                       WEBREADER_CLIENT_VERSION, WEBREADER_TOKEN_HEADERS)
from . import sync_data as _sync_data

CHROME_MAJOR = "153"
# Feste MIME-Typen fuer Formate, die mimetypes je nach System nicht kennt.
_FIXED_MIME_TYPES = {".epub": "application/epub+zip",
                     ".pdf": "application/pdf"}
_BROWSER_PLATFORM = {
    "win32": ("Windows NT 10.0; Win64; x64", '"Windows"'),
    "darwin": ("Macintosh; Intel Mac OS X 10_15_7", '"macOS"'),
}.get(sys.platform, ("X11; Linux x86_64", '"Linux"'))
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (%s) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/%s.0.0.0 Safari/537.36"
    % (_BROWSER_PLATFORM[0], CHROME_MAJOR)
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
        "Sec-CH-UA-Platform": _BROWSER_PLATFORM[1],
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Sec-GPC": "1",
    }


RELOGIN = "Bitte bei tolino anmelden."
# Gleichzeitige Einlösung eines Single-Use-Tokens beendet die Sitzung.
BUSY_MESSAGE = "Die Anmeldung wird gerade erneuert, bitte kurz warten."
RELOGIN_THEN = "Danach bei tolino anmelden."
def report_error(exc, context="", secrets=()):
    """Loggt Code, Text und Detail (ohne Token); gibt die Nutzermeldung zurück.

    Rückgabe: genau eine Zeile, «Kurztext (Code A01)». Der Dialog bekommt
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
    # Die Exception selbst formatieren: so landet auch die Kette
    # «Ursache -> Folgefehler» im Log, nicht nur der aktuelle Fehler.
    if (exc.__traceback__ is not None or exc.__cause__ is not None
            or exc.__context__ is not None):
        trace = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__))
        log_warning("Tolino-Details " + sanitize_error(trace, secrets))
    return "%s (Code %s)" % (shown, code)


def _log(message, method):
    """Schreibt eine Zeile ins Calibre-Log -- ohne Zugangsdaten.

    Die Meldung darf NIE einen Token-Wert enthalten. Ausserhalb von Calibre
    (Tests, direkter Import) bleibt der Aufruf still: das ist die Naht zum
    Log und darf selbst nicht werfen.
    """
    try:
        from calibre.utils.logging import default_log
        getattr(default_log, method)(str(message))
    except Exception:
        pass


def log_info(message):
    """Betriebsmeldung (Keep-alive gestartet/erneuert) ins Calibre-Log."""
    _log(message, "info")


def log_warning(message):
    """Warnung ins Calibre-Log."""
    _log(message, "warn")


BASE_URL = "https://bosh.pageplace.de/bosh/rest"


# Feldbefund: Nach registerhw wird die Geräte-ID verzögert akzeptiert.
_REGISTER_SETTLE_SECONDS = 1.5
# Feldbefund: Der erste Token-Aufruf kann am Bot-Schutz scheitern.
_WAF_RETRY_SECONDS = (2.0, 5.0)
# Gueltigkeitsvorlauf: ein Zehntel der Laufzeit, zwischen 30 s und 5 min.
_ACCESS_MARGIN_MIN = 30
_ACCESS_MARGIN_MAX = 300
# Liegt der Ausstellungszeitpunkt des Tokens weiter als das von der lokalen
# Uhr entfernt, ist die Uhr falsch gestellt; dann zaehlt nur die relative
# Laufzeit aus ``expires_in``.
_CLOCK_SKEW_TOLERANCE = 300

class _SafeRedirects(HTTPRedirectHandler):
    """Prueft ein Weiterleitungsziel, bevor urllib es kontaktiert.

    Die Pruefung des finalen Links nach `urlopen` kommt zu spaet: urllib
    hat das Ziel dann bereits angesprochen. Der Handler lehnt eine unsichere
    Weiterleitung deshalb schon beim Folgen ab.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(str(newurl))
        if target.scheme.casefold() != "https" or not target.hostname:
            # Der abgelehnte Antwort-Stream wird nicht mehr gebraucht und
            # sonst erst vom Garbage Collector geschlossen.
            if fp is not None:
                fp.close()
            raise TolinoApiError(
                "Der Download wurde auf eine unsichere URL umgeleitet.",
                code="N02",
                short="tolino hat einen unsicheren Download-Link geliefert.")
        return HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)


_DOWNLOAD_OPENER = build_opener(_SafeRedirects())


def _raise_transport_error(exc, secrets):
    """Raise a credential-safe error for the sole urllib transport."""
    if (isinstance(exc, TimeoutError)
            or isinstance(getattr(exc, "reason", None), TimeoutError)):
        raise TolinoApiError(
            "Zeitüberschreitung bei der Verbindung zu tolino.", code="N01",
            short="Zeitüberschreitung bei der Verbindung.") from exc
    raise TolinoApiError(
        "Übertragung an tolino fehlgeschlagen: %s"
        % sanitize_error(exc, secrets), code="N01",
        short="Die Verbindung zu tolino ist fehlgeschlagen.") from exc


_JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*"
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
    """Remove configured credentials and token-shaped values from any text."""
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
        return str(uuid.UUID(hex=compact))
    return text


def _access_deadline(access_token, lifetime, now=None):
    """Ablaufzeit des Access-Tokens (unix seconds) aus Antwort und Claims.

    ``expires_in`` kommt vom Server und gilt unabhaengig von der lokalen
    Uhr. Der ``exp``-Claim wird zusaetzlich genutzt, wenn das Token gerade
    ausgestellt wurde -- dann darf er nur verkuerzen, nie verlaengern. Weicht
    der Ausstellungszeitpunkt deutlich von der lokalen Uhr ab, ist die Uhr
    falsch gestellt: dann wuerde der Claim einen Token scheinbar ewig gelten
    lassen (jeder Aufruf laeuft in ein 401) oder sofort als abgelaufen
    melden (jeder Aufruf tauscht).
    """
    now = time.time() if now is None else now
    lifetime = max(0, int(lifetime or 0))
    margin = min(_ACCESS_MARGIN_MAX,
                 max(_ACCESS_MARGIN_MIN, (lifetime or 3600) // 10))
    deadline = now + max(0, lifetime - margin)
    issued, claim = token_issued_at(access_token), token_expires_at(access_token)
    if claim is None:
        return deadline
    if issued is not None and abs(issued - now) > _CLOCK_SKEW_TOLERANCE:
        log_warning("Tolino-Zugang: die Uhr weicht von den Token-Zeiten ab; "
                    "die Gültigkeit wird relativ gerechnet.")
        return deadline
    return min(deadline, claim - margin)


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


def _http_error_detail(raw_detail):
    """Normalize a Tolino error body for logs and user-facing messages."""
    detail = raw_detail
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
    return detail


class TolinoClient:
    """Small stdlib-only client for the endpoints used by the web reader."""

    def __init__(self, partner_id, hardware, refresh=None, timeout=45,
                 token_callback=None, hardware_callback=None,
                 token_rejected_callback=None, access=None,
                 access_expires_at=0, access_callback=None,
                 stored_token_callback=None):
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
        # Der Access-Token ist Wegwerfware; gespeichert spart er beim Start
        # einen Tausch und damit eine Rotation des Einmal-Tokens.
        self.access = str(access or "") or None
        self.expires_at = float(access_expires_at or 0)
        self.last_error_text = None
        self.token_callback = token_callback
        self.hardware_callback = hardware_callback
        self.token_rejected_callback = token_rejected_callback
        self.access_callback = access_callback
        # Der einzige Weg zum gespeicherten Token: der Vault reicht hier
        # seinen lock-freien Reader herein, auch fuer den ersten Tausch.
        self.stored_token_callback = stored_token_callback
        # Wird vor JEDEM Tausch aufgerufen; der Vault prueft hier, ob die
        # Rotation unter seinem Lock speicherbar ist.
        self.rotation_guard = None
        self.token_vault = None
        self.account_name = ""
        self._in_device_recovery = False
        self._hw_registered_now = False
        self._reuse_retried = False
        # Eine uebernommene Geraete-ID wird erst gespeichert, wenn der
        # Wiederholungsversuch bestaetigt, dass sie funktioniert.
        self._hw_adopted = False
        self._login_lock = threading.Lock()
        # Ein Lesevorgang der sync-data reicht, bis etwas geschrieben wird:
        # Lese-Status und Sammlungen brauchen dieselben Daten.
        self._sync_cache = None

    def _access_valid(self):
        """Ein noch gueltiger Zugang erspart einen zweiten Token-Tausch."""
        return bool(self.access) and time.time() < self.expires_at

    def login(self, force=False):
        """Login; ``force`` tauscht auch bei noch gueltigem Access-Token.

        Ein gueltiger Access-Token erspart sonst einen zweiten Tausch des
        Einmal-Tokens. Der Keep-alive muss dagegen faellige Refresh-Token
        wirklich rotieren.
        """
        if not force and self._access_valid():
            return self.refresh
        with self._login_lock:
            if not force and self._access_valid():
                return self.refresh
            if self.token_vault is None:
                return self._login()
            # Nach einer späten 401 zählt der zuletzt GESPEICHERTE Token:
            # der Keep-alive kann inzwischen rotiert haben.
            return self.token_vault.relogin(self)

    def _apply_token_response(self, data):
        """Store the token endpoint response; returns the rotated refresh."""
        if not data.get("access_token"):
            raise TolinoAuthError("Ungültige Anmelde-Antwort.", code="A01", detail="kein access_token")
        previous_refresh = self.refresh
        self.access = data["access_token"]
        new_refresh = normalize_refresh_token(
            data.get("refresh_token") or self.refresh)
        if not new_refresh:
            # Ein leerer Ersatz waere der Verlust der Anmeldung: der
            # gespeicherte Token bleibt unangetastet.
            self.access = None
            self.expires_at = 0
            raise TolinoAuthError("Ungültige Anmelde-Antwort.", code="A01",
                                  detail="kein refresh_token")
        # Den rotierten Single-Use-Token vor weiteren Anfragen speichern.
        self.refresh = new_refresh
        if new_refresh != previous_refresh and self.token_callback:
            try:
                # Rueckgabe ``False`` heisst: nicht gespeichert. ``None`` ist
                # bei allgemeinen Callbacks dagegen ein normaler Erfolg.
                stored = self.token_callback(self.refresh)
                detail = ""
            except Exception as exc:
                stored, detail = False, sanitize_error(
                    exc, (previous_refresh, new_refresh, self.access))
            if stored is False:
                # Der neue Token ist gueltig, aber nicht gesichert: die
                # Anmeldung darf nicht als gelungen gelten. Den bisherigen
                # gespeicherten Token nicht loeschen -- der Schreibfehler
                # sagt nichts ueber seine Gueltigkeit aus.
                self.access = None
                self.expires_at = 0
                raise TolinoAuthError(
                    "Der erneuerte Zugang konnte nicht gespeichert werden. " + RELOGIN,
                    code="A01",
                    detail=detail or "Einstellungen konnten nicht geschrieben werden."
                ) from None

        try:
            lifetime = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            lifetime = 0
        if lifetime <= 0:
            # Ohne expires_in gilt die Laufzeit aus dem Token selbst.
            lifetime = int(token_lifetime(self.access) or 3600)
        self.expires_at = _access_deadline(self.access, lifetime)
        # Ein neuer Token darf wieder einen Versuch mit einem inzwischen
        # gespeicherten Refresh-Token bekommen.
        self._reuse_retried = False
        self._notify_access()
        return self.refresh

    def _notify_access(self, token=None, expires_at=None):
        """Den Access-Token zum Speichern melden; diese Naht wirft nie."""
        callback = self.access_callback
        if not callback:
            return
        try:
            callback(self.access if token is None else token,
                     self.expires_at if expires_at is None else expires_at)
        except Exception as exc:
            log_warning("Access-Token konnte nicht gespeichert werden: %s"
                        % sanitize_error(exc, (self.refresh, self.access)))

    def _login(self):
        if not self.refresh:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN, code="A01")
        if self.rotation_guard is not None:
            self.rotation_guard()
        # Der Token, der hier wirklich eingeloest wird. Nur er darf bei einer
        # Ablehnung geloescht werden -- der Vault kann inzwischen einen
        # anderen (per `relogin` geladenen) gespeichert haben.
        used_refresh = self.refresh
        payload = {
            "client_id": self.partner["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": used_refresh,
        }
        # Ohne ``scope`` gilt der beim Ausstellen verwendete (RFC 6749, 6).
        # Ein hartkodierter Wert waere geraten: weicht er vom echten Scope ab,
        # scheitert die Erneuerung mit invalid_scope -- und das sieht aus wie
        # eine abgelaufene Anmeldung. Mitgeschickt wird nur, was im Token
        # selbst steht.
        scope = (token_payload(used_refresh) or {}).get("scope")
        if isinstance(scope, str) and scope.strip():
            payload["scope"] = scope.strip()
        try:
            data = self._request(self.partner["token_url"], "POST", payload,
                                 form=True, authenticated=False)
        except (TolinoApiError, TolinoAuthError) as exc:
            detail = str(exc)
            folded = detail.casefold()
            session_ended = "not active" in folded
            token_rejected = ("invalid_grant" in folded
                              or "reuse exceeded" in folded)
            if session_ended or token_rejected:
                # Ein anderer Lauf (Keep-alive, zweite Calibre-Instanz) kann
                # inzwischen rotiert haben: dann ist der gespeicherte Token
                # der einzige, der noch gilt -- und ein Versuch damit ist
                # kein zweiter Versuch mit dem abgelehnten. Genau EINER.
                newer = self._newer_stored_token(used_refresh)
                if newer:
                    log_warning("Tolino-Anmeldung: der gespeicherte Zugang "
                                "wurde inzwischen erneuert; neuer Versuch.")
                    self.refresh = newer
                    return self._login()
                # A stale refresh token must never be tried again.
                self.refresh = self.access = None
                self.expires_at = 0
                self._notify_access("", 0)
                self._notify_token_rejected(used_refresh)
            if session_ended:
                raise TolinoAuthError(
                    "Die tolino-Sitzung ist beendet. " + RELOGIN,
                    code="A01", detail=detail) from exc
            if token_rejected:
                raise TolinoAuthError(
                    "tolino hat die gespeicherte Anmeldung abgelehnt. " + RELOGIN,
                    code="A01", detail=detail) from exc
            if getattr(exc, "code", None) == "N01":
                # Ein Netzfehler ist keine abgelehnte Anmeldung: der Token
                # bleibt gültig und die Oberfläche verlangt keinen Neu-Login.
                raise
            raise TolinoAuthError(
                "Anmeldung bei tolino fehlgeschlagen: %s" % exc, code="A01",
                short="Die Anmeldung bei tolino ist fehlgeschlagen.")

        return self._apply_token_response(data)

    def _newer_stored_token(self, rejected):
        """Inzwischen gespeicherten Refresh-Token holen -- einmalig.

        Der Reader kommt vom Vault und liest ohne dessen Lock: dieser Pfad
        laeuft ueber `relogin()` bereits darunter, ein zweiter waere ein
        Deadlock. Ohne Reader gilt der Token als abgelehnt.
        """
        callback = self.stored_token_callback
        if self._reuse_retried or callback is None:
            return ""
        try:
            stored = normalize_refresh_token(callback())
        except Exception:
            return ""
        if not stored or stored == rejected:
            return ""
        self._reuse_retried = True
        return stored

    def _notify_token_rejected(self, rejected=""):
        """Den abgelehnten Token aus der Config loeschen lassen.

        ``rejected`` ist der in diesem Versuch verwendete Token; nur er darf
        verschwinden, falls inzwischen ein anderer gespeichert ist.
        """
        callback = self.token_rejected_callback
        if callback:
            try:
                callback(rejected)
            except Exception as exc:
                log_warning("Abgelehnter Token konnte nicht geloescht "
                            "werden: %s" % sanitize_error(exc, (rejected,)))

    def _request(self, url, method="GET", data=None, form=False,
                 authenticated=True, content_type=None, _extra_headers=None):
        retry = {"auth": True, "hardware": True, "waf": 0}
        try:
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
                    # HTTPError is itself a response wrapper; leaving it open
                    # leaks the socket until the garbage collector runs.
                    try:
                        raw_detail = exc.read().decode("utf-8", "replace")
                    finally:
                        exc.close()
                    # Kehrt nur zurück, wenn ein weiterer Versuch erlaubt ist.
                    self._handle_http_error(exc, raw_detail,
                                            url, authenticated, retry)
                    continue
                except OSError as exc:
                    _raise_transport_error(exc, (self.refresh, self.access))
                data = _decode_json(raw)
                # Nur ein AUTHENTIFIZIERTER Aufruf bestaetigt die uebernommene
                # Geraete-ID: der Token-Tausch selbst nutzt sie nicht.
                if authenticated and not retry["hardware"]:
                    self._confirm_adopted_hardware()
                return data
        except Exception:
            # Kein erfolgreicher Aufruf in DIESEM Versuch: eine uebernommene
            # Geraete-ID bleibt ungespeichert, statt spaeter von einem
            # beliebigen erfolgreichen Bestandsaufruf bestaetigt zu werden.
            self._hw_adopted = False
            raise

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

    def _retry_http_auth(self, exc, authenticated, retry):
        """Retry once after a 401 with a stale access token."""
        if not (exc.code == 401 and authenticated and self.refresh
                and retry["auth"]):
            return False
        detail = self.last_error_text.casefold()
        if "invalid_grant" in detail or "reuse exceeded" in detail:
            return False
        retry["auth"] = False
        self.access = None
        self.login()
        return True

    def _retry_http_hardware(self, exc, url, authenticated, retry):
        """Retry a BOSH 400 after renewing the device registration."""
        if not (exc.code == 400 and authenticated
                and url != self.partner.get("token_url")):
            return False
        if retry["hardware"]:
            self._hw_registered_now = False
            if self._recover_device_registration():
                retry["hardware"] = False
                if self._hw_registered_now:
                    time.sleep(_REGISTER_SETTLE_SECONDS)
                return True
        elif self._hw_registered_now:
            self._hw_registered_now = False
            time.sleep(_REGISTER_SETTLE_SECONDS)
            return True
        return False

    def _retry_http_waf(self, exc, url, retry):
        """Wait and retry a token-endpoint 403 that looks like a bot check."""
        if not (exc.code == 403
                and url == self.partner.get("token_url")
                and retry["waf"] < len(_WAF_RETRY_SECONDS)):
            return False
        if not any(marker in self.last_error_text.casefold()
                   for marker in (
                       "zugriff geblockt", "access denied", "captcha",
                       "cloudfront", "request blocked", "<!doctype html")):
            return False
        time.sleep(_WAF_RETRY_SECONDS[retry["waf"]])
        retry["waf"] += 1
        return True

    def _raise_http_auth_error(self, exc):
        """Raise the cleaned A01 error for a 401/403 response."""
        self.access = None
        self.expires_at = 0
        # Ein toter Access-Token darf den naechsten Start nicht in ein 401
        # schicken; der Refresh-Token bleibt unangetastet.
        self._notify_access("", 0)
        message = re.sub(r"<[^>]+>", " ", self.last_error_text or "")
        message = re.sub(r"\s+", " ", message).strip()
        message = message or "no response detail"
        raise TolinoAuthError(
            "tolino hat die Anmeldung abgelehnt (%s): %s"
            % (exc.code, message), code="A01",
            short="tolino hat die Anmeldung abgelehnt.")

    def _handle_http_error(self, exc, raw_detail, url, authenticated, retry):
        """Fehlerdetail merken und entweder wiederholen lassen oder werfen.

        `retry` führt die drei Wiederholungsgründe: abgelaufener
        Access-Token, unbekannte Geräte-Registrierung, WAF-Block.
        """
        self.last_error_text = sanitize_error(
            _http_error_detail(raw_detail),
            (self.refresh, self.access))[:500]

        if self._retry_http_auth(exc, authenticated, retry):
            return
        if self._retry_http_hardware(exc, url, authenticated, retry):
            return

        # Nur 401 und ein 403 am Token-Endpunkt sind eine Anmeldefrage. Ein
        # 403 anderswo ist meist eine Rechte- oder Quotabeschränkung; ein
        # einzelner Upload darf deshalb nicht den ganzen Lauf beenden.
        if (exc.code == 401
                or (exc.code == 403
                    and url == self.partner.get("token_url"))):
            if self._retry_http_waf(exc, url, retry):
                return
            self._raise_http_auth_error(exc)
        raise TolinoApiError(
            "tolino-Fehler (HTTP %s): %s"
            % (exc.code, self.last_error_text), code="N03",
            short="tolino meldet einen Fehler (HTTP %s)." % exc.code)


    def inventory(self):
        """Read the PublicationInventory form used by Tolino's API.

        Eine unbekannte Hülle ist ein Fehler, keine leere Bibliothek: sonst
        sähe der Vergleich eine Bibliothek ohne Bücher und plante Duplikate.
        """
        records, problems, complete = parse_inventory(
            self._request(BASE_URL + "/inventory/delta?strip=true"))
        if problems:
            log_warning("Tolino-Inventar: %s" % "; ".join(problems))
        if not complete:
            # Ein unvollstaendiger Bestand wuerde die Zuordnung zu den
            # fehlenden Datensaetzen loeschen und spaeter einen doppelten
            # Upload planen.
            raise TolinoApiError(
                "Der Bestand von tolino ist unvollständig (%s)."
                % "; ".join(problems), code="N02",
                short="tolino hat einen unvollständigen Bestand geliefert.")
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
        name = os.path.basename(str(filename)).replace("\\", "_")
        name = re.sub(r'["\r\n]', "_", name)
        suffix = os.path.splitext(name)[1].casefold()
        guessed_type = mimetypes.guess_type(name)
        mime = (_FIXED_MIME_TYPES.get(suffix)
                or next(iter(guessed_type), None)
                or "application/octet-stream")
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
            raise TolinoApiError("Upload ohne deliverableId in der Antwort.", code="N02", short="tolino hat den Upload nicht bestätigt.")
        return str(deliverable)

    def upload_cover(self, deliverable_id, data, suffix=".jpg"):
        # The cover endpoint requires the same control block as a normal
        # upload; without it the server answers HTTP 400 without details.
        control = json.dumps({
            "filesize": len(data),
            "transactionId": str(uuid.uuid4()),
        }, separators=(",", ":"))
        body, content_type = self._multipart(
            {"control": control, "deliverableId": deliverable_id}, data,
            "cover" + str(suffix or ""))
        self._request(BASE_URL + "/cover", "POST", body,
                      content_type=content_type)

    def delete(self, deliverable_id):
        self._request(BASE_URL + "/deletecontent?deliverableId="
                      + _url_quote(str(deliverable_id)))

    META_URL = BASE_URL + "/meta"

    def update_metadata(self, deliverable_id, fields):
        """Update title/author/isbn on an existing book instead of
        re-uploading the file.

        GET /meta?deliverableId=... returns the book with its "metadata"
        dict; the merged dict goes back via PUT as {"uploadMetaData": …}.
        """
        if not isinstance(fields, dict) or not fields:
            raise TolinoApiError("Metadaten-Update ohne Felder.", code="E99", short="Es gibt nichts zu aktualisieren.")
        url = self.META_URL + "/?deliverableId=" + _url_quote(str(deliverable_id))
        book = self._request(url)
        metadata = book.get("metadata") if isinstance(book, dict) else None
        if not isinstance(metadata, dict):
            raise TolinoApiError(
                "Tolino hat keine Metadaten zu %s geliefert." % deliverable_id,
                code="N02", short="tolino hat keine Daten zu diesem Buch geliefert.")
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
                code="N02",
                short="tolino hat die Metadaten nicht übernommen.")
        return verified_metadata


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
            raise TolinoApiError("Geräteliste leer.", code="N02", short="tolino meldet kein registriertes Gerät.")
        devices.sort(key=lambda item: str(item.get("deviceLastUsage", "")))
        hardware = devices[-1].get("deviceId")
        if not hardware:
            raise TolinoApiError("Gerät ohne deviceId.", code="N02", short="Die Geräteliste von tolino ist unvollständig.")
        return str(hardware)

    def _confirm_adopted_hardware(self):
        """Eine uebernommene Geraete-ID erst nach einem geglueckten Aufruf sichern.

        Die Wiederbelebung laeuft bei jedem HTTP 400 -- auch wenn der 400
        gar nichts mit der Hardware zu tun hat (etwa ein abgelehnter
        Metadaten-Patch). Wuerde die ID sofort gespeichert, tauschte ein
        solcher Fehler die Geraete-ID im Konto, ohne dass die ID je
        funktioniert hat.
        """
        if not self._hw_adopted:
            return
        self._hw_adopted = False
        self._notify_hardware()

    def _notify_hardware(self):
        """Tell the UI about an adopted hardware id; never raises.

        ``False`` vom Callback heisst «nicht gespeichert»: die uebernommene ID
        waere dann nur im Speicher und der naechste Lauf erzeugte wieder eine
        neue. Das ist ein Fehlschlag und kein stiller Erfolg.
        """
        callback = self.hardware_callback
        if not callback:
            return
        try:
            stored = callback(self.hardware)
            detail = ""
        except Exception as exc:
            stored, detail = False, sanitize_error(
                exc, (self.refresh, self.access))
        if stored is False:
            log_warning("Geräte-ID konnte nicht gespeichert werden: %s"
                        % (detail or "Einstellungen konnten nicht "
                           "geschrieben werden."))

    def _register_hardware(self):
        """Register this hardware id at the BOSH service (reference flow)."""
        payload = {"hardware_name": "other"}
        extra = {
            "hardware_type": "HTML5",
            "client_type": WEBREADER_CLIENT_TYPE,
            "client_version": WEBREADER_CLIENT_VERSION,
        }
        try:
            self._request(BASE_URL + "/v2/registerhw", "POST", payload,
                          content_type="application/json",
                          _extra_headers=extra)
            return True
        except TolinoError:
            return False

    def _recover_device_registration(self):
        """Adopt or register a hardware id the BOSH service accepts."""
        if self._in_device_recovery:
            return False
        self._in_device_recovery = True
        try:
            try:
                registered = normalize_hardware_id(self.fetch_hardware_id())
            except TolinoError:
                registered = ""
            if registered:
                if registered == normalize_hardware_id(self.hardware):
                    # Already THE registered device: the 400 has another
                    # cause -- do not mask it behind a pointless retry.
                    return False
                self.hardware = registered
                self._hw_adopted = True
                return True
            registered_ok = self._register_hardware()
            self._hw_registered_now = bool(registered_ok)
            return registered_ok
        finally:
            self._in_device_recovery = False


    def download(self, deliverable_id):
        """Download one book from the cloud as bytes."""
        # Die Cloud erwartet die Kennung base64-kodiert und antwortet auf
        # die rohe ID mit HTTP 404; base64-Sonderzeichen werden kodiert.
        token = _url_quote(base64.b64encode(
            str(deliverable_id).encode("utf-8")).decode("ascii"), safe="")
        info_url = (BASE_URL + "/cloud/downloadinfo/%s/%s/type/external-download"
                    % (token, token))
        info = self._request(info_url)
        content_url = (info.get("DownloadInfo") or {}).get("contentUrl")
        if not content_url:
            raise TolinoApiError("Kein Download-Link in der Antwort.", code="N02", short="tolino hat keinen Download-Link geliefert.")
        try:
            parsed_content_url = urlsplit(str(content_url))
        except (TypeError, ValueError) as exc:
            raise TolinoApiError("Ungültiger Download-Link.", code="N02",
                                 short="tolino hat einen ungültigen Download-Link geliefert.") from exc
        if parsed_content_url.scheme.casefold() != "https" or not parsed_content_url.hostname:
            raise TolinoApiError("Unsicherer Download-Link.", code="N02",
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
            # `_SafeRedirects` prueft jede Weiterleitung, bevor sie folgt.
            with _DOWNLOAD_OPENER.open(request,
                                       timeout=self.timeout) as response:
                content = response.read()
        except OSError as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            raise TolinoApiError("Download von Tolino fehlgeschlagen: %s" %
                                 sanitize_error(exc, (self.refresh, self.access)),
                                 code="N02", short="Der Download von Tolino ist fehlgeschlagen.")
        return content


    SYNC_DATA_URL = BASE_URL + "/sync-data?paths=publications"

    def _sync_patch(self, payload):
        # Nach Schreibvorgängen muss die Revision neu gelesen werden.
        self._sync_cache = None
        data = self._request(self.SYNC_DATA_URL, "PATCH", payload,
                             content_type="application/json")
        return data if isinstance(data, dict) else {}

    def sync_snapshot(self):
        """(revision, patches) aus EINEM Lesevorgang, mehrfach nutzbar.

        Sammlungen und Lese-Status liegen in denselben sync-data-Daten;
        ohne Zwischenspeicher würde derselbe Stand zweimal geholt. Jeder
        Schreibvorgang verwirft ihn wieder (siehe `_sync_patch`).
        """
        if self._sync_cache is None:
            data = self._sync_patch({"revision": None, "patches": []})
            revision = data.get("revision")
            # Die API laesst `patches` weg, wenn seit der Revision nichts
            # zu synchronisieren ist. Nur ein vorhandener, falscher Typ ist
            # eine unvollstaendige Antwort; `null` bleibt ebenfalls Fehler.
            if (revision in (None, "")
                    or ("patches" in data and not isinstance(
                        data["patches"], (list, tuple)))):
                raise TolinoApiError(
                    "Die Cloud-Antwort zu den Markierungen ist unvollständig.",
                    code="N02",
                    short="Die Antwort von tolino ist unvollständig.")
            self._sync_cache = (revision, list(data.get("patches") or ()))
        return self._sync_cache

    def read_ids(self):
        """Bücher, die im Konto als gelesen markiert sind.

        Quelle ist die System-Marke des Web Readers – dieselbe, die
        `mark_read_many` setzt. Damit kann der Bestandsvergleich die
        Spalte «Gelesen» vorbelegen.
        """
        _revision, patches = self.sync_snapshot()
        return _sync_data.read_ids(patches)

    def progress_by_id(self):
        """Lesefortschritt je tolino-ID aus denselben sync-data-Daten.

        Die Werte sind reine Anzeige: Das Plugin schreibt keine Leseposition
        in die Cloud zurück. Der Snapshot wird mit `read_ids` geteilt.
        """
        _revision, patches = self.sync_snapshot()
        return _sync_data.reading_progress(patches)

    def _current_revision(self, revision):
        """Revision fuer einen Schreibvorgang: die aktuelle, nie None.

        Ein PATCH ohne Revision kann den vorhandenen Stand ersetzen und
        damit fremde Marken (Lese-Status, andere Sammlungen) verlieren.
        Die Revision wird deshalb gelesen und mitgeschickt.
        """
        if revision is None:
            revision, _patches = self.sync_snapshot()
        if revision in (None, ""):
            # Ein PATCH ohne Revision kann vorhandene Marken ersetzen.
            raise TolinoApiError(
                "Die Cloud hat keine Revision zu den Markierungen geliefert.",
                code="N02",
                short="Die Antwort von tolino ist unvollständig; bitte neu "
                      "vergleichen.")
        return revision

    def add_to_collection(self, book_id, collection_name, revision=None):
        """Add an uploaded/purchased book to a named collection.

        `revision` kann aus dem vorigen Aufruf weitergereicht werden, damit
        mehrere Zuordnungen nur eine Cloud-Anfrage zum Lesen brauchen.
        """
        patch = _sync_data.tag_patch(
            "add", book_id, collection_name, "collection")
        data = self._sync_patch({
            "revision": self._current_revision(revision), "patches": [patch]})
        return data.get("revision")

    def mark_read_many(self, book_ids, finished=True):
        """Set (or clear) the finished mark for several books at once.

        Ein Lesevorgang, ein PATCH: alle Bücher bekommen dieselbe Revision.
        Bereits gesetzte Marken werden nicht erneut geschrieben, fehlende
        beim Entfernen übersprungen.
        """
        # Doppelte IDs würden denselben Patch mehrfach erzeugen. `dict`
        # behält die erste Reihenfolge und entfernt nur echte Duplikate.
        ids = list(dict.fromkeys(
            str(book_id) for book_id in book_ids or []
            if str(book_id or "").strip()))
        if not ids:
            raise TolinoApiError(
                "Gelesen-Markierung ohne Buch.", code="E99",
                short="Es wurde kein Buch ausgewählt.")
        revision, patches = self.sync_snapshot()
        active = _sync_data.read_tag_patches(patches)
        updates = []
        for book_id in ids:
            # Beide Schreibweisen direkt nachschlagen: das Mapping ist schon
            # nach (Buch, Kategorie) indiziert.
            book = str(book_id)
            marks = [(category, active[(book, category)])
                     for category in _sync_data.READ_TAG_CATEGORIES
                     if (book, category) in active]
            if finished:
                if not marks:
                    updates.append(_sync_data.tag_patch(
                        "add", book_id, _sync_data.READ_TAG, "system"))
                continue
            # Beide Schreibweisen entfernen: der Reader legt die Marke je
            # nach Quelle als System- oder Sammlungs-Tag ab.
            for category, item in marks:
                updates.append(_sync_data.tag_patch(
                    "remove", book_id, _sync_data.READ_TAG, category,
                    revision=item["value"].get("revision"),
                    tag_id=_sync_data.tag_id_from_patch(item)))
        if not updates:
            return revision
        data = self._sync_patch({"revision": revision, "patches": updates})
        return data.get("revision")

    def collection_pairs(self):
        """Return the {(deliverable, collection)} pairs set in the cloud.

        Reads sync-data once so the series reconcile only has to
        PATCH genuinely missing assignments.
        """
        _revision, patches = self.sync_snapshot()
        return _sync_data.collection_pairs(patches)

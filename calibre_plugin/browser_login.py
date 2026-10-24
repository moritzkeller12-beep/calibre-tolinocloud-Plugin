"""Browser sign-in and token extraction from Tolino's Web Reader."""
import json
import time
from urllib.parse import urlencode

from .partners import PARTNERS
from .tolino import (RELOGIN, RELOGIN_THEN, TolinoAuthError,
                     TolinoClient, _NO_AUTO_LOGIN, log_warning,
                     normalize_hardware_id, sanitize_error)
from .transport import _compact_error_text
from .token_candidates import (
    _candidate_age_note, _filter_live_candidates, _jwt_payload,
    _refresh_token_iat_sort_key, _normalize_live_grab,
)

LOGIN_TIMEOUT = 300


def _exchange_fresh_response(partner_id, hardware, candidate, data):
    """Exchange via in-page fetch and apply the response with full logic."""
    client = TolinoClient(partner_id, hardware or "")
    client.refresh = candidate
    client._apply_token_response(data)
    return client.refresh or candidate, client.hardware or hardware


def _exchange_grabbed_token(partner_id, hardware, grabbed, fresh):
    """Exchange the freshest grabbed candidate; returns (refresh, hardware)."""
    # Keycloak-Refresh-JWTs tragen "typ": "Refresh"; Access-Tokens sind
    # kurzlebig und haben denselben JWT-Aufbau -- ein Access-Token am
    # Token-Endpunkt "zu verbrauchen" hilft niemandem und der eigentliche
    # Refresh-Token wird dann womoeglich gar nicht erst probiert.
    def _is_refresh_typ(token):
        payload = _jwt_payload(token) or {}
        return str(payload.get("typ", "")).casefold() == "refresh"

    ordered = sorted(fresh, key=_refresh_token_iat_sort_key, reverse=True)
    ordered.sort(key=lambda token: 0 if _is_refresh_typ(token) else 1)
    candidate = ordered[0]
    hardware_candidates = [normalize_hardware_id(value)
                           for value in (grabbed.get("hardware") or ())]
    hardware_candidates = [value for value in hardware_candidates if value]
    hardware_id = hardware_candidates[0] if hardware_candidates else hardware
    partner = PARTNERS[int(partner_id)]

    # Grant zuerst IN der Reader-Seite (in-page fetch): dieselben TLS-/Header-
    # Fingerprints wie der Reader selbst -- der eigene POST wird vom
    # Bot-Schutz gelegentlich mit WAF-403 abgewiesen.
    try:
        from . import cdp as cdp_module
        ws_url = cdp_module.reader_ws_url()
    except Exception:
        cdp_module = None
        ws_url = None
    if ws_url:
        form = urlencode({
            "client_id": partner["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": candidate,
            "scope": partner["scope"],
        })
        status, body = cdp_module.exchange_refresh_in_browser(
            ws_url, partner["token_url"], form)
        if status == 200:
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            if isinstance(data, dict) and data.get("access_token"):
                return _exchange_fresh_response(
                    partner_id, hardware_id, candidate, data)
        browser_note = "Browser-Tausch HTTP %s: %s" % (
            status, _compact_error_text(sanitize_error(body))[:200])
    else:
        browser_note = "kein Anmeldefenster offen"

    # Fallback: Plugin-eigener POST (alter Weg, WAF-anfaellig).
    client = TolinoClient(partner_id, hardware_id or "")
    client.refresh = candidate
    try:
        client._login()
    except TolinoAuthError as exc:
        # The exchange consumed one grant; a retry would only replay.
        raise TolinoAuthError(
            "Tolino hat den Zugang aus dem Anmeldefenster abgelehnt. "
            + RELOGIN, code="A02",
            detail="%s; Browser-Tausch: %s" % (exc, browser_note)) from exc
    return client.refresh or candidate, client.hardware or hardware_id


# Nach Ablehnung wird auf eine NEU geschriebene Kopie gewartet: der Reader
# rotiert selbst alle 40-60 s. Lesen, was der Reader schreibt -- kein
# erzwungener Reload, keine Netzwerk-Interception.
_NEW_TOKEN_ATTEMPTS = 30
_NEW_TOKEN_INTERVAL = 3
# Wartezeit richtet sich nach dem Fensterzustand:
# * Endpunkt stumm: nach _WINDOW_MISSES_BEFORE_QUIT abbrechen;
# * Fenster offen ohne Reader-Tab (Sitzung tot):
#   _LOGIN_PAGE_EXTRA_ATTEMPTS weitere Versuche -- dort kann neu
#   angemeldet und das Fenster wiederverwendet werden.
_LOGIN_PAGE_EXTRA_ATTEMPTS = 30
_WINDOW_MISSES_BEFORE_QUIT = 3


def _close_window_after_grab(progress=None):
    """Anmeldefenster nach der Uebernahme schliessen.

    Zwei Versuche: Chromium braucht beim Herunterfahren manchmal laenger
    als die zwei Sekunden des ersten. Bleibt es offen, sagt die
    Statuszeile das -- der uebernommene Token waere sonst still gefaehrdet,
    weil der Reader ihn weiterrotieren wuerde (Reuse-Schutz).
    """
    from . import cdp as cdp_module
    try:
        if cdp_module.close_grabber_window():
            return True
        if cdp_module.close_grabber_window(timeout=5):
            return True
    except Exception as exc:
        log_warning("Anmeldefenster konnte nicht geschlossen werden: %s"
                    % sanitize_error(exc))
    if progress:
        progress("Zugang übernommen – das Anmeldefenster liess sich nicht "
                 "automatisch schliessen. Bitte von Hand schliessen, damit "
                 "der Zugang gültig bleibt.")
    return False


def grab_live_refresh(partner_id, hardware, timeout=300, progress=None):
    """Read the Web Reader's CURRENT token via a private CDP window.

    Returns (rotated_refresh, hardware_id). Raises TolinoAuthError when
    no Chromium browser is installed – the caller reports that and
    offers the manual route (access key in the settings) instead.
    """
    from . import cdp as cdp_module
    grabbed = _normalize_live_grab(cdp_module.grab_live_tokens(
        partner_id, timeout=timeout, progress=progress))
    tried = set()
    first_error = ""
    attempt = 0
    endpoint_misses = 0
    at_login_page = False
    while True:
        attempt += 1
        fresh = [token for token in _filter_live_candidates(
            (grabbed or {}).get("refresh") or ()) if token not in tried]
        if fresh:
            if progress:
                progress("Prüfe den gelesenen Zugang bei Tolino …")
            try:
                result = _exchange_grabbed_token(partner_id, hardware,
                                                 grabbed, fresh)
            except TolinoAuthError as exc:
                # Endgueltige Ablehnung: diese Kopie und ihre Geschwister
                # derselben Sitzung sind verbraucht und werden nie wieder
                # probiert (Reuse-Schutz) -- stattdessen wird unten auf
                # eine NEU geschriebene Kopie gewartet.
                first_error = str(exc)
                tried.update((grabbed or {}).get("refresh") or ())
                if progress:
                    progress("Zugang abgelehnt – warte auf einen frischen "
                             "Zugang aus dem Web Reader …")
            else:
                # Der Token gehoert ab jetzt Calibre: ein offener Reader rotiert weiter
                # und wuerde die uebernommene Kopie verbrennen (Reuse-Schutz).
                if progress:
                    progress("Zugang übernommen – das Anmeldefenster "
                             "wird geschlossen.")
                _close_window_after_grab(progress)
                return result
        limit = _NEW_TOKEN_ATTEMPTS + (
            _LOGIN_PAGE_EXTRA_ATTEMPTS if at_login_page else 0)
        if attempt >= limit:
            break
        if progress:
            if at_login_page:
                progress("Im Anmeldefenster anmelden, bis die Bücherliste "
                         "lädt – der Zugang wird automatisch übernommen "
                         "(Versuch %d) …" % attempt)
            else:
                progress("Warte auf den Zugang aus dem Web Reader "
                         "(Versuch %d von %d) – bitte angemeldet bleiben …"
                         % (attempt, _NEW_TOKEN_ATTEMPTS))
        time.sleep(_NEW_TOKEN_INTERVAL)
        more = _normalize_live_grab(
            cdp_module.grab_once_from_grabber())
        if more and more.get("refresh"):
            grabbed = more
            continue
        # Nichts lesbar -- WARUM? Fenster weg (Endpunkt stumm, nach drei
        # Fehlschlaegen abbrechen) vs. Fenster offen ohne Reader-Tab (Sitzung
        # tot: weiter warten und Neu-Anmeldung vor Ort anfordern).
        try:
            window = cdp_module.grabber_window_state()
        except Exception:
            window = None
        if window is None:
            continue
        if window.get("window"):
            endpoint_misses = 0
            at_login_page = not window.get("reader_tab")
            continue
        endpoint_misses += 1
        if endpoint_misses >= _WINDOW_MISSES_BEFORE_QUIT:
            break
    try:
        state = cdp_module.describe_grab_state()
    except Exception:
        state = "Zustand der Seite nicht lesbar"
    try:
        window = cdp_module.grabber_window_state()
    except Exception:
        window = {"window": False, "reader_tab": False}
    if window.get("window") and not window.get("reader_tab"):
        # Fenster auf der Anmeldeseite: NICHT schliessen -- dort kann neu
        # angemeldet werden; launch_reader_window wiederverwendet es.
        code = "B01"
        action = ("Das Anmeldefenster ist offen, aber nicht angemeldet. "
                  "Bitte dort anmelden, bis die Bücherliste lädt. "
                  + RELOGIN_THEN)
    else:
        # Nichts Tauschbares im Fenster: offen zu lassen wuerde den
        # naechsten Versuch wieder zum Loop machen (dieselbe tote Kopie,
        # dieselbe Ablehnung). Schliessen -> naechster Start mit frischem
        # Profil und echter Anmeldeseite statt gecachter Buecherliste.
        cdp_module.close_grabber_window()
        code = "B02"
        action = ("Das Anmeldefenster wurde geschlossen, weil der Zugang "
                  "verbraucht war. " + RELOGIN)
    age_note = _candidate_age_note(
        tried or (grabbed or {}).get("refresh") or ())
    raise TolinoAuthError(
        action, code=code,
        detail="Seite: %s%s%s" % (
            state,
            "; Letzter Fehler: %s" % first_error if first_error else "",
            "; %s" % age_note if age_note else ""))


def browser_login(partner_id, hardware, timeout=LOGIN_TIMEOUT,
                  progress=None):
    """Sign in via the partner's OAuth authorization endpoint."""
    partner = PARTNERS.get(int(partner_id))

    if not partner or not partner.get("token_url"):
        raise TolinoAuthError(_NO_AUTO_LOGIN, code="A05", detail="token_url fehlt")

    # Die OAuth-Seite speichert den Token im gemeinsamen Web Reader. Lies
    # ihn aus dem Plugin-Browser; ein lokaler Callback funktioniert hier nicht.
    return grab_live_refresh(int(partner_id), hardware, timeout=timeout,
                             progress=progress)



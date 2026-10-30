"""Browser sign-in and token extraction from Tolino's Web Reader."""
import time

from . import cdp as cdp_module
from .tolino import (RELOGIN, RELOGIN_THEN, TolinoAuthError,
                     TolinoClient, log_info, log_warning,
                     normalize_hardware_id, sanitize_error)
from .token_candidates import (
    _candidate_age_note, _filter_live_candidates, _jwt_payload,
    _refresh_token_iat_sort_key, _normalize_live_grab,
)

LOGIN_TIMEOUT = 300


def _exchange_grabbed_token(partner_id, hardware, grabbed, fresh):
    """Exchange the freshest grabbed candidate; returns (refresh, hardware)."""
    # Keycloak-Refresh-JWTs tragen "typ": "Refresh"; Access-Tokens sind
    # kurzlebig und haben denselben JWT-Aufbau -- ein Access-Token am
    # Token-Endpunkt "zu verbrauchen" hilft niemandem und der eigentliche
    # Refresh-Token wird dann womoeglich gar nicht erst probiert.
    def _is_refresh_typ(token):
        payload = _jwt_payload(token) or {}
        return str(payload.get("typ", "")).casefold() == "refresh"

    candidate = min(
        fresh,
        key=lambda token: (not _is_refresh_typ(token),
                           -_refresh_token_iat_sort_key(token)))
    hardware_candidates = [normalize_hardware_id(value)
                           for value in (grabbed.get("hardware") or ())]
    hardware_candidates = [value for value in hardware_candidates if value]
    hardware_id = hardware_candidates[0] if hardware_candidates else hardware
    client = TolinoClient(partner_id, hardware_id or "")
    client.refresh = candidate
    try:
        client._login()
    except TolinoAuthError as exc:
        raise TolinoAuthError(
            "Tolino hat den Zugang aus dem Anmeldefenster abgelehnt. "
            + RELOGIN, code="A02", detail=str(exc)) from exc
    log_info("Tolino-Token-Tausch: HTTP (Plugin).")
    return client.refresh or candidate, client.hardware or hardware_id


# The reader rotates tokens on its own; poll without replaying a rejected grant.
_NEW_TOKEN_INTERVAL = 3
_CLOSED_WINDOW_MISSES = 3
_WINDOW_CHECK_INTERVAL = 5.0


def _close_window_after_grab(progress=None):
    """Anmeldefenster nach der Uebernahme schliessen.

    Zwei Versuche: Chromium braucht beim Herunterfahren manchmal laenger
    als die zwei Sekunden des ersten. Bleibt es offen, sagt die
    Statuszeile das -- der uebernommene Token waere sonst still gefaehrdet,
    weil der Reader ihn weiterrotieren wuerde (Reuse-Schutz).
    """
    for timeout in (2, 5):
        try:
            if cdp_module.close_grabber_window(timeout=timeout):
                return True
        except Exception as exc:
            log_warning("Anmeldefenster konnte nicht geschlossen werden: %s"
                        % sanitize_error(exc))
    if progress:
        progress("Zugang übernommen – das Anmeldefenster liess sich nicht "
                 "automatisch schliessen. Bitte von Hand schliessen, damit "
                 "der Zugang gültig bleibt.")
    return False


def _fresh_grab_tokens(grabbed, rejected):
    """Candidates not yet rejected by the token endpoint."""
    return [token for token in _filter_live_candidates(
        (grabbed or {}).get("refresh") or ()) if token not in rejected]


def _grabber_window_state(getter):
    """Read the grabber window; a transient CDP error is no state."""
    try:
        return getter()
    except Exception:
        return None


def _raise_grab_failure(last_error, grabbed, rejected):
    """Raise the final B02 error with age/error context."""
    details = []
    if last_error:
        details.append("Letzter Fehler: " + sanitize_error(last_error))
    candidates = rejected or (grabbed or {}).get("refresh") or ()
    age_note = _candidate_age_note(candidates)
    if age_note:
        details.append(age_note)
    raise TolinoAuthError(
        "Das Anmeldefenster wurde geschlossen, weil kein gültiger Zugang "
        "gelesen werden konnte. " + RELOGIN,
        code="B02", detail="; ".join(details))


def grab_live_refresh(partner_id, hardware, timeout=300, progress=None):
    """Read and exchange the Web Reader's CURRENT token in one wait loop.

    Das Zeitbudget beginnt vor dem ersten Grab und gilt fuer Fensterstart,
    Lesen und Tausch. Returns (rotated_refresh, hardware_id). Raises
    TolinoAuthError when no Chromium browser is installed – the caller
    reports that and offers the manual route instead.
    """
    deadline = time.monotonic() + max(0, timeout)
    port = cdp_module.launch_reader_window()
    if progress:
        progress("Anmeldefenster geöffnet – bitte anmelden und warten, "
                 "bis die Bücherliste lädt.")

    grabbed = {"refresh": [], "hardware": []}
    rejected = set()
    last_error = ""
    window = None
    endpoint_misses = 0
    last_window_check = -_WINDOW_CHECK_INTERVAL
    saw_reader = False

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        more = _normalize_live_grab(cdp_module.grab_once_from_grabber(
            port, timeout=max(0.1, min(12, remaining))))
        if more and more.get("refresh"):
            grabbed = more
            saw_reader = True
            endpoint_misses = 0
            fresh = _fresh_grab_tokens(grabbed, rejected)
            if fresh:
                if progress:
                    progress("Prüfe den gelesenen Zugang bei Tolino …")
                try:
                    result = _exchange_grabbed_token(partner_id, hardware,
                                                     grabbed, fresh)
                except TolinoAuthError as exc:
                    # A rejected single-use grant and its session siblings are spent.
                    rejected.update((grabbed or {}).get("refresh") or ())
                    last_error = str(exc)
                    if progress:
                        progress("Zugang abgelehnt – warte auf einen frischen "
                                 "Zugang aus dem Web Reader …")
                else:
                    if progress:
                        progress("Zugang übernommen – das Anmeldefenster "
                                 "wird geschlossen.")
                    _close_window_after_grab(progress)
                    return result

        now = time.monotonic()
        if now - last_window_check >= _WINDOW_CHECK_INTERVAL:
            last_window_check = now
            window = _grabber_window_state(cdp_module.grabber_window_state)
            if window is not None:
                if window.get("window"):
                    endpoint_misses = 0
                    saw_reader = saw_reader or bool(window.get("reader_tab"))
                else:
                    endpoint_misses += 1
                    if endpoint_misses >= _CLOSED_WINDOW_MISSES:
                        # Nach einem gelesenen Token bleibt die Ablehnung
                        # die genauere Meldung; ohne Token ist das Fenster
                        # wirklich vorzeitig verschwunden.
                        if saw_reader:
                            break
                        if progress:
                            progress("Anmeldefenster geschlossen – abgebrochen.")
                        raise TolinoAuthError(
                            "Das Anmeldefenster wurde geschlossen, bevor die "
                            "Übernahme fertig war. Bitte erneut versuchen und "
                            "es offen lassen.", code="B03")

        if (progress and window and window.get("window")
                and not window.get("reader_tab")):
            progress("Im Anmeldefenster anmelden, bis die Bücherliste "
                     "lädt – der Zugang wird automatisch übernommen …")
        elif progress and not saw_reader:
            progress("Der Web Reader wird geladen …")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_NEW_TOKEN_INTERVAL, remaining))

    if not window:
        window = _grabber_window_state(cdp_module.grabber_window_state)
    if window and window.get("window") and not window.get("reader_tab"):
        # Leave the partner login page open so the user can finish signing in.
        if saw_reader:
            raise TolinoAuthError(
                "Das Anmeldefenster ist offen, aber noch nicht angemeldet. "
                "Bitte dort anmelden, bis die Bücherliste lädt. " + RELOGIN_THEN,
                code="B01", detail=last_error)
        raise TolinoAuthError(
            "Zeitüberschreitung: Die Anmeldung wurde nicht abgeschlossen. "
            + RELOGIN, code="B04", detail="kein Web-Reader-Tab")

    cdp_module.close_grabber_window()
    if saw_reader:
        _raise_grab_failure(last_error, grabbed, rejected)
    raise TolinoAuthError(
        "Zeitüberschreitung: Die Anmeldung wurde nicht abgeschlossen. "
        + RELOGIN, code="B04", detail="kein Web-Reader-Tab")


def browser_login(partner_id, hardware, timeout=LOGIN_TIMEOUT,
                  progress=None):
    """Sign in via the partner's OAuth authorization endpoint."""
    # Die OAuth-Seite speichert den Token im gemeinsamen Web Reader. Lies
    # ihn aus dem Plugin-Browser; ein lokaler Callback funktioniert hier nicht.
    return grab_live_refresh(int(partner_id), hardware, timeout=timeout,
                             progress=progress)

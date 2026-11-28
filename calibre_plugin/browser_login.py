"""Browser sign-in and token extraction from Tolino's Web Reader."""
import time

from . import cdp as cdp_module
from .partners import UNKNOWN_SHOP, partner_from_token
from .tolino import (RELOGIN, RELOGIN_THEN, TolinoAuthError,
                     TolinoClient, log_info, log_warning,
                     normalize_hardware_id, sanitize_error)
from .jwt import issued_at, payload
from .token_candidates import (
    _candidate_age_note, _filter_live_candidates, _normalize_live_grab,
)

LOGIN_TIMEOUT = 300


def _exchange_grabbed_token(hardware, grabbed, fresh):
    """Tausche den frischesten Kandidaten; der Shop kommt aus ``iss``.

    Returns (refresh, hardware, partner_id). Ohne erkennbaren Shop wird
    nichts getauscht -- ein geratener Endpunkt verbrennt den Einmal-Token.
    """
    # Keycloak-Refresh-JWTs tragen "typ": "Refresh"; Access-Tokens sehen
    # gleich aus. Ein Access-Token am Token-Endpunkt zu verbrauchen hilft
    # niemandem und der echte Refresh-Token wuerde gar nicht probiert.
    known = {token: partner_from_token(token) for token in fresh}
    if not any(known.values()):
        raise TolinoAuthError(UNKNOWN_SHOP + RELOGIN, code="B03")
    candidate = min((token for token in fresh if known[token]),
                    key=lambda token: (
                        str((payload(token) or {}).get("typ", "")).casefold()
                        != "refresh", -(issued_at(token) or 0.0)))
    partner_id = known[candidate]
    hardware_id = next((value for value in (
        normalize_hardware_id(item) for item in (grabbed.get("hardware") or ()))
        if value), hardware)
    client = TolinoClient(partner_id, hardware_id or "")
    client.refresh = candidate
    try:
        client._login()
    except TolinoAuthError as exc:
        raise TolinoAuthError(
            "Tolino hat den Zugang aus dem Anmeldefenster abgelehnt. "
            + RELOGIN, code="A01", detail=str(exc)) from exc
    log_info("Tolino-Token-Tausch: HTTP (Plugin).")
    return (client.refresh or candidate, client.hardware or hardware_id,
            partner_id)


# The reader rotates tokens on its own; poll without replaying a rejected grant.
_NEW_TOKEN_INTERVAL = 3
_CLOSED_WINDOW_MISSES = 3
_WINDOW_CHECK_INTERVAL = 5.0


def _close_window(timeout=2):
    """Anmeldefenster schliessen; ein Fehler wird geloggt, nicht geworfen."""
    try:
        return cdp_module.close_grabber_window(timeout=timeout)
    except Exception as exc:
        log_warning("Anmeldefenster konnte nicht geschlossen werden: %s"
                    % sanitize_error(exc))
        return False


def _notify(progress, text):
    """Statusmeldung an den optionalen Fortschritts-Callback."""
    if progress:
        progress(text)


def _login_page_open(window):
    """Fenster offen, aber noch kein Web-Reader-Tab: die Anmeldemaske."""
    return bool(window and window.get("window")
                and not window.get("reader_tab"))


def _close_window_after_grab(progress=None):
    """Anmeldefenster nach der Uebernahme schliessen.

    Zwei Versuche: Chromium braucht beim Herunterfahren manchmal laenger
    als die zwei Sekunden des ersten. Bleibt es offen, sagt die
    Statuszeile das -- der uebernommene Token waere sonst still gefaehrdet,
    weil der Reader ihn weiterrotieren wuerde (Reuse-Schutz).
    """
    for timeout in (2, 5):
        if _close_window(timeout):
            return True
    _notify(progress, "Zugang übernommen – das Anmeldefenster liess sich "
                      "nicht automatisch schliessen. Bitte von Hand "
                      "schliessen, damit der Zugang gültig bleibt.")
    return False


def _exchange_live_grab(hardware, grabbed, rejected, progress=None):
    """Try the freshest live token; returns (result, error)."""
    fresh = [token for token in _filter_live_candidates(grabbed["refresh"])
             if token not in rejected]
    if not fresh:
        return None, ""
    _notify(progress, "Prüfe den gelesenen Zugang bei Tolino …")
    try:
        result = _exchange_grabbed_token(hardware, grabbed, fresh)
    except TolinoAuthError as exc:
        if getattr(exc, "code", "") == "B03":
            # Ohne erkennbaren Shop gibt es nichts abzuwarten: Fenster zu,
            # klare Meldung, kein verbranntes Token.
            _close_window()
            raise
        # A rejected single-use grant and its session siblings are spent.
        rejected.update(grabbed["refresh"])
        _notify(progress, "Zugang abgelehnt – warte auf einen frischen "
                          "Zugang aus dem Web Reader …")
        return None, str(exc)
    _notify(progress, "Zugang übernommen – das Anmeldefenster "
                      "wird geschlossen.")
    _close_window_after_grab(progress)
    return result, ""


def _raise_grab_end(window, saw_reader, grabbed, rejected, last_error):
    """Raise the matching B02 error after the wait loop."""
    login_page_open = _login_page_open(window)
    if login_page_open and saw_reader:
        # Die Partner-Anmeldeseite bleibt offen, damit man sich anmelden kann.
        raise TolinoAuthError(
            "Das Anmeldefenster ist offen, aber noch nicht angemeldet. "
            "Bitte dort anmelden, bis die Bücherliste lädt. " + RELOGIN_THEN,
            code="B02", detail=last_error)
    if not login_page_open:
        _close_window()
        if saw_reader:
            details = []
            if last_error:
                details.append("Letzter Fehler: " + sanitize_error(last_error))
            candidates = rejected or grabbed.get("refresh") or ()
            age_note = _candidate_age_note(candidates)
            if age_note:
                details.append(age_note)
            raise TolinoAuthError(
                "Das Anmeldefenster wurde geschlossen, weil kein gültiger "
                "Zugang gelesen werden konnte. " + RELOGIN,
                code="B02", detail="; ".join(details))
    raise TolinoAuthError(
        "Zeitüberschreitung: Die Anmeldung wurde nicht abgeschlossen. "
        + RELOGIN, code="B02", detail="kein Web-Reader-Tab")


def grab_live_refresh(hardware, timeout=300, progress=None):
    """Read and exchange the Web Reader's CURRENT token in one wait loop.

    Das Zeitbudget beginnt vor dem ersten Grab und gilt fuer Fensterstart,
    Lesen und Tausch. Returns (rotated_refresh, hardware_id, partner_id);
    den Buchhändler bestimmt der ``iss``-Claim des gelesenen Tokens. Raises
    TolinoAuthError when no Chromium browser is installed – the caller
    reports that and offers the manual route instead.
    """
    deadline = time.monotonic() + max(0, timeout)
    cdp_module.launch_reader_window()
    _notify(progress, "Anmeldefenster geöffnet – bitte anmelden und "
                      "warten, bis die Bücherliste lädt.")

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
        try:
            more = _normalize_live_grab(cdp_module.grab_once_from_grabber(
                timeout=max(0.1, min(12, remaining))))
        except Exception:
            # Auch ein fehlender Decoder darf Chromium und das Temp-Profil
            # nicht bis zum nächsten Calibre-Start offen lassen.
            _close_window()
            raise
        if more and more.get("refresh"):
            grabbed = more
            saw_reader = True
            endpoint_misses = 0
            try:
                result, error = _exchange_live_grab(
                    hardware, grabbed, rejected, progress)
            except TolinoAuthError:
                # Der B03-Pfad hat das Fenster bereits geschlossen.
                raise
            except Exception:
                # Auch ein Netzwehler (N01) beim Tausch darf das Fenster und
                # sein Temp-Profil nicht bis zum naechsten Start offen lassen.
                _close_window()
                raise
            if result is not None:
                return result
            if error:
                last_error = error

        now = time.monotonic()
        if now - last_window_check >= _WINDOW_CHECK_INTERVAL:
            last_window_check = now
            window = cdp_module.grabber_window_state()
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
                    _notify(progress,
                            "Anmeldefenster geschlossen – abgebrochen.")
                    raise TolinoAuthError(
                        "Das Anmeldefenster wurde geschlossen, bevor die "
                        "Übernahme fertig war. Bitte erneut versuchen und "
                        "es offen lassen.", code="B02")

        if progress and _login_page_open(window):
            progress("Im Anmeldefenster anmelden, bis die Bücherliste "
                     "lädt – der Zugang wird automatisch übernommen …")
        elif progress and not saw_reader:
            progress("Der Web Reader wird geladen …")

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(_NEW_TOKEN_INTERVAL, remaining))

    if not window:
        window = cdp_module.grabber_window_state()
    _raise_grab_end(window, saw_reader, grabbed, rejected, last_error)

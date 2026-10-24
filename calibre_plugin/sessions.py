"""Serialize refresh-token use and keep stored Tolino sessions alive."""
import contextlib
import threading

from .tolino import (RELOGIN, TolinoClient, log_info, log_warning,
                     normalize_refresh_token, sanitize_error)

# --- Token Keep-alive --------------------------------------------------------
# Keycloak-Refresh-Tokens sterben nach ~1 h Leerlauf; der Keep-alive
# rotiert sie im Intervall still und speichert sie ueber den Callback.
_KEEPALIVE_LOCK = threading.Lock()
_KEEPALIVE_STOP = threading.Event()

# Ein laufender Sync und der Keep-alive duerfen NICHT gleichzeitig denselben
# Refresh-Token einloesen -- der Reuse-Schutz von Keycloak beendet die
# Sitzung dann sofort. Waehrend einer Sitzung haelt der Keep-alive deshalb
# still.
#
# Gezaehlt statt nur gemerkt: Vorbereitung und Sync koennen sich ueberlappen,
# und in der Zeit zwischen den beiden Fenstern (der Vergleich kann beliebig
# lange offen bleiben) darf der Keep-alive den Token rotieren, ohne dass der
# anschliessende Lauf mit dem ALTEN Token startet.
_SESSION_LOCK = threading.Lock()
_SESSIONS_ACTIVE = 0


@contextlib.contextmanager
def token_session():
    """Haelt den Keep-alive still, solange der Block laeuft.

    Der Refresh-Token ist single-use. Jede Stelle, die ihn einloest, oeffnet
    eine Sitzung; verschachtelte Sitzungen geben den Zaehler erst nach dem
    letzten Austritt wieder frei -- auch wenn ein Zweig mit Fehler endet
    (das ``finally`` raeumt auf).
    """
    global _SESSIONS_ACTIVE
    with _SESSION_LOCK:
        _SESSIONS_ACTIVE += 1
    try:
        yield
    finally:
        with _SESSION_LOCK:
            if _SESSIONS_ACTIVE > 0:
                _SESSIONS_ACTIVE -= 1


def sessions_active():
    """Anzahl der gerade offenen Token-Sitzungen (Keep-alive-Sperre)."""
    with _SESSION_LOCK:
        return _SESSIONS_ACTIVE


def lock_keepalive(timeout=5):
    """Kurz auf eine laufende Keep-alive-Rotation warten.

    True heisst: die Sperre wird gehalten und muss mit `unlock_keepalive`
    wieder freigegeben werden. False heisst: es rotiert gerade jemand, der
    nicht gewartet hat -- der Aufrufer darf jetzt nicht einloesen.
    """
    return _KEEPALIVE_LOCK.acquire(timeout=max(0, timeout))


def unlock_keepalive():
    """Gibt die Keep-alive-Sperre frei, falls sie gehalten wird."""
    try:
        _KEEPALIVE_LOCK.release()
    except RuntimeError:
        pass


def _rotation_persister(initial, persist):
    """Callback that persists each newly rotated token exactly once.

    ``login()`` may report the same value twice (token response and the
    explicit call afterwards). The first value is already stored, so only a
    real change is written. ``persist`` may be None: rotation still works,
    it is just not stored. The callback deliberately knows nothing about
    tokens in logs or error messages.
    """
    last = {"value": initial}

    def save(value):
        if value and value != last["value"]:
            last["value"] = value
            if persist is not None:
                persist(value)

    return save


def keepalive_refresh(get_credentials, persist):
    """Rotate every stored login in one keep-alive lock phase.

    A mapping is accepted for older callers; the UI supplies a list of
    account snapshots. Each rotated token is persisted to its captured
    account name, never to whichever account happens to be active later.
    """
    if sessions_active():
        return None
    if not _KEEPALIVE_LOCK.acquire(blocking=False):
        return None
    try:
        snapshot = get_credentials() or {}
        accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else None
        if accounts is None:
            accounts = [snapshot] if isinstance(snapshot, dict) else []
        rotated = []
        for creds in accounts:
            if not isinstance(creds, dict):
                continue
            token = str(creds.get("refresh_token") or "").strip()
            if not token:
                continue
            account_name = str(creds.get("account_name") or creds.get("name") or "")

            def store(value, captured_name=account_name):
                if persist is not None:
                    persist(value, captured_name)

            save_token = _rotation_persister(token, store)
            try:
                client = TolinoClient(
                    creds.get("partner_id"), creds.get("hardware_id") or "",
                    token, token_callback=save_token)
                client.login()
                save_token(client.refresh)
                rotated.append(client.refresh)
            except Exception as exc:
                log_warning("Tolino-Keep-alive fehlgeschlagen (%s): %s"
                            % (account_name or "Konto", sanitize_error(exc, (token,))))
        if rotated:
            log_info("Tolino-Keep-alive: %d %s erneuert."
                     % (len(rotated), "Konto" if len(rotated) == 1 else "Konten"))
        return rotated[-1] if rotated else None
    finally:
        _KEEPALIVE_LOCK.release()


def check_stored_login(partner_id, hardware, refresh, persist=None,
                       lock_timeout=5):
    """Einen GESPEICHERTEN Zugang pruefen, ohne ein Anmeldefenster zu oeffnen.

    Der Anmeldeknopf fragt damit zuerst: funktioniert der vorhandene Zugang
    noch? Nur wenn nicht, geht das Browser-Fenster auf.

    Antwort in drei klar getrennten Faellen, damit der Aufrufer entscheiden
    kann -- ein reines None/Token waere nicht unterscheidbar:

    ``("valid", token)``   Zugang funktioniert; `token` ist der ggf. rotierte
                            Refresh-Token (schon gespeichert, wenn `persist`
                            uebergeben wurde).
    ``("busy", "")``       Es laeuft gerade ein Sync oder eine andere
                            Rotation: der Zugang wurde NICHT geprueft.
    ``("invalid", text)``   Tolino hat den Zugang abgelehnt; `text` ist der
                            Grund (ohne Token-Inhalt).

    Der Refresh-Token ist single-use. Deshalb laeuft die Pruefung unter
    demselben Lock wie der Keep-alive, und ein waehrenddessen gestarteter
    Sync bricht sie ab, statt den Token doppelt einzuloesen.
    """
    token = normalize_refresh_token(refresh)
    if not token:
        return "invalid", "Noch nicht angemeldet. " + RELOGIN
    if sessions_active():
        return "busy", ""
    acquired = False
    try:
        acquired = _KEEPALIVE_LOCK.acquire(timeout=max(0, lock_timeout))
        if not acquired:
            return "busy", ""
        if sessions_active():
            # Die Sitzung hat in der Zwischenzeit begonnen und loest
            # denselben Token ein -- jetzt nicht auch noch.
            return "busy", ""
        _persist = _rotation_persister(token, persist)

        client = TolinoClient(partner_id, hardware, token,
                              token_callback=_persist)
        client.login()
        _persist(client.refresh)
        return "valid", client.refresh
    except Exception as exc:
        return "invalid", sanitize_error(exc, (token,))
    finally:
        if acquired:
            _KEEPALIVE_LOCK.release()


def start_token_keepalive(get_credentials, persist, interval=45 * 60):
    """Rotate the stored refresh token every `interval` seconds."""
    if getattr(start_token_keepalive, "_started", False):
        return None
    start_token_keepalive._started = True

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            keepalive_refresh(get_credentials, persist)

    thread = threading.Thread(target=loop, name="tolino-token-keepalive",
                              daemon=True)
    thread.start()
    log_info("Tolino-Keep-alive gestartet: alle %d Minuten."
             % (max(60, interval) // 60))
    return thread



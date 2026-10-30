"""Serialize single-use refresh-token grants through one shared vault."""
import contextlib
import threading

from .config import CredentialStore
from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoAuthError, TolinoClient,
                     log_info, log_warning, normalize_refresh_token,
                     sanitize_error)


class TokenVault:
    """Read current credentials and rotate their token under one lock."""

    def __init__(self):
        self._lock = threading.Lock()

    def login(self, store, lock_timeout=5):
        """Return a logged-in client; the lock covers only the login phase."""
        with self._held_lock(lock_timeout):
            values, refresh = self._freshest_token(store)
            client = TolinoClient(
                store.values.get("partner_id"), values.get("hardware_id"),
                refresh, token_callback=store.save_refresh,
                hardware_callback=store.save_hardware)
            # Der Client speichert eine Rotation selbst beim Eintreffen der
            # Antwort (genau einmal, ueber seinen Callback).
            # Erst anmelden, dann den Vault einhängen: `login()` würde
            # sonst über `relogin()` den bereits gehaltenen Lock anfordern.
            client.login()
            client.token_vault = self
            client.credential_store = store
        return client

    @contextlib.contextmanager
    def _held_lock(self, timeout):
        """Vault-Lock (alle Konten teilen ihn) für den Block halten."""
        if not self._lock.acquire(timeout=max(0, timeout)):
            raise TolinoAuthError(BUSY_MESSAGE, code="S91")
        try:
            yield
        finally:
            self._lock.release()

    @staticmethod
    def _freshest_token(store):
        """Werte und frischer Token des Kontos; leer heisst «nicht angemeldet»."""
        values = store.freshest()
        refresh = normalize_refresh_token(values.get("refresh_token"))
        if not refresh:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN,
                                  code="A03")
        return values, refresh

    def relogin(self, client, timeout=5):
        """Refresh once under the shared lock using the freshest saved token."""
        store = client.credential_store
        with self._held_lock(timeout):
            values, refresh = self._freshest_token(store)
            client.refresh = refresh
            client.hardware = values.get("hardware_id") or client.hardware
            # `_login()` meldet den neuen Token über den Callback, der ihn
            # bereits persistiert -- kein zweites Speichern hier.
            return client._login()

    def keepalive_refresh(self, get_credentials):
        """Refresh saved accounts through the same locked client factory."""
        snapshot = get_credentials() or {}
        accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else None
        if accounts is None:
            accounts = [snapshot] if isinstance(snapshot, dict) else []
        rotated = []
        for values in accounts:
            if not isinstance(values, dict):
                continue
            account = str(values.get("account_name") or values.get("name") or "")
            store = CredentialStore(values, account)
            try:
                client = self.login(store, lock_timeout=0)
                rotated.append(client.refresh)
            except Exception as exc:
                # Ein belegter Lock (S91) ist kein Fehler -- nächste Runde.
                if isinstance(exc, TolinoAuthError) and exc.code == "S91":
                    continue
                log_warning("Tolino-Keep-alive fehlgeschlagen (%s): %s"
                            % (account or "Konto",
                               sanitize_error(exc, (store.refresh_token,))))
        if rotated:
            log_info("Tolino-Keep-alive: %d %s erneuert."
                     % (len(rotated), "Konto" if len(rotated) == 1 else "Konten"))
        return rotated[-1] if rotated else None

TOKEN_VAULT = TokenVault()
_KEEPALIVE_STOP = threading.Event()
_KEEPALIVE_THREAD = None


def make_client(store, lock_timeout=5):
    """Log in with the freshest stored token for an account."""
    return TOKEN_VAULT.login(store, lock_timeout)


def start_token_keepalive(get_credentials, interval=45 * 60):
    """Rotate saved refresh tokens periodically in a daemon thread."""
    global _KEEPALIVE_THREAD
    if _KEEPALIVE_THREAD is not None:
        return None
    _KEEPALIVE_STOP.clear()

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            TOKEN_VAULT.keepalive_refresh(get_credentials)

    _KEEPALIVE_THREAD = threading.Thread(
        target=loop, name="tolino-token-keepalive", daemon=True)
    _KEEPALIVE_THREAD.start()
    log_info("Tolino-Keep-alive gestartet: alle %d Minuten."
             % (max(60, interval) // 60))
    return _KEEPALIVE_THREAD


def stop_token_keepalive(timeout=2):
    """Keep-alive-Schleife anhalten -- aus dem Shutdown des Plugins.

    Calibre prüft beim Herunterfahren, ob noch Threads laufen, und meldet
    sonst «Thread still alive: tolino-token-keepalive». Ein atexit-Haken
    käme dafür zu spät: er läuft erst am Ende des Interpreters, die Prüfung
    davor.
    """
    global _KEEPALIVE_THREAD
    _KEEPALIVE_STOP.set()
    thread, _KEEPALIVE_THREAD = _KEEPALIVE_THREAD, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=max(0.1, timeout))


def check_stored_login(store, lock_timeout=5):
    """Check one saved account through the shared refresh-token lock."""
    try:
        TOKEN_VAULT.login(store, lock_timeout)
        return "valid", ""
    except TolinoAuthError as exc:
        if exc.code == "S91":
            return "busy", ""
        return "invalid", sanitize_error(exc, (store.refresh_token,))
    except Exception as exc:
        return "invalid", sanitize_error(exc, (store.refresh_token,))

"""Serialize single-use refresh-token grants through one shared vault.

Zugangsdaten haben genau eine Quelle: das Konto in der Config. Der Vault
liest Token und Geraete-ID unter seinem Lock und schreibt Rotationen
sofort zurueck; niemand sonst haelt eine zweite Kopie.
"""
import contextlib
import threading

from .config import (account_by_name, account_secrets, clear_refresh_token,
                     save_hardware_id, save_refresh_token, settings)
from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoAuthError, TolinoClient,
                     log_info, log_warning, normalize_refresh_token,
                     sanitize_error)


class TokenVault:
    """Read stored credentials and rotate their token under one lock."""

    def __init__(self):
        self._lock = threading.Lock()

    def login(self, account_name, partner_id, lock_timeout=5):
        """Return a logged-in client; the lock covers only the login phase."""
        with self._held_lock(lock_timeout):
            values = self._stored_credentials(account_name)
            client = TolinoClient(
                partner_id, values["hardware_id"], values["refresh_token"],
                token_callback=lambda token: save_refresh_token(
                    account_name, token),
                hardware_callback=lambda hardware: save_hardware_id(
                    account_name, hardware))
            client.token_rejected_callback = lambda: clear_refresh_token(
                account_name)
            if not values["hardware_id"]:
                # Die erzeugte ID gehoert ins Konto, sonst wechselt sie je Lauf.
                save_hardware_id(account_name, client.hardware)
            # Erst anmelden, dann den Vault einhaengen: `login()` wuerde
            # sonst ueber `relogin()` den bereits gehaltenen Lock anfordern.
            client.login()
            client.account_name = account_name
            client.token_vault = self
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
    def _stored_credentials(account_name):
        """Token und Geraete-ID des Kontos; leer heisst «nicht angemeldet»."""
        account = account_by_name(account_name) or {}
        refresh = normalize_refresh_token(account.get("refresh_token"))
        if not refresh:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN,
                                  code="A01")
        return {"refresh_token": refresh,
                "hardware_id": str(account.get("hardware_id") or "")}

    def relogin(self, client, timeout=5):
        """Refresh once under the shared lock using the freshest saved token."""
        with self._held_lock(timeout):
            values = self._stored_credentials(client.account_name)
            client.refresh = values["refresh_token"]
            client.hardware = values["hardware_id"] or client.hardware
            # `_login()` meldet den neuen Token ueber den Callback, der ihn
            # bereits persistiert -- kein zweites Speichern hier.
            return client._login()

    def keepalive_refresh(self):
        """Refresh saved accounts; return only the number of rotations."""
        rotated = 0
        for account in settings()["accounts"]:
            name = str(account.get("name") or "")
            if not name or not str(account.get("refresh_token") or "").strip():
                continue
            try:
                self.login(name, account.get("partner_id"), lock_timeout=0)
                rotated += 1
            except Exception as exc:
                # Ein belegter Lock (S91) ist kein Fehler -- nächste Runde.
                if isinstance(exc, TolinoAuthError) and exc.code == "S91":
                    continue
                log_warning("Tolino-Keep-alive fehlgeschlagen (%s): %s"
                            % (name, sanitize_error(
                                exc, account_secrets(name))))
        if rotated:
            log_info("Tolino-Keep-alive: %d %s erneuert."
                     % (rotated, "Konto" if rotated == 1 else "Konten"))
        # Kein Token zurueckgeben: der Wert ist ein Geheimnis und wird nur
        # zum Speichern durch den Client-Callback gebraucht.
        return rotated

TOKEN_VAULT = TokenVault()
_KEEPALIVE_STOP = threading.Event()
_KEEPALIVE_THREAD = None


def start_token_keepalive(interval=45 * 60):
    """Rotate saved refresh tokens periodically in a daemon thread."""
    global _KEEPALIVE_THREAD
    if _KEEPALIVE_THREAD is not None:
        return None
    _KEEPALIVE_STOP.clear()

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            TOKEN_VAULT.keepalive_refresh()

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


def check_stored_login(account_name, partner_id, lock_timeout=5):
    """Check one saved account through the shared refresh-token lock."""
    try:
        TOKEN_VAULT.login(account_name, partner_id, lock_timeout)
        return "valid", ""
    except Exception as exc:
        if isinstance(exc, TolinoAuthError) and exc.code == "S91":
            return "busy", ""
        return "invalid", sanitize_error(exc, account_secrets(account_name))

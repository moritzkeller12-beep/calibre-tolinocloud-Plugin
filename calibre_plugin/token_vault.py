"""Serialize single-use refresh-token grants through one shared vault.

Zugangsdaten haben genau eine Quelle: das Konto in der Config. Der Vault
liest Token und Geraete-ID unter seinem Lock und schreibt Rotationen
sofort zurueck; niemand sonst haelt eine zweite Kopie.
"""
import contextlib
import threading

from .config import (account_secrets, account_summary, clear_refresh_token,
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
        account = account_summary(account_name) or {}
        refresh = normalize_refresh_token(account.get("refresh_token"))
        if not refresh:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN,
                                  code="A01")
        return {"refresh_token": refresh,
                "hardware_id": str(account.get("hardware_id") or "")}

    def relogin(self, client, timeout=30):
        """Refresh once under the shared lock using the freshest saved token.

        Der Keep-alive haelt den Lock ueber den Netzaufruf und dessen
        WAF-Retries. Ein Sync, der waehrenddessen neu anmelden muss, wartet
        besser kurz, statt mit S91 fatal abzubrechen.
        """
        with self._held_lock(timeout):
            values = self._stored_credentials(client.account_name)
            client.refresh = values["refresh_token"]
            client.hardware = values["hardware_id"] or client.hardware
            # `_login()` meldet den neuen Token ueber den Callback, der ihn
            # bereits persistiert -- kein zweites Speichern hier.
            return client._login()

    def keepalive_refresh(self):
        """Refresh saved accounts; return only the number of rotations."""
        try:
            accounts = settings()["accounts"]
        except Exception:
            log_warning("Tolino-Keep-alive: Einstellungen konnten nicht "
                        "gelesen werden.")
            return 0
        rotated = 0
        for account in accounts:
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
_KEEPALIVE_THREAD = None
_KEEPALIVE_STOP = None
_KEEPALIVE_LOCK = threading.Lock()


def start_token_keepalive(interval=45 * 60):
    """Rotate saved refresh tokens periodically in one daemon thread."""
    global _KEEPALIVE_THREAD, _KEEPALIVE_STOP
    wait = max(60, interval)
    with _KEEPALIVE_LOCK:
        if _KEEPALIVE_THREAD is not None and _KEEPALIVE_THREAD.is_alive():
            return None
        # Jeder Lauf bekommt sein eigenes Stop-Event; ein alter Thread kann
        # einen Neustart damit nicht versehentlich wieder aktivieren.
        stop = threading.Event()

        def loop():
            while not stop.wait(wait):
                TOKEN_VAULT.keepalive_refresh()

        thread = threading.Thread(
            target=loop, name="tolino-token-keepalive", daemon=True)
        _KEEPALIVE_THREAD, _KEEPALIVE_STOP = thread, stop
        thread.start()
    log_info("Tolino-Keep-alive gestartet: alle %d Minuten." % (wait // 60))
    return thread


def stop_token_keepalive(timeout=2):
    """Keep-alive-Schleife anhalten -- aus dem Shutdown des Plugins.

    Ein Thread, der nach dem Timeout noch in einer Netzwerkoperation steckt,
    bleibt referenziert; solange er lebt, startet kein zweiter Keep-alive.
    """
    global _KEEPALIVE_THREAD, _KEEPALIVE_STOP
    with _KEEPALIVE_LOCK:
        thread, stop = _KEEPALIVE_THREAD, _KEEPALIVE_STOP
        if thread is None:
            return True
        stop.set()
        if thread.is_alive():
            thread.join(timeout=max(0.1, timeout))
        if thread.is_alive():
            return False
        _KEEPALIVE_THREAD = _KEEPALIVE_STOP = None
        return True


def check_stored_login(account_name, partner_id, lock_timeout=5):
    """Check one saved account through the shared refresh-token lock."""
    try:
        TOKEN_VAULT.login(account_name, partner_id, lock_timeout)
        return "valid", ""
    except Exception as exc:
        if isinstance(exc, TolinoAuthError) and exc.code == "S91":
            return "busy", ""
        return "invalid", sanitize_error(exc, account_secrets(account_name))

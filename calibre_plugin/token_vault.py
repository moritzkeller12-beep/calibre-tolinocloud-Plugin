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
            save_rotation = self._callback_for_value(
                store.refresh_token, store.save_refresh)
            client = TolinoClient(
                store.values.get("partner_id"), values.get("hardware_id"),
                refresh, token_callback=save_rotation,
                hardware_callback=store.save_hardware)
            # Erst anmelden, dann den Vault einhängen: `login()` würde
            # sonst über `relogin()` den bereits gehaltenen Lock anfordern.
            client.login()
            save_rotation(client.refresh)
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
            result = client._login()
            if client.refresh:
                store.save_refresh(client.refresh)
            return result

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

    @staticmethod
    def _callback_for_value(initial, persist):
        previous = [initial]

        def save(refresh):
            value = normalize_refresh_token(refresh)
            if value and value != previous[0]:
                previous[0] = value
                if persist is not None:
                    persist(value)

        return save


TOKEN_VAULT = TokenVault()
_KEEPALIVE_STOP = threading.Event()
_KEEPALIVE_STARTED = threading.Event()


def make_client(store, lock_timeout=5):
    """Log in with the freshest stored token for an account."""
    return TOKEN_VAULT.login(store, lock_timeout)


def start_token_keepalive(get_credentials, interval=45 * 60):
    """Rotate saved refresh tokens periodically in a daemon thread."""
    if _KEEPALIVE_STARTED.is_set():
        return None
    _KEEPALIVE_STARTED.set()

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            TOKEN_VAULT.keepalive_refresh(get_credentials)

    thread = threading.Thread(target=loop, name="tolino-token-keepalive",
                              daemon=True)
    thread.start()
    log_info("Tolino-Keep-alive gestartet: alle %d Minuten."
             % (max(60, interval) // 60))
    return thread


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

"""Serialize single-use refresh-token grants through one shared vault."""
import contextlib
import threading

from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoAuthError, TolinoClient,
                     log_info, log_warning, normalize_refresh_token,
                     sanitize_error)


class TokenVault:
    """Read current credentials and rotate their token under one lock."""

    def __init__(self):
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def login(self, store, lock_timeout=5, client_factory=None):
        """Yield a logged-in client; the lock covers only the login phase."""
        with self._held_lock(lock_timeout):
            values, refresh = self._freshest_token(store)
            save_rotation = self._callback_for_value(
                store.refresh_token, store.save_refresh)
            factory = client_factory or TolinoClient
            client = factory(
                store.values.get("partner_id"), values.get("hardware_id"),
                refresh, token_callback=save_rotation,
                hardware_callback=store.save_hardware)
            client.token_lock = self._lock
            client.token_vault = self
            client.token_lock_timeout = lock_timeout
            client.credential_store = store
            client.token_lock_held = True
            try:
                client.login()
                save_rotation(client.refresh)
            finally:
                client.token_lock_held = False
        yield client

    @contextlib.contextmanager
    def _held_lock(self, timeout):
        """Konto-Lock für die Dauer des Blocks halten -- sonst «belegt»."""
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

    def relogin(self, client):
        """Refresh once under the account lock using the freshest saved token."""
        store = client.credential_store
        with self._held_lock(client.token_lock_timeout):
            values, refresh = self._freshest_token(store)
            client.refresh = refresh
            client.hardware = values.get("hardware_id") or client.hardware
            result = client._login()
            if client.refresh:
                store.save_refresh(client.refresh)
            return result

    def keepalive_refresh(self, get_credentials, persist):
        """Rotate available saved accounts without replaying a busy token."""
        snapshot = get_credentials() or {}
        accounts = snapshot.get("accounts") if isinstance(snapshot, dict) else None
        if accounts is None:
            accounts = [snapshot] if isinstance(snapshot, dict) else []
        rotated = []
        for creds in accounts:
            if not isinstance(creds, dict):
                continue
            account = str(creds.get("account_name") or creds.get("name") or "")
            from .config import CredentialStore
            store = CredentialStore(creds, account)
            try:
                if not self._lock.acquire(blocking=False):
                    continue
                try:
                    values = store.freshest()
                    token = normalize_refresh_token(values.get("refresh_token"))
                    if not token:
                        continue

                    def save(refresh):
                        store.values["refresh_token"] = refresh
                        if persist is not None:
                            persist(refresh, account)

                    save_rotation = self._callback_for_value(token, save)
                    client = TolinoClient(
                        values.get("partner_id"), values.get("hardware_id") or "",
                        token, token_callback=save_rotation)
                    client.token_lock = self._lock
                    client.token_lock_held = True
                    try:
                        client.login()
                        save_rotation(client.refresh)
                    finally:
                        client.token_lock_held = False
                    rotated.append(client.refresh)
                finally:
                    self._lock.release()
            except Exception as exc:
                if isinstance(exc, TolinoAuthError) and exc.code == "S91":
                    # Belegt statt kaputt: der nächste Durchlauf versucht es
                    # erneut -- ein Logeintrag wäre hier nur Rauschen.
                    continue
                log_warning("Tolino-Keep-alive fehlgeschlagen (%s): %s"
                            % (account or "Konto",
                               sanitize_error(exc, (creds.get("refresh_token"),))))
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


def start_token_keepalive(get_credentials, persist, interval=45 * 60):
    """Rotate saved refresh tokens periodically in a daemon thread."""
    if getattr(start_token_keepalive, "_started", False):
        return None
    start_token_keepalive._started = True

    def loop():
        while not _KEEPALIVE_STOP.wait(max(60, interval)):
            TOKEN_VAULT.keepalive_refresh(get_credentials, persist)

    thread = threading.Thread(target=loop, name="tolino-token-keepalive",
                              daemon=True)
    thread.start()
    log_info("Tolino-Keep-alive gestartet: alle %d Minuten."
             % (max(60, interval) // 60))
    return thread


def check_stored_login(store, lock_timeout=5):
    """Check one saved account through the shared refresh-token lock."""
    try:
        with TOKEN_VAULT.login(store, lock_timeout) as client:
            return "valid", client.refresh
    except TolinoAuthError as exc:
        if exc.code == "S91":
            return "busy", ""
        return "invalid", sanitize_error(exc, (store.refresh_token,))
    except Exception as exc:
        return "invalid", sanitize_error(exc, (store.refresh_token,))

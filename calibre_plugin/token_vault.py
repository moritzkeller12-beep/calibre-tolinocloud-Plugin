"""Serialize single-use refresh-token grants through one shared vault.

Zugangsdaten haben genau eine Quelle: das Konto in der Config. Der Vault
liest Token und Geraete-ID unter seinem Lock und schreibt Rotationen
sofort zurueck; niemand sonst haelt eine zweite Kopie.
"""
import contextlib
import threading
import time

from .config import (account_secrets, account_tokens,
                     clear_refresh_token_if_unchanged, save_access_token,
                     save_hardware_id, save_refresh_token, settings,
                     settings_write_lock, settings_writable)
from .jwt import lifetime_seconds
from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoAuthError, TolinoClient,
                     TolinoError, log_info, log_warning,
                     normalize_refresh_token, sanitize_error)

# Der Keep-alive folgt der Laufzeit des Refresh-Tokens: ein Fuenftel Vorlauf,
# mindestens zwei Minuten, und nicht oefter als alle fuenf Minuten.
KEEPALIVE_MIN_DELAY = 5 * 60
REFRESH_MARGIN_MIN = 120
REFRESH_MARGIN_FRACTION = 0.2


def _lock_busy(exc):
    """Der gemeinsame Vault-Lock war belegt -- kein Fehler des Kontos."""
    return isinstance(exc, TolinoAuthError) and exc.code == "S91"


def _seconds(value):
    """Zahl aus dem Einstellungsspeicher (JSON liefert int/float/str) oder 0."""
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _partner_number(value):
    """Buchhaendler-Nummer des Kontos; ohne Angabe der Standardpartner."""
    if value is None:
        return 1
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class TokenVault:
    """Read stored credentials and rotate their token under one lock."""

    def __init__(self):
        self._lock = threading.Lock()

    def login(self, account_name, partner_id=None, lock_timeout=5, *,
              keepalive=False):
        """Login-Client; der Shop steht im Konto (``partner_id`` nur als Ersatz).
        """
        guard = settings_write_lock() if keepalive else contextlib.nullcontext()
        with self._held_lock(lock_timeout), guard:
            values = self._stored_credentials(account_name)
            client = TolinoClient(
                partner_id or values["partner_id"], values["hardware_id"],
                values["refresh_token"],
                token_callback=lambda token: save_refresh_token(
                    account_name, token),
                hardware_callback=lambda hardware: save_hardware_id(
                    account_name, hardware),
                # Ein noch gueltiger Access-Token erspart den Tausch -- und
                # damit eine Rotation des Einmal-Tokens.
                access=values["access_token"],
                access_expires_at=values["access_expires_at"],
                access_callback=lambda token, expires: save_access_token(
                    account_name, token, expires),
                # Der eine Weg zum gespeicherten Token: auch beim ersten
                # Tausch sichtbar, damit ein parallel rotierter Token nicht
                # als abgelehnt geloescht wird. Liest ohne Vault-Lock, weil
                # `relogin()` ihn bereits haelt.
                stored_token_callback=lambda: account_tokens(
                    account_name).get("refresh_token"))
            # Nur der tatsaechlich eingeloeste Token darf verschwinden: ein
            # paralleler Refresh (Keep-alive, zweite Instanz) oder ein
            # `relogin()` kann inzwischen einen anderen gespeichert haben.
            client.token_rejected_callback = (
                lambda rejected: clear_refresh_token_if_unchanged(
                    account_name, rejected))
            if not values["hardware_id"] and not save_hardware_id(
                    account_name, client.hardware):
                # Die erzeugte ID gehoert ins Konto, sonst wechselt sie je Lauf
                # und tolino zaehlt jedes Mal ein neues Geraet.
                raise TolinoError(
                    "Die Geräte-ID konnte nicht gespeichert werden. Ohne sie "
                    "gilt jeder Lauf als neues Gerät; bitte die "
                    "Einstellungsdatei prüfen und erneut anmelden.",
                    short="Die Geräte-ID konnte nicht gespeichert werden.",
                    detail="Einstellungen konnten nicht geschrieben werden.")
            # Der Tausch selbst muss speicherbar sein: ein rotierter
            # Einmal-Token waere sonst verbrannt, bevor er im Konto steht.
            if not settings_writable():
                raise TolinoError(
                    "Die Einstellungen sind nicht beschreibbar; der Zugang "
                    "wird nicht erneuert.",
                    short="Die Einstellungen konnten nicht gespeichert "
                          "werden. " + RELOGIN,
                    detail="Token-Tausch ohne speicherbare Rotation "
                           "abgelehnt.")
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
        account = account_tokens(account_name) or {}
        refresh = normalize_refresh_token(account.get("refresh_token"))
        if not refresh:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN,
                                  code="A01")
        return {"refresh_token": refresh,
                "hardware_id": str(account.get("hardware_id") or ""),
                "partner_id": _partner_number(account.get("partner_id")),
                "access_token": str(account.get("access_token") or ""),
                "access_expires_at": _seconds(account.get("access_expires_at"))}

    def relogin(self, client, timeout=30):
        """Refresh once under the shared lock using the freshest saved token.

        Der Keep-alive haelt den Lock ueber den Netzaufruf und dessen
        WAF-Retries. Ein Sync, der waehrenddessen neu anmelden muss, wartet
        besser kurz, statt mit S91 fatal abzubrechen.
        """
        with self._held_lock(timeout):
            values = self._stored_credentials(client.account_name)
            if values["partner_id"] != client.partner_id:
                # Der gespeicherte Token gilt fuer den Buchhaendler des
                # Kontos. Ein Client fuer einen anderen Shop darf ihn nicht
                # einloesen: der Server lehnte ihn ab und der Vault loeschte
                # einen gueltigen Token.
                raise TolinoAuthError(
                    "Das gespeicherte Konto gehoert zu einem anderen "
                    "Buchhändler (Partner %s statt %s). "
                    % (values["partner_id"], client.partner_id) + RELOGIN,
                    code="A01",
                    short="Die Anmeldung gehört zu einem anderen Buchhändler.",
                    detail="Bitte mit dem hinterlegten Buchhändler anmelden.")
            client.refresh = values["refresh_token"]
            client.hardware = values["hardware_id"] or client.hardware
            # `_login()` meldet den neuen Token ueber den Callback, der ihn
            # bereits persistiert -- kein zweites Speichern hier.
            return client._login()

    def due_in(self, account_name):
        """Sekunden bis zur naechsten Erneuerung; None heisst unbekannt.

        Zwei Grenzen zaehlen: ein Fuenftel der Refresh-Token-Laufzeit (aus
        ``exp-iat``, also unabhaengig von der lokalen Uhr) und der Ablauf des
        gespeicherten Access-Tokens.
        """
        values = account_tokens(account_name)
        if not normalize_refresh_token(values.get("refresh_token")):
            return None
        now, deadlines = time.time(), []
        lifetime = lifetime_seconds(values["refresh_token"])
        rotated = _seconds(values.get("refresh_rotated_at"))
        if lifetime and rotated:
            margin = max(REFRESH_MARGIN_MIN,
                         lifetime * REFRESH_MARGIN_FRACTION)
            deadlines.append(rotated + lifetime - margin)
        access_expires = _seconds(values.get("access_expires_at"))
        if access_expires:
            deadlines.append(access_expires)
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - now)

    def next_delay(self, default):
        """Wartezeit bis zur naechsten Runde (5 min bis ``default``)."""
        try:
            accounts = settings()["accounts"]
        except Exception:
            return default
        delays = []
        for account in accounts:
            if not normalize_refresh_token(account.get("refresh_token")):
                continue
            due = self.due_in(str(account.get("name") or ""))
            if due is not None:
                delays.append(due)
        if not delays:
            return default
        return max(KEEPALIVE_MIN_DELAY, min(default, min(delays)))

    def keepalive_refresh(self, only_due=False):
        """Refresh saved accounts; return only the number of rotations.

        Mit ``only_due`` bleiben Konten aus, deren Token noch lange gilt:
        jede Rotation ist ein Einmal-Tausch und soll nur passieren, wenn die
        Laufzeit es verlangt.
        """
        if not settings_writable():
            # Ein Token, dessen Rotation nicht gespeichert werden kann, waere
            # verbrannt: lieber gar nicht erst einloesen.
            log_warning("Tolino-Keep-alive: Einstellungen sind nicht "
                        "speicherbar; es wird nichts erneuert.")
            return 0
        try:
            accounts = settings()["accounts"]
        except Exception:
            log_warning("Tolino-Keep-alive: Einstellungen konnten nicht "
                        "gelesen werden.")
            return 0
        rotated = 0
        for account in accounts:
            name = str(account.get("name") or "")
            # Dieselbe Normalisierung wie im Login: was danach leer ist,
            # waere ohnehin ein «nicht angemeldet».
            if not name or not normalize_refresh_token(
                    account.get("refresh_token")):
                continue
            if only_due:
                due = self.due_in(name)
                if due is not None and due > 0:
                    continue
            try:
                self.login(name, lock_timeout=0, keepalive=True)
                rotated += 1
            except Exception as exc:
                # Ein belegter Lock ist kein Fehler -- nächste Runde.
                if _lock_busy(exc):
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
    """Rotate saved refresh tokens as their own lifetime requires.

    ``interval`` ist die Obergrenze zwischen zwei Runden; gearbeitet wird
    frueher, wenn die Laufzeit des Tokens es verlangt. Die erste Runde laeuft
    sofort: nach Tagen ohne Calibre ist der gespeicherte Zugang oft faellig.
    """
    global _KEEPALIVE_THREAD, _KEEPALIVE_STOP
    cap = max(60, interval)
    with _KEEPALIVE_LOCK:
        if _KEEPALIVE_THREAD is not None and _KEEPALIVE_THREAD.is_alive():
            return None
        # Jeder Lauf bekommt sein eigenes Stop-Event; ein alter Thread kann
        # einen Neustart damit nicht versehentlich wieder aktivieren.
        stop = threading.Event()

        def loop():
            TOKEN_VAULT.keepalive_refresh(only_due=True)
            while not stop.wait(TOKEN_VAULT.next_delay(cap)):
                TOKEN_VAULT.keepalive_refresh(only_due=True)

        thread = threading.Thread(
            target=loop, name="tolino-token-keepalive", daemon=True)
        _KEEPALIVE_THREAD, _KEEPALIVE_STOP = thread, stop
        thread.start()
    log_info("Tolino-Keep-alive gestartet: nächste Runde nach der Laufzeit "
             "des Tokens (spätestens alle %d Minuten)." % (cap // 60))
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


def check_stored_login(account_name, partner_id=None, lock_timeout=5):
    """Check one saved account through the shared refresh-token lock."""
    try:
        TOKEN_VAULT.login(account_name, partner_id, lock_timeout)
        return "valid", ""
    except Exception as exc:
        if _lock_busy(exc):
            return "busy", ""
        return "invalid", sanitize_error(exc, account_secrets(account_name))

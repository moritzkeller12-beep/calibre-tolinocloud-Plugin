"""Der gemeinsame Token-Vault: Rotation, Persistenz, Keep-alive, Relogin.

Der Vault spricht einen Loopback-Token-Endpunkt an (echte Sockets) und
schreibt in einen echten Einstellungsspeicher; geprüft wird, was nach einem
Tausch im Konto steht und was bei Ablehnung oder belegtem Lock passiert.
"""
import contextlib
import json
import unittest
from urllib.parse import parse_qs

from .. import config
from ..errors import TolinoAuthError, TolinoError
from ..token_vault import (TOKEN_VAULT, check_stored_login,
                           start_token_keepalive, stop_token_keepalive)
from ..tolino import BUSY_MESSAGE, TolinoClient
from ._support import (LocalHTTP, PreferencesCase, break_settings,
                       local_tolino_endpoints)

JSON_HEADERS = {"Content-Type": "application/json"}


class TokenServer:
    """Token-Endpunkt, der bei jedem Tausch einen neuen Token ausgibt."""

    def __init__(self, rejected=()):
        self.rejected = set(rejected)
        self.count = 0
        self.server = LocalHTTP(self.respond)

    def respond(self, method, path, headers, payload):
        form = parse_qs((payload or b"").decode("utf-8"))
        token = (form.get("refresh_token") or [""])[0]
        if token in self.rejected:
            body = json.dumps({"error": "invalid_grant"}).encode()
            return 400, JSON_HEADERS, body
        self.count += 1
        body = json.dumps({"access_token": "acc-%d" % self.count,
                           "refresh_token": "rot-%d" % self.count,
                           "expires_in": 3600}).encode()
        return 200, JSON_HEADERS, body

    @property
    def used_tokens(self):
        return [(parse_qs(request["body"].decode("utf-8"))
                 .get("refresh_token") or [""])[0]
                for request in self.server.requests]


class VaultCase(PreferencesCase):
    """Frischer Einstellungsspeicher je Test."""

    @contextlib.contextmanager
    def vault(self, server):
        with server.server as running, local_tolino_endpoints(running.origin):
            yield running


class LoginTests(VaultCase):
    """Ein Login tauscht genau einen Token und schreibt die Rotation zurück."""

    def test_the_rotated_token_replaces_the_stored_one(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start"})
        with self.vault(server):
            client = TOKEN_VAULT.login("A")
        self.assertEqual("rot-1", client.refresh)
        self.assertEqual("rot-1", config.account_summary("A")["refresh_token"])
        self.assertEqual(["start"], server.used_tokens)
        self.assertEqual("A", client.account_name)

    def test_a_generated_hardware_id_is_stored_in_the_account(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start"})
        with self.vault(server):
            client = TOKEN_VAULT.login("A")
        stored = config.account_summary("A")["hardware_id"]
        self.assertTrue(stored)
        self.assertEqual(client.hardware, stored)

    def test_a_generated_hardware_id_that_cannot_be_stored_stops_the_login(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start"})
        break_settings(self)
        with self.vault(server):
            with self.assertRaises(TolinoError) as caught:
                TOKEN_VAULT.login("A")
        self.assertIn("Geräte-ID", str(caught.exception))
        self.assertEqual([], server.server.requests)

    def test_an_account_without_a_token_never_calls_the_server(self):
        server = TokenServer()
        config.save_account("A", {"hardware_id": "hw"})
        with self.vault(server):
            with self.assertRaises(TolinoAuthError) as caught:
                TOKEN_VAULT.login("A")
        self.assertEqual("A01", caught.exception.code)
        self.assertIn("Noch nicht angemeldet", str(caught.exception))
        self.assertEqual([], server.server.requests)

    def test_a_rejected_token_is_cleared(self):
        server = TokenServer(rejected={"verloren"})
        config.save_account("A", {"refresh_token": "verloren"})
        with self.vault(server):
            with self.assertRaises(TolinoAuthError) as caught:
                TOKEN_VAULT.login("A")
        self.assertEqual("A01", caught.exception.code)
        self.assertEqual("", config.account_summary("A")["refresh_token"])

    def test_the_exchange_itself_refuses_unreadable_settings(self):
        """Auch ohne Keep-alive-Vorpruefung darf kein Token verbrannt werden."""
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start",
                                  "hardware_id": "hw-A"})
        break_settings(self)
        with self.vault(server):
            with self.assertRaises(TolinoError):
                TOKEN_VAULT.login("A")
        self.assertEqual([], server.server.requests)
        self.assertEqual("start", config.account_summary("A")["refresh_token"])

    def test_relogin_does_not_use_a_token_of_another_partner(self):
        """Der Token gilt fuer den Buchhaendler des Kontos, nicht den Client."""
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start", "partner_id": 2,
                                  "hardware_id": "hw-A"})
        client = TolinoClient(1, "hw-A", refresh="start")
        client.account_name = "A"
        with self.vault(server):
            with self.assertRaises(TolinoAuthError) as caught:
                TOKEN_VAULT.relogin(client)
        self.assertEqual("A01", caught.exception.code)
        self.assertEqual([], server.server.requests)
        self.assertEqual("start", config.account_summary("A")["refresh_token"])

    def test_relogin_uses_the_token_saved_in_the_meantime(self):
        # Der Client kennt nach dem Login nur rot-1; der Server lehnt genau
        # diesen ab. Nur der inzwischen NEUER gespeicherte Token kommt durch.
        server = TokenServer(rejected={"rot-1"})
        config.save_account("A", {"refresh_token": "start"})
        with self.vault(server):
            client = TOKEN_VAULT.login("A")
            self.assertEqual("rot-1", client.refresh)
            config.save_refresh_token("A", "rot-1-neu")
            self.assertEqual("rot-2", TOKEN_VAULT.relogin(client))
        self.assertEqual(["start", "rot-1-neu"], server.used_tokens)
        self.assertEqual("rot-2", config.account_summary("A")["refresh_token"])


class LockTests(VaultCase):
    """Der gemeinsame Lock schützt den Einmal-Token; belegt ist kein Fehler."""

    def test_a_busy_vault_reports_busy_instead_of_failing(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "start"})
        with self.vault(server):
            TOKEN_VAULT._lock.acquire()
            try:
                with self.assertRaises(TolinoAuthError) as caught:
                    TOKEN_VAULT.login("A", lock_timeout=0)
                self.assertEqual("S91", caught.exception.code)
                self.assertEqual(BUSY_MESSAGE, str(caught.exception))
                self.assertEqual(("busy", ""),
                                 check_stored_login("A", lock_timeout=0))
            finally:
                TOKEN_VAULT._lock.release()
        self.assertEqual([], server.server.requests)

    def test_a_stored_login_is_checked_through_the_vault(self):
        server = TokenServer(rejected={"abgelehnt"})
        config.save_account("gut", {"refresh_token": "start"})
        config.save_account("schlecht", {"refresh_token": "abgelehnt"})
        with self.vault(server):
            self.assertEqual(("valid", ""), check_stored_login("gut"))
            status, message = check_stored_login("schlecht")
        self.assertEqual("invalid", status)
        self.assertIn("abgelehnt", message)
        self.assertNotIn("abgelehnt", config.account_summary("schlecht")
                         ["refresh_token"])


class KeepaliveTests(VaultCase):
    """Der Keep-alive erneuert nur Konten mit Token und meldet deren Anzahl."""

    def test_every_saved_account_is_rotated_once(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "tok-A"})
        config.save_account("B", {"refresh_token": "tok-B"})
        config.save_account("ohne", {"hardware_id": "hw"})
        with self.vault(server):
            self.assertEqual(2, TOKEN_VAULT.keepalive_refresh())
        self.assertEqual("rot-1", config.account_summary("A")["refresh_token"])
        self.assertEqual("rot-2", config.account_summary("B")["refresh_token"])
        self.assertEqual("", config.account_summary("ohne")["refresh_token"])
        self.assertEqual(["tok-A", "tok-B"], server.used_tokens)

    def test_one_failing_account_does_not_stop_the_others(self):
        server = TokenServer(rejected={"tok-A"})
        config.save_account("A", {"refresh_token": "tok-A"})
        config.save_account("B", {"refresh_token": "tok-B"})
        with self.vault(server):
            self.assertEqual(1, TOKEN_VAULT.keepalive_refresh())
        self.assertEqual("", config.account_summary("A")["refresh_token"])
        self.assertEqual("rot-1", config.account_summary("B")["refresh_token"])

    def test_an_unreadable_settings_file_rotates_nothing(self):
        server = TokenServer()
        config.save_account("A", {"refresh_token": "tok-A",
                                  "hardware_id": "hw-A"})
        with open(config.PREFERENCES.file_path, "w", encoding="utf-8") as handle:
            handle.write('{"accounts": [')
        with self.vault(server):
            self.assertEqual(0, TOKEN_VAULT.keepalive_refresh())
        # Ein Token, dessen Rotation nicht gespeichert werden kann, darf gar
        # nicht erst eingeloest werden -- und das Konto bleibt unangetastet.
        self.assertEqual([], server.server.requests)
        account = config.account_summary("A")
        self.assertEqual("tok-A", account["refresh_token"])
        self.assertEqual("hw-A", account["hardware_id"])

    def test_settings_that_break_mid_round_stop_the_next_exchange(self):
        config.save_account("A", {"refresh_token": "tok-A", "hardware_id": "a"})
        config.save_account("B", {"refresh_token": "tok-B", "hardware_id": "b"})
        path = config.PREFERENCES.file_path

        class InterruptedStoreServer(TokenServer):
            def respond(self, *request):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write('{"accounts": [')
                return super().respond(*request)

        server = InterruptedStoreServer()
        with self.vault(server):
            self.assertEqual(0, TOKEN_VAULT.keepalive_refresh())
        self.assertEqual(["tok-A"], server.used_tokens)
        with open(path, encoding="utf-8") as handle:
            self.assertEqual('{"accounts": [', handle.read())

    def test_the_thread_starts_once_and_stops_cleanly(self):
        self.addCleanup(stop_token_keepalive)
        thread = start_token_keepalive(interval=3600)
        try:
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
            self.assertIsNone(start_token_keepalive(interval=3600))
            self.assertTrue(stop_token_keepalive())
            self.assertFalse(thread.is_alive())
            self.assertIsNotNone(start_token_keepalive(interval=3600))
        finally:
            stop_token_keepalive()
        self.assertTrue(stop_token_keepalive())


if __name__ == "__main__":
    unittest.main()

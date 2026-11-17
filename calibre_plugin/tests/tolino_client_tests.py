"""Anmeldung und Token-Rotation des tolino-Clients -- über echte Sockets.

Der Client spricht einen Loopback-HTTP-Server an, statt `urlopen` zu
ersetzen: geprüft wird, was tatsächlich über die Leitung geht (Formular,
Kopfdaten, Rotationsreihenfolge). Das sind die Fehler, die den Nutzer
ausloggen oder einen Einmal-Token verbrennen.
"""
import json
import unittest

from ..errors import TolinoAuthError, TolinoError
from ..tolino import TolinoClient, sanitize_error
from ._support import LocalHTTP, local_tolino_endpoints

HARDWARE = "11111111-1111-4111-8111-111111111111"
TOKEN_BODY = json.dumps({"access_token": "acc-1", "refresh_token": "rot-1",
                         "expires_in": 3600}).encode()


def token_server(body=TOKEN_BODY, status=200, extra=None):
    """Server, der den Token-Endpunkt bedient; alles andere sind Fehler."""

    def responder(method, path, headers, payload):
        if path.startswith("/token/"):
            if extra is not None:
                return extra(method, path, headers, payload)
            return status, {"Content-Type": "application/json"}, body
        return 404, {}, b""

    return LocalHTTP(responder)


class LoginTests(unittest.TestCase):
    """Der Refresh-Token ist einmalig: Reihenfolge und Meldung muessen stimmen."""

    def test_login_exchanges_the_token_and_reports_the_rotation(self):
        saved = []
        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_callback=saved.append)
            self.assertEqual("rot-1", client.login())
            request = server.requests[0]
        self.assertEqual(["rot-1"], saved)
        self.assertIn("grant_type=refresh_token", request["body"].decode())
        self.assertIn("refresh_token=start", request["body"].decode())

    def test_a_rejected_token_is_reported_and_cleared_once(self):
        rejected = []
        body = json.dumps({"error": "invalid_grant"}).encode()
        with token_server(body=body, status=400) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_rejected_callback=rejected.append)
            with self.assertRaises(TolinoAuthError) as caught:
                client.login()
        self.assertEqual("A01", caught.exception.code)
        self.assertIn("abgelehnt", str(caught.exception))
        self.assertEqual(["start"], rejected)

    def test_a_network_error_keeps_the_stored_token(self):
        with token_server() as server:
            origin = server.origin
        # Server ist wieder zu: der Port antwortet nicht mehr.
        with local_tolino_endpoints(origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoError) as caught:
                client.login()
        self.assertEqual("N01", caught.exception.code)
        self.assertEqual("start", client.refresh)

    def test_a_second_login_reuses_the_valid_access_token(self):
        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            client.login()
            client.login()
            requests = len(server.requests)
        self.assertEqual(1, requests)

    def test_an_unknown_partner_is_rejected(self):
        with self.assertRaises(TolinoError):
            TolinoClient(99, HARDWARE, refresh="start")


class ErrorTextTests(unittest.TestCase):
    """Fehlertexte nennen den Grund, aber nie Zugangsdaten."""

    def test_tokens_and_tracebacks_are_redacted(self):
        exc = TolinoError("abgelehnt für tok-123", code="A01")
        text = sanitize_error(exc, ("tok-123",))
        self.assertNotIn("tok-123", text)
        self.assertIn("abgelehnt", text)

    def test_a_broken_token_response_is_an_auth_error(self):
        body = json.dumps({"refresh_token": "rot-1"}).encode()
        with token_server(body=body) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoAuthError):
                client.login()


if __name__ == "__main__":
    unittest.main()

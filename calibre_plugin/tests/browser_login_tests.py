"""Fallbacks der Browser-Anmeldung: Token-Auswahl und Abbruchmeldungen.

Geprueft wird die Entscheidungslogik ohne Browserfenster: welche gelesenen
Tokens brauchbar sind, was ohne PyCryptodome passiert und welche Meldung der
Nutzer je Fensterzustand bekommt.
"""
import base64
import json
import time
import unittest
from unittest.mock import patch

from ..browser_login import _raise_grab_end
from ..errors import TolinoAuthError
from ..token_candidates import (_candidate_age_note, _filter_live_candidates,
                                _normalize_live_grab)

# Ein formal gueltiger CryptoJS-Blob ("Salted__" + Salt + ein Block),
# damit der Decoder wirklich nach PyCryptodome fragt.
BLOB = base64.b64encode(b"Salted__" + b"12345678" + b"\x00" * 16).decode()


def jwt(iat=None):
    """Refresh-Token im JWT-Format mit gestellter Ausstellungszeit."""
    payload = {"iat": int(time.time() if iat is None else iat),
               "typ": "Refresh"}
    head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode())
    return "%s.%s.sig" % (head.decode().rstrip("="),
                          body.decode().rstrip("="))


def no_crypto():
    """Steht fuer eine Installation ohne PyCryptodome."""
    return patch("calibre_plugin.browser_crypto._aes",
                 side_effect=TolinoAuthError("PyCryptodome fehlt",
                                             code="A01"))


class TokenCandidateTests(unittest.TestCase):
    """Nur frische Tokens werden eingeloest; unlesbare nie stillschweigend."""

    def test_stale_tokens_are_dropped_and_undatable_ones_kept(self):
        fresh, stale, unknown = jwt(), jwt(iat=time.time() - 7200), "kein-jwt"
        self.assertEqual([fresh, unknown],
                         _filter_live_candidates([fresh, stale, unknown]))

    def test_the_age_note_never_contains_the_token(self):
        token = jwt(iat=time.time() - 90)
        note = _candidate_age_note([token])
        self.assertIn("Sekunden alt", note)
        self.assertNotIn(token, note)
        self.assertEqual("", _candidate_age_note([]))
        self.assertIn("unbekannt", _candidate_age_note(["kurz"]))

    def test_a_plain_token_survives_a_missing_decoder(self):
        with no_crypto():
            grabbed = _normalize_live_grab(
                {"refresh": [BLOB, jwt()], "hardware": []})
        self.assertEqual(1, len(grabbed["refresh"]))
        self.assertTrue(grabbed["refresh"][0].startswith("eyJ"))

    def test_only_an_encrypted_token_without_decoder_is_an_error(self):
        with no_crypto():
            with self.assertRaises(TolinoAuthError) as caught:
                _normalize_live_grab({"refresh": [BLOB], "hardware": []})
        self.assertIn("PyCryptodome", str(caught.exception))


class GrabEndTests(unittest.TestCase):
    """Nach dem Warten bekommt der Nutzer den Grund, der wirklich zutrifft."""

    def _error(self, *args):
        with self.assertRaises(TolinoAuthError) as caught:
            _raise_grab_end(*args)
        self.assertEqual("B02", caught.exception.code)
        return caught.exception

    def test_an_open_login_page_asks_for_the_sign_in(self):
        error = self._error({"window": True, "reader_tab": False}, True,
                            {"refresh": []}, set(), "invalid_grant")
        self.assertIn("noch nicht angemeldet", str(error))
        self.assertIn("invalid_grant", error.detail)

    def test_a_closed_window_after_a_read_reports_the_rejection(self):
        error = self._error({"window": False}, True, {"refresh": [jwt()]},
                            set(), "")
        self.assertIn("kein gültiger Zugang gelesen", str(error))
        self.assertIn("Sekunden alt", error.detail)

    def test_a_timeout_without_reader_tab_names_the_missing_tab(self):
        error = self._error(None, False, {"refresh": []}, set(), "")
        self.assertIn("Zeitüberschreitung", str(error))
        self.assertIn("kein Web-Reader-Tab", error.detail)

    def test_the_message_contains_no_token(self):
        token = jwt(iat=time.time() - 30)
        error = self._error({"window": False}, True, {"refresh": [token]},
                            set(), "")
        self.assertNotIn(token, str(error) + error.detail)


if __name__ == "__main__":
    unittest.main()

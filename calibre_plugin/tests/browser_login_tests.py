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

from ..browser_login import (_exchange_grabbed_token, _exchange_live_grab,
                             _raise_grab_end, grab_live_refresh)
from ..errors import TolinoAuthError
from ..token_candidates import (_candidate_age_note, _filter_live_candidates,
                                _normalize_live_grab)
from ._support import no_crypto

# Ein formal gueltiger CryptoJS-Blob ("Salted__" + Salt + ein Block),
# damit der Decoder wirklich nach PyCryptodome fragt.
BLOB = base64.b64encode(b"Salted__" + b"12345678" + b"\x00" * 16).decode()


THALIA = "https://www.thalia.de"


def jwt(iat=None, typ="Refresh", iss=THALIA):
    """JWT-Token mit gestellter Ausstellungszeit, Typ und Shop."""
    payload = {"iat": int(time.time() if iat is None else iat), "typ": typ}
    if iss:
        payload["iss"] = iss
    head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
    body = base64.urlsafe_b64encode(json.dumps(payload).encode())
    return "%s.%s.sig" % (head.decode().rstrip("="),
                          body.decode().rstrip("="))


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


class FakeTolinoClient:
    """Client-Doppel: merkt sich Kandidat und Geraet, kein Netz."""

    instances = []
    failure = None

    def __init__(self, partner_id, hardware, **_kwargs):
        self.partner_id = partner_id
        self.hardware = hardware
        self.refresh = ""
        self.refresh_in = ""
        self.access = "access"
        FakeTolinoClient.instances.append(self)

    def _login(self):
        if FakeTolinoClient.failure is not None:
            raise FakeTolinoClient.failure
        self.refresh_in = self.refresh


class ExchangeTests(unittest.TestCase):
    """Welcher gelesene Token eingeloest wird und welcher nicht."""

    def setUp(self):
        FakeTolinoClient.instances = []
        FakeTolinoClient.failure = None
        patcher = patch("calibre_plugin.browser_login.TolinoClient",
                        FakeTolinoClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_freshest_refresh_token_of_a_known_shop_is_used(self):
        access = jwt(iat=2000, typ="Access")
        older = jwt(iat=1000)
        newer = jwt(iat=1500)
        result = _exchange_grabbed_token("hw", {"hardware": []},
                                        [access, older, newer])
        self.assertEqual((newer, "hw", 1), result)
        self.assertEqual(newer, FakeTolinoClient.instances[0].refresh_in)

    def test_a_reader_hardware_id_replaces_the_stored_one(self):
        token = jwt()
        result = _exchange_grabbed_token("alt", {"hardware": [" hw-neu "]},
                                        [token])
        self.assertEqual("hw-neu", FakeTolinoClient.instances[0].hardware)
        self.assertEqual("hw-neu", result[1])

    def test_without_a_known_shop_nothing_is_exchanged(self):
        unknown = jwt(iss="https://shop.example")
        with self.assertRaises(TolinoAuthError) as caught:
            _exchange_grabbed_token("hw", {"hardware": []}, [unknown])
        self.assertEqual("B03", caught.exception.code)
        self.assertEqual([], FakeTolinoClient.instances)

    def test_a_rejected_grant_is_not_redeemed_again(self):
        FakeTolinoClient.failure = TolinoAuthError("invalid_grant", code="A01")
        token = jwt()
        grabbed = {"refresh": [token], "hardware": []}
        rejected = set()
        with patch("calibre_plugin.cdp.close_grabber_window"):
            result, error = _exchange_live_grab("hw", grabbed, rejected)
            self.assertIsNone(result)
            self.assertTrue(error)
            self.assertNotIn(token, error)
            self.assertIn(token, rejected)
            self.assertEqual(1, len(FakeTolinoClient.instances))
            # Derselbe Kandidat wird nicht noch einmal eingeloest.
            self.assertEqual((None, ""),
                             _exchange_live_grab("hw", grabbed, rejected))
        self.assertEqual(1, len(FakeTolinoClient.instances))


class GrabFlowTests(unittest.TestCase):
    """Der Wartepfad endet mit Token, Zeitueberschreitung oder Fensterschluss."""

    def setUp(self):
        FakeTolinoClient.instances = []
        FakeTolinoClient.failure = None
        patcher = patch("calibre_plugin.browser_login.TolinoClient",
                        FakeTolinoClient)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_successful_grab_returns_token_partner_and_closes(self):
        with patch("calibre_plugin.cdp.launch_reader_window"), \
                patch("calibre_plugin.cdp.grab_once_from_grabber",
                      return_value={"refresh": [jwt()], "hardware": []}), \
                patch("calibre_plugin.cdp.grabber_window_state",
                      return_value={"window": True, "reader_tab": True}), \
                patch("calibre_plugin.cdp.close_grabber_window",
                      return_value=True) as close:
            refresh, hardware, partner = grab_live_refresh("hw", timeout=5)
        self.assertTrue(refresh.startswith("eyJ"))
        self.assertEqual(("hw", 1), (hardware, partner))
        self.assertTrue(close.called)

    def test_a_timeout_without_a_reader_tab_keeps_the_window_open(self):
        with patch("calibre_plugin.cdp.launch_reader_window"), \
                patch("calibre_plugin.cdp.grab_once_from_grabber",
                      return_value=None), \
                patch("calibre_plugin.cdp.grabber_window_state",
                      return_value={"window": True, "reader_tab": False}), \
                patch("calibre_plugin.cdp.close_grabber_window") as close:
            with self.assertRaises(TolinoAuthError) as caught:
                grab_live_refresh("hw", timeout=0.05)
        self.assertEqual("B02", caught.exception.code)
        self.assertIn("kein Web-Reader-Tab", caught.exception.detail)
        # Die Anmeldemaske bleibt offen, damit man sich anmelden kann.
        close.assert_not_called()

    def test_a_closed_window_after_a_rejected_token_is_closed_and_reported(self):
        FakeTolinoClient.failure = TolinoAuthError("invalid_grant", code="A01")
        # Einmal gelesen, dann ist das Fenster weg: der Grab liefert nichts
        # mehr, und die drei verpassten Endpunkt-Checks brechen ab.
        grabs = [{"refresh": [jwt()], "hardware": []}] + [None] * 3
        with patch("calibre_plugin.cdp.launch_reader_window"), \
                patch("calibre_plugin.cdp.grab_once_from_grabber",
                      side_effect=grabs), \
                patch("calibre_plugin.cdp.grabber_window_state",
                      return_value={"window": False}), \
                patch("calibre_plugin.cdp.close_grabber_window",
                      return_value=True) as close, \
                patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0), \
                patch("calibre_plugin.browser_login._WINDOW_CHECK_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as caught:
                grab_live_refresh("hw", timeout=5)
        self.assertEqual("B02", caught.exception.code)
        self.assertIn("kein gültiger Zugang", str(caught.exception))
        self.assertTrue(close.called)


if __name__ == "__main__":
    unittest.main()

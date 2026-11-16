"""Tests fuer die Auflösung der Zugangsdaten aus den Project-Secrets."""
import unittest

from ..errors import TolinoAuthError
from .secrets_support import (HARDWARE_ENV, TOKEN_ENV,
                              credentials_from_env)

# Reine Platzhalter: kein echter Zugang, keine echte Geräte-ID.
DUMMY_TOKEN = "test-token-" + "x" * 40
DUMMY_HARDWARE = "hw-test-0001"


class SecretResolutionTests(unittest.TestCase):
    def test_both_secrets_are_read_from_the_environment(self):
        token, hardware = credentials_from_env({
            TOKEN_ENV: "  " + DUMMY_TOKEN + "  ",
            HARDWARE_ENV: " " + DUMMY_HARDWARE + " ",
        })
        self.assertEqual(DUMMY_TOKEN, token)
        self.assertEqual(DUMMY_HARDWARE, hardware)

    def test_invalid_credentials_are_rejected(self):
        cases = (
            ("missing token", {HARDWARE_ENV: DUMMY_HARDWARE}, TOKEN_ENV,
             "Project-Secrets"),
            ("blank token", {TOKEN_ENV: "   \n\t "}, TOKEN_ENV, None),
            ("placeholder token", {TOKEN_ENV: "kurz"}, TOKEN_ENV, None),
            ("token with spaces", {TOKEN_ENV: "zwei teile " + "x" * 40},
             TOKEN_ENV, None),
            ("invalid hardware", {TOKEN_ENV: DUMMY_TOKEN,
                                  HARDWARE_ENV: "hw 0001!?#"}, HARDWARE_ENV,
             None),
        )
        for label, env, mentioned, note in cases:
            with self.subTest(label):
                with self.assertRaises(TolinoAuthError) as caught:
                    credentials_from_env(env)
                self.assertIn(mentioned, str(caught.exception))
                self.assertEqual("A01", caught.exception.code)
                if note:
                    self.assertIn(note, str(caught.exception))

    def test_a_missing_hardware_id_is_allowed(self):
        token, hardware = credentials_from_env({TOKEN_ENV: DUMMY_TOKEN})
        self.assertEqual(DUMMY_TOKEN, token)
        self.assertEqual("", hardware)

    def test_an_unexpected_hardware_id_is_rejected(self):
        with self.assertRaises(TolinoAuthError) as caught:
            credentials_from_env({TOKEN_ENV: DUMMY_TOKEN,
                                  HARDWARE_ENV: "hw 0001!?#"})
        self.assertIn(HARDWARE_ENV, str(caught.exception))

    def test_an_error_never_contains_the_secret(self):
        secret = "geheim-" + "y" * 40
        with self.assertRaises(TolinoAuthError) as caught:
            credentials_from_env({TOKEN_ENV: secret,
                                  HARDWARE_ENV: "hw ungültig!"})
        text = str(caught.exception) + str(caught.exception.detail)
        self.assertNotIn(secret, text)

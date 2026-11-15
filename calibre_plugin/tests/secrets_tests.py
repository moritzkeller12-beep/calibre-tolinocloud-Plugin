"""Tests fuer die Auflösung der Zugangsdaten aus den Project-Secrets."""
import unittest

from ..errors import TolinoAuthError
from .secrets_support import (HARDWARE_ENV, MIN_TOKEN_LENGTH, TOKEN_ENV,
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

    def test_a_missing_token_names_the_missing_secret(self):
        with self.assertRaises(TolinoAuthError) as caught:
            credentials_from_env({HARDWARE_ENV: DUMMY_HARDWARE})
        self.assertIn(TOKEN_ENV, str(caught.exception))
        self.assertIn("Project-Secrets", str(caught.exception))
        self.assertEqual("A01", caught.exception.code)

    def test_a_blank_token_is_treated_as_missing(self):
        with self.assertRaises(TolinoAuthError):
            credentials_from_env({TOKEN_ENV: "   \n\t "})

    def test_a_placeholder_token_is_rejected(self):
        with self.assertRaises(TolinoAuthError) as caught:
            credentials_from_env({TOKEN_ENV: "kurz"})
        self.assertIn(TOKEN_ENV, str(caught.exception))
        self.assertLess(len("kurz"), MIN_TOKEN_LENGTH)

    def test_a_token_with_spaces_is_rejected(self):
        with self.assertRaises(TolinoAuthError):
            credentials_from_env({TOKEN_ENV: "zwei teile " + "x" * 40})

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

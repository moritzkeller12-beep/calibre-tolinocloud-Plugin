"""Konten und Tokens: Trennung, Rotation und verspaetete Rueckmeldungen."""
import unittest
import uuid

from .. import config


class ConfigCase(unittest.TestCase):
    """Jeder Test bekommt einen eigenen Einstellungsspeicher."""

    def setUp(self):
        self._original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)

    def tearDown(self):
        config.PREFERENCES = self._original


class AccountTests(ConfigCase):
    """Mehrere Konten bleiben getrennt; die Auswahl aendert keine Werte."""

    def test_accounts_keep_their_tokens_and_state_apart(self):
        config.save_account("A", {"refresh_token": "tok-A",
                                  "state": {"b1": {"tolino_id": "d1"}}},
                            active="A")
        config.save_account("B", {"refresh_token": "tok-B",
                                  "state": {"b2": {"tolino_id": "d2"}}})
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])
        self.assertEqual({"b1": {"tolino_id": "d1"}},
                         config.account_by_name("A")["state"])
        self.assertEqual({"b2": {"tolino_id": "d2"}},
                         config.account_by_name("B")["state"])
        self.assertEqual("A", config.settings()["active_account"])

    def test_switching_the_account_keeps_every_stored_value(self):
        config.save_account("A", {"refresh_token": "tok-A"}, active="A")
        config.save_account("B", {"refresh_token": "tok-B"})
        self.assertTrue(config.set_active_account("B"))
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])
        self.assertEqual("B", config.settings()["active_account"])
        self.assertFalse(config.set_active_account("gibt-es-nicht"))

    def test_preferred_formats_are_one_global_setting(self):
        config.save_account("A", {"refresh_token": "tok-A"},
                            preferred_formats=["PDF"])
        self.assertEqual(["PDF"], config.settings()["preferred_formats"])
        config.save_account("B", {"refresh_token": "tok-B"})
        self.assertEqual(["PDF"], config.settings()["preferred_formats"])

    def test_a_removed_account_is_not_recreated_by_a_late_callback(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        config.save_account("B", {"refresh_token": "tok-B"})
        self.assertTrue(config.remove_account("A"))
        self.assertFalse(config.save_refresh_token("A", "tok-neu"))
        self.assertFalse(config.save_credentials("A", "tok-neu", "hw"))
        self.assertEqual({}, config.account_by_name("A"))


class TokenTests(ConfigCase):
    """Token-Rotation: nur der abgelehnte Token darf verschwinden."""

    def test_a_rotated_token_replaces_the_stored_one(self):
        config.save_account("A", {"refresh_token": "alt"})
        self.assertTrue(config.save_refresh_token("A", "neu"))
        self.assertEqual("neu", config.account_summary("A")["refresh_token"])

    def test_an_empty_token_does_not_clear_the_login(self):
        config.save_account("A", {"refresh_token": "alt"})
        self.assertFalse(config.save_refresh_token("A", ""))
        self.assertEqual("alt", config.account_summary("A")["refresh_token"])

    def test_only_the_rejected_token_is_cleared(self):
        config.save_account("A", {"refresh_token": "verloren"})
        self.assertTrue(config.clear_refresh_token_if_unchanged(
            "A", "verloren"))
        self.assertEqual("", config.account_summary("A")["refresh_token"])
        config.save_account("A", {"refresh_token": "gewinner"})
        self.assertFalse(config.clear_refresh_token_if_unchanged(
            "A", "verloren"))
        self.assertEqual("gewinner", config.account_summary("A")["refresh_token"])

    def test_an_unreadable_settings_file_is_not_adopted(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        path = config.PREFERENCES.file_path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"accounts": [')
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])
        self.assertFalse(config.save_refresh_token("A", "tok-B"))


if __name__ == "__main__":
    unittest.main()

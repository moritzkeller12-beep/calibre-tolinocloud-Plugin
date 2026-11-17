"""Konten und Tokens: Trennung, Rotation und verspaetete Rueckmeldungen."""
import json
import os
import shutil
import tempfile
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


class ExternalChangeTests(ConfigCase):
    """Die Datei gehoert mehreren Prozessen: fremde Staende bleiben erhalten.

    Geprueft wird die beobachtbare Datei, nicht die Zahl der Schreibaufrufe:
    ein zweites Calibre-Fenster oder der Keep-alive schreibt denselben Store.
    """

    def file_path(self):
        return config.PREFERENCES.file_path

    def write_externally(self, accounts, active=None):
        """Eine andere Instanz schreibt den vollstaendigen Stand.

        Bewusst direkt am Store vorbei: der eigene Cache muss die Datei
        erneut lesen, statt seinen veralteten Stand zurueckzuschreiben.
        """
        data = {"accounts": accounts}
        if active is not None:
            data["active_account"] = active
        with open(self.file_path(), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    @staticmethod
    def account(name, token, state=None):
        return {"name": name, "partner_id": 1, "hardware_id": "",
                "refresh_token": token, "state": state or {}}

    def test_a_missing_settings_file_is_a_fresh_installation(self):
        """Ohne Datei gibt es ein virtuelles Default-Konto, keinen Fehler."""
        self.assertFalse(os.path.exists(self.file_path()))
        self.assertEqual("default", config.settings()["active_account"])
        self.assertEqual("", config.account_by_name("default")["refresh_token"])
        self.assertTrue(config.save_account("A", {"refresh_token": "tok-A"}))
        self.assertTrue(os.path.exists(self.file_path()))
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])

    def test_a_file_changed_by_another_process_is_adopted(self):
        config.save_account("A", {"refresh_token": "alt"}, active="A")
        self.write_externally(
            [self.account("A", "extern"), self.account("B", "tok-B")],
            active="A")
        self.assertEqual("extern",
                         config.account_summary("A")["refresh_token"])
        self.assertEqual("tok-B", config.account_summary("B")["refresh_token"])

    def test_an_externally_added_account_survives_a_local_write(self):
        config.save_account("A", {"refresh_token": "alt"}, active="A")
        self.write_externally(
            [self.account("A", "alt"), self.account("B", "tok-B")],
            active="A")
        self.assertTrue(config.save_refresh_token("A", "neu"))
        self.assertEqual("neu", config.account_summary("A")["refresh_token"])
        self.assertEqual("tok-B", config.account_summary("B")["refresh_token"])

    def test_an_externally_selected_account_stays_active(self):
        config.save_account("A", {"refresh_token": "alt"}, active="A")
        self.write_externally(
            [self.account("A", "alt"), self.account("B", "tok-B")],
            active="B")
        # Eine Token-Rotation darf die Auswahl nicht umschalten.
        self.assertTrue(config.save_refresh_token("A", "neu"))
        self.assertEqual("B", config.settings()["active_account"])

    def test_an_unreadable_file_is_not_treated_as_deleted(self):
        """Ein Lesefehler ist kein geloeschter Stand.

        Als unlesbarer Speicherort dient ein Verzeichnis: das ist ein echter
        E/A-Fehler (POSIX: ``IsADirectoryError``, Windows: ``PermissionError``)
        und unterscheidet sich von «Datei fehlt».
        """
        config.save_account("A", {"refresh_token": "tok-A"})
        path = self.file_path()
        with open(path, "rb") as handle:
            before = handle.read()
        unreadable = tempfile.mkdtemp(prefix="tolino-unreadable-")
        self.addCleanup(shutil.rmtree, unreadable, True)
        config.PREFERENCES.file_path = unreadable
        # Lesen bleibt beim letzten guten Stand ...
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])
        # ... und ein Schreibvorgang bricht ab, statt zu ueberschreiben.
        self.assertFalse(config.save_refresh_token("A", "tok-B"))
        self.assertFalse(config.set_active_account("A"))
        config.PREFERENCES.file_path = path
        with open(path, "rb") as handle:
            self.assertEqual(before, handle.read())
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])


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

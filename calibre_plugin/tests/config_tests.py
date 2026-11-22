"""Konten und Tokens: Trennung, Rotation und verspaetete Rueckmeldungen."""
import json
import os
import unittest
from unittest.mock import Mock, patch

from .. import config
from ._support import PreferencesCase, break_settings


class ConfigCase(PreferencesCase):
    """Jeder Test bekommt einen eigenen Einstellungsspeicher."""


class StoreDoubleTests(unittest.TestCase):
    """Das Test-Doppel schreibt erst bei ``commit()``, wie Calibres Vorbild."""

    def test_update_touches_memory_only_until_commit(self):
        store = config.JSONConfig("store-double")
        store.update({"a": 1})
        self.assertEqual(1, store["a"])
        self.assertFalse(os.path.exists(store.file_path))
        store.commit()
        with open(store.file_path, encoding="utf-8") as handle:
            self.assertEqual({"a": 1}, json.load(handle))
        dict.clear(store)
        store.refresh()
        self.assertEqual(1, store["a"])


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
        break_settings(self)
        # Lesen bleibt beim letzten guten Stand ...
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])
        # ... und ein Schreibvorgang bricht ab, statt zu ueberschreiben.
        self.assertFalse(config.save_refresh_token("A", "tok-B"))
        self.assertFalse(config.set_active_account("A"))
        config.PREFERENCES.file_path = path
        with open(path, "rb") as handle:
            self.assertEqual(before, handle.read())
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])


    @unittest.skipIf(os.name == "nt", "POSIX file lock")
    def test_a_failed_file_lock_does_not_write_settings(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        with patch("fcntl.flock", side_effect=OSError("Sperre belegt")):
            with self.assertRaisesRegex(OSError, "Sperre belegt"):
                config.save_refresh_token("A", "tok-B")
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])

    def test_a_missing_lock_file_does_not_prevent_a_write(self):
        config.save_account("A", {"refresh_token": "tok-A"})
        lock_path = str(self.file_path()) + ".lock"
        os.remove(lock_path)
        os.mkdir(lock_path)
        self.assertTrue(config.save_refresh_token("A", "tok-B"))
        self.assertEqual("tok-B", config.account_summary("A")["refresh_token"])

    def test_the_windows_lock_retries_contention(self):
        """msvcrt.LK_LOCK bricht nach zehn Sekunden ab; der Lock muss warten."""
        fake = type("FakeMsvcrt", (), {
            "LK_NBLCK": 2,
            "locking": Mock(side_effect=[OSError(13, "belegt")] * 2 + [None])})
        with patch("os.name", "nt"), \
                patch.dict("sys.modules", {"msvcrt": fake}):
            config._lock_handle(Mock())
        self.assertEqual(3, fake.locking.call_count)
        broken = type("BrokenMsvcrt", (), {
            "LK_NBLCK": 2,
            "locking": Mock(side_effect=OSError(5, "kaputt"))})
        with patch("os.name", "nt"), \
                patch.dict("sys.modules", {"msvcrt": broken}):
            with self.assertRaises(OSError):
                config._lock_handle(Mock())


class TokenTests(ConfigCase):
    """Token-Rotation: nur der abgelehnte Token darf verschwinden."""

    def test_only_a_non_empty_rotation_replaces_the_login(self):
        config.save_account("A", {"refresh_token": "alt"})
        self.assertFalse(config.save_refresh_token("A", ""))
        self.assertEqual("alt", config.account_summary("A")["refresh_token"])
        self.assertTrue(config.save_refresh_token("A", "neu"))
        self.assertEqual("neu", config.account_summary("A")["refresh_token"])

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

    def test_a_state_save_keeps_a_foreign_change_to_the_same_copy(self):
        config.save_account("A", {"refresh_token": "tok-A", "state": {
            "b1": {"tolino_id": "alt", "calibre_id": 1,
                   "reading": {"read": True}}}})
        # Ausgangsstand des Laufs: derselbe Eintrag, aber anderer Lesestand.
        base = {"b1": {"tolino_id": "alt", "calibre_id": 1,
                       "reading": {"read": False}}}
        self.assertTrue(config.save_state(
            "A", {"b1": {"tolino_id": "alt", "calibre_id": 2,
                         "reading": {"read": False}}}, base))
        self.assertEqual({"tolino_id": "alt", "calibre_id": 2,
                          "reading": {"read": True}},
                         config.account_by_name("A")["state"]["b1"])

    def test_a_copy_change_discards_the_old_copy_values(self):
        config.save_account("A", {"refresh_token": "tok-A", "state": {
            "b1": {"tolino_id": "alt", "reading": {"read": True},
                   "collections": ["Serie"],
                   "meta_verified_fp": "fp"}}})
        # Nur die alte Kopie ist gespeichert; lokal ersetzt ein Upload sie.
        base = {"b1": {"tolino_id": "alt"}}
        self.assertTrue(config.save_state(
            "A", {"b1": {"tolino_id": "neu"}}, base))
        self.assertEqual({"tolino_id": "neu"},
                         config.account_by_name("A")["state"]["b1"])

    def test_stored_values_of_the_new_copy_survive_a_replacement(self):
        """Der andere Lauf hat die Ersatzkopie schon gelesen: nicht verwerfen."""
        stored = {"tolino_id": "neu", "reading": {"read": True}}
        for base in ({"b1": {"tolino_id": "alt"}},
                     {"b1": {"tolino_id": "alt", "reading": {"read": False}}}):
            with self.subTest(base=base):
                config.save_account("A", {"state": {"b1": stored}})
                self.assertTrue(config.save_state(
                    "A", {"b1": {"tolino_id": "neu"}}, base))
                self.assertEqual(stored,
                                 config.account_by_name("A")["state"]["b1"])

    def test_a_field_removed_locally_disappears_from_the_stored_state(self):
        config.save_account("A", {"refresh_token": "tok-A", "state": {
            "b1": {"tolino_id": "alt", "collections": ["Serie"]}}})
        base = {"b1": {"tolino_id": "alt", "collections": ["Serie"]}}
        self.assertTrue(config.save_state("A", {"b1": {"tolino_id": "alt"}},
                                          base))
        self.assertEqual({"tolino_id": "alt"},
                         config.account_by_name("A")["state"]["b1"])

    def test_a_new_book_keeps_foreign_reading_only_for_the_same_copy(self):
        for stored_id in ("neu", "extern"):
            with self.subTest(stored_id=stored_id):
                reading = {"read": True}
                config.save_account("A", {"state": {
                    "b1": {"tolino_id": stored_id, "reading": reading}}})
                self.assertTrue(config.save_state(
                    "A", {"b1": {"tolino_id": "neu", "calibre_id": 7}}, {}))
                expected = {"tolino_id": "neu", "calibre_id": 7}
                if stored_id == "neu":
                    expected["reading"] = reading
                self.assertEqual(expected,
                                 config.account_by_name("A")["state"]["b1"])

    def test_a_local_reading_change_applies_only_to_its_own_copy(self):
        base = {"b1": {"tolino_id": "alt", "reading": {"read": False}}}
        local = {"b1": {"tolino_id": "alt", "reading": {"read": True}}}
        for stored_id, stored_reading in (("alt", {"read": False}),
                                           ("neu", {"read": False}),
                                           ("neu", {"read": True})):
            with self.subTest(stored_id=stored_id, reading=stored_reading):
                stored = {"tolino_id": stored_id, "reading": stored_reading}
                config.save_account("A", {"state": {"b1": stored}})
                self.assertTrue(config.save_state("A", local, base))
                self.assertEqual(local["b1"] if stored_id == "alt" else stored,
                                 config.account_by_name("A")["state"]["b1"])


if __name__ == "__main__":
    unittest.main()

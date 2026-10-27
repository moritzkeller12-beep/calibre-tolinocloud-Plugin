"""Dashboard, Konten und Anmelde-Vorpruefung."""

import calibre_plugin.tolino as tolino_module
import calibre_plugin.token_vault as vault_module
import re
import unittest
from . import ui_base_tests
from .. import config
from ..partners import PARTNERS
from ..library import TOLINO_COLUMN
from ..tolino import TolinoAuthError
from ._support import (
    FakeText, _ui_source_text, start_cdp_stubs, stop_cdp_stubs, ui_module_for)
from pathlib import Path
from unittest.mock import patch


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class DashboardAccountSwitchTests(unittest.TestCase):
    """Mehrere Konten: Umschalten direkt auf der ersten Seite.

    Feldbefund: «Kann man die Kontoauswahl, sofern mehrere Konten
    vorhanden, ebenfalls auf die erste Seite bringen?» Die Zeile erscheint
    nur bei mehr als einem Konto – sonst wäre sie nur Ballast.
    """

    class Combo:
        """Minimaler Qt-Nachbau für Kombobox, Eingabe und Label."""

        def __init__(self, *_args, **_kwargs):
            self.items = []
            self.current_index = 0
            self.visible = True
            self.text_value = ""

        def blockSignals(self, value):
            self.blocked = value

        def clear(self):
            self.items = []

        def addItem(self, text, data=None):
            self.items.append((text, data))

        def setCurrentIndex(self, index):
            self.current_index = index

        def currentData(self):
            return self.itemData(self.current_index)

        def findData(self, data):
            return next((index for index, (_text, item) in enumerate(self.items)
                         if item == data), -1)

        def itemData(self, index):
            if 0 <= index < len(self.items):
                return self.items[index][1]
            return None

        def setVisible(self, value):
            self.visible = value

        def setText(self, text):
            self.text_value = text

        def text(self):
            return self.text_value

        def setToolTip(self, text):
            self.tooltip = text

        def setEnabled(self, value):
            self.enabled = value

        def setChecked(self, value):
            self.checked = bool(value)

        def isChecked(self):
            return getattr(self, "checked", False)

        def setRange(self, *_args):
            pass

        def setValue(self, _value):
            pass

    def _dashboard(self):
        dashboard = type("Dashboard", (), {})()
        for name in ("account_select", "account_quick", "partner", "hardware",
                     "refresh", "formats", "account_quick_label", "status",
                     "progress", "progress_label", "primary", "covers",
                     "deletions", "tolino_column"):
            setattr(dashboard, name, self.Combo())
        dashboard.thread = None
        return dashboard

    @staticmethod
    def _accounts(count):
        return [{"name": "erstes", "partner_id": 1, "hardware_id": "hw-1",
                 "refresh_token": "token-1",
                 "state": {}},
                {"name": "zweites", "partner_id": 2, "hardware_id": "hw-2",
                 "refresh_token": "",
                 "state": {}}][:count]

    def _run(self, count, active="erstes"):
        """load_values/switch_account mit gefälschten Einstellungen."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        original = (config.PREFERENCES, ui_module.dashboard.hardware_id,
                    config.set_active_account)

        def restore():
            (config.PREFERENCES, ui_module.dashboard.hardware_id) = original[:2]
            ui_module._restore_stubs_for_tests()

        # Erst NACH dem Test zurücknehmen: sonst ist der Umbau weg, bevor
        # der Test umschaltet.
        self.addCleanup(restore)
        calls = []
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({"accounts": self._accounts(count),
                                   "active_account": active})
        ui_module.dashboard.hardware_id = lambda: "auto-hw"
        real_set_active = original[2]

        def record(name):
            calls.append(("active", name))
            return real_set_active(name)

        ui_module.dashboard.set_active_account = record
        dashboard = self._dashboard()
        dashboard.partner.items = [("%s" % pid, pid)
                                   for pid in sorted(PARTNERS)]
        # Die echten Methoden an das gefälschte Fenster binden: geprüft
        # wird ihr Verhalten, nicht ein Nachbau davon.
        for name in ("load_values", "switch_account", "update_status"):
            setattr(dashboard, name,
                    getattr(ui_module.dashboard.SyncDashboard, name).__get__(dashboard))
        dashboard._save_visible_account = lambda: calls.append(("save", None))
        dashboard.load_values()
        return ui_module, dashboard, calls

    def test_one_account_hides_the_row(self):
        ui_module, dashboard, _calls = self._run(1)
        self.assertFalse(dashboard.account_quick.visible)
        self.assertFalse(dashboard.account_quick_label.visible)
        self.assertEqual([("erstes", "erstes")], dashboard.account_quick.items)

    def test_two_accounts_show_the_row_on_the_first_page(self):
        ui_module, dashboard, _calls = self._run(2)
        self.assertTrue(dashboard.account_quick.visible)
        self.assertTrue(dashboard.account_quick_label.visible)
        self.assertEqual([("erstes", "erstes"), ("zweites", "zweites")],
                         dashboard.account_quick.items)
        self.assertEqual(0, dashboard.account_quick.current_index)
        self.assertEqual(0, dashboard.account_select.current_index)

    def test_switching_on_the_first_page_moves_the_active_account(self):
        ui_module, dashboard, calls = self._run(2)
        self.assertTrue(dashboard.switch_account("zweites"))
        # Erst sichern, dann umschalten.
        self.assertEqual([("save", None), ("active", "zweites")], calls)
        self.assertEqual("zweites", dashboard.account_name)
        # Beide Auswahlen zeigen dasselbe Konto -- sonst arbeitet der
        # Plugin mit dem vorherigen weiter.
        self.assertEqual(1, dashboard.account_quick.current_index)
        self.assertEqual(1, dashboard.account_select.current_index)
        # Buchhändler, Hardware-ID und Zugang kommen vom neuen Konto.
        self.assertEqual(2, dashboard.partner.currentData())
        self.assertEqual("hw-2", dashboard.hardware.text())
        self.assertEqual("", dashboard.refresh.text())
        self.assertEqual("Nicht angemeldet.", dashboard.status.text_value)
        self.assertFalse(dashboard.primary.enabled)

    def test_switching_to_the_same_or_unknown_account_does_nothing(self):
        ui_module, dashboard, calls = self._run(2)
        self.assertFalse(dashboard.switch_account("erstes"))
        self.assertFalse(dashboard.switch_account("gibt-es-nicht"))
        self.assertFalse(dashboard.switch_account(""))
        self.assertEqual([], calls)

class DashboardLayoutTests(unittest.TestCase):
    """Ensure every dashboard widget is added to a visible layout."""

    def _source(self):
        return _ui_source_text()

    def _dashboard_body(self):
        return self._source().split("class SyncDashboard(QDialog):", 1)[1].split(
            "\nclass ", 1)[0]

    def test_every_created_button_is_added_to_a_layout(self):
        """Auch der Knopf im Einstellungsfenster und die Kontoauswahl auf
        der ersten Seite."""
        source = self._source()
        names = re.findall(
            r"self\.(\w+) = (?:QPushButton|QCheckBox|QComboBox|QLineEdit)\(",
            source)
        self.assertNotIn("debug", names)
        # Beschriftungen wie "Hardware-ID (automatisch erkannt)" enthalten
        # selbst Klammern -- deshalb zeilenweise prüfen: dieselbe Zeile muss
        # an ein addRow/addWidget/addLayout gehen.
        for name in names:
            self.assertTrue(
                re.search(r"add(?:Row|Widget|Layout)\([^\n]*self\.%s\b" % name,
                          source),
                "Widget %r hängt in keinem Layout" % name)

    def test_login_and_compare_have_their_own_buttons(self):
        """Der alte Doppelweg (zwei Anmeldeknöpfe) bleibt weg, aber der
        Anmeldeknopf steht wieder sichtbar auf der ersten Seite."""
        body = self._dashboard_body()
        self.assertIn("self.primary.clicked.connect(self.primary_action)", body)
        self.assertIn(
            "self.login_button.clicked.connect(self.browser_login)", body)
        self.assertNotIn("self.browser = QPushButton(", body)
        self.assertNotIn("self.start = QPushButton(", body)
        self.assertNotIn("self.cancel = QPushButton(", body)

    def test_the_settings_dialog_holds_the_rare_options(self):
        """Alles Seltene liegt in EINEM Fenster, nicht hinter zwei
        «… anzeigen»-Häkchen."""
        settings = ((Path(__file__).resolve().parents[1] / "ui_settings.py")
                   .read_text(encoding="utf-8"))
        self.assertIn("class SettingsDialog(QDialog):", settings)
        for group in ("Einstellungen", "Konten und Verbindung"):
            self.assertIn('QGroupBox("%s")' % group, settings)
        body = self._dashboard_body()
        self.assertNotIn("advanced_toggle", body)
        self.assertNotIn("_toggle_advanced", body)
        self.assertIn("self.settings_button.clicked.connect(self.open_settings)",
                      body)

    def test_connection_controls_remain_available_without_diagnostics(self):
        """Removing the report does not remove account or token controls."""
        source = self._source()
        settings = ((Path(__file__).resolve().parents[1] / "ui_settings.py")
                   .read_text(encoding="utf-8"))
        self.assertIn("Konten und Verbindung", settings)
        self.assertIn('QGroupBox("Konten und Verbindung")', settings)
        for label in ("Aktives Konto", "Geräte-ID (automatisch)",
                      "Zugangstoken (nur bei Problemen ändern)"):
            self.assertIn(label, source)
        self.assertNotIn("DiagnosticDialog", source)
        self.assertNotIn("diagnose_preparation", source)
        self.assertNotIn("Lokale Diagnose", source)
        self.assertIn("self.login_button = QPushButton", source)
        self.assertIn("for pid, partner in sorted(PARTNERS.items())", source)
        self.assertNotIn("partner.get(\"token_url\")", source)
        self.assertIn("self.partner.findData(partner_id) < 0", source)

class ColumnSetupButtonTests(unittest.TestCase):
    """Je Calibre-Spalte ein Knopf in den Einstellungen."""

    def _ui(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _dashboard(ui_module, calls):
        """Stub ohne Qt-Aufbau, aber mit den echten Methoden gebunden."""
        cls = ui_module.dashboard.SyncDashboard
        dashboard = type("Dashboard", (), {})()
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = False
        dashboard.read_column_enabled = False
        dashboard.update_status = lambda: calls.append("status")
        for name in ("create_columns", "_refresh_column_flags"):
            setattr(dashboard, name, getattr(cls, name).__get__(dashboard))
        return dashboard

    def test_the_settings_offer_one_button_per_column(self):
        source = _ui_source_text()
        self.assertIn("self.column_buttons = {}", source)
        self.assertIn("for index, (label, _name, datatype) in "
                      "enumerate(CUSTOM_COLUMNS):", source)
        self.assertIn('button = QPushButton("#%s anlegen …" % label)', source)
        self.assertIn("lambda _checked=False, key=label: "
                      "self.create_columns(key)", source)
        self.assertIn("self.column_buttons[label] = button", source)
        self.assertIn('transfer.addRow("Calibre-Spalten" if index == 0 '
                      'else "", button)', source)

    def test_one_button_creates_only_its_own_column(self):
        from unittest.mock import patch
        ui_module = self._ui()
        calls, boxes, seen = [], [], []
        dashboard = self._dashboard(ui_module, calls)

        def fake_create(_db, labels):
            seen.append(list(labels))
            return list(labels)

        with patch("calibre_plugin.ui_dashboard.create_custom_columns",
                   fake_create), \
             patch("calibre_plugin.ui_dashboard.tolino_column_active",
                   lambda *_args: True), \
             patch("calibre_plugin.ui_dashboard.read_column_available",
                   lambda _db: True), \
             patch("calibre_plugin.ui_dashboard.info_dialog",
                   lambda *args, **kwargs: boxes.append(args)):
            dashboard.create_columns("tolino_ignore")
        # Genau diese eine Spalte wird angelegt -- nicht alle drei.
        self.assertEqual([["tolino_ignore"]], seen)
        self.assertIn("#tolino_ignore", boxes[0][2])
        self.assertIn("neu starten", boxes[0][2])
        self.assertTrue(dashboard.sync_column_enabled)
        self.assertTrue(dashboard.read_column_enabled)
        self.assertEqual(["status"], calls)

    def test_an_existing_column_says_so_and_changes_nothing(self):
        from unittest.mock import patch
        ui_module = self._ui()
        calls, boxes = [], []
        dashboard = self._dashboard(ui_module, calls)
        with patch("calibre_plugin.ui_dashboard.create_custom_columns",
                   lambda _db, _labels: []), \
             patch("calibre_plugin.ui_dashboard.info_dialog",
                   lambda *args, **kwargs: boxes.append(args)):
            dashboard.create_columns("tolino_read")
        self.assertIn("schon vorhanden", boxes[0][2])
        self.assertIn("#tolino_read", boxes[0][2])
        self.assertEqual([], calls)
        self.assertFalse(dashboard.read_column_enabled)

    def test_a_refused_creation_is_reported_instead_of_crashing(self):
        from unittest.mock import patch

        def boom(_db, _labels):
            raise RuntimeError(
                "Diese Calibre-Version kennt create_custom_column nicht.")

        ui_module = self._ui()
        calls, errors = [], []
        dashboard = self._dashboard(ui_module, calls)
        with patch("calibre_plugin.ui_dashboard.create_custom_columns", boom), \
             patch("calibre_plugin.ui_dashboard.log_warning",
                   lambda _text: None), \
             patch("calibre_plugin.ui_dashboard.error_dialog",
                   lambda *args, **kwargs: errors.append(args)):
            dashboard.create_columns("tolino_id")
        self.assertIn("create_custom_column", errors[0][2])
        self.assertEqual([], calls)


class DashboardPrimaryActionTests(unittest.TestCase):
    """Hauptknopf und Anmeldeknopf getrennt.

    Feldbefund: «Bring mir den Anmeldeknopf auf die erste Seite zurück.»
    Vorher war das Anmelden nur ein Zustand des Hauptknopfes – bei
    gespeichertem, aber abgelaufenem Zugang war es damit nicht erreichbar.
    """

    @staticmethod
    def _dashboard(token="", running=False):
        class LineEdit:
            def __init__(self, value):
                self.value = value

            def text(self):
                return self.value

        class Widget:
            def setText(self, text):
                self.text_value = text

            def setVisible(self, value):
                self.visible = value

            def setEnabled(self, value):
                self.enabled = value

            def setToolTip(self, text):
                self.tooltip = text

        dashboard = type("Dashboard", (), {})()
        dashboard.thread = object() if running else None
        dashboard.refresh = LineEdit(token)
        dashboard.status = Widget()
        dashboard.progress = Widget()
        dashboard.progress_label = Widget()
        dashboard.primary = Widget()
        dashboard.login_button = Widget()
        return dashboard

    def _update_status(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            return ui_module.dashboard.SyncDashboard.update_status
        finally:
            ui_module._restore_stubs_for_tests()

    def test_compare_button_follows_the_login_state(self):
        """Ohne Zugang wartet der Hauptknopf, mit Zugang ist er bereit."""
        update_status = self._update_status()
        waiting = self._dashboard("")
        update_status(waiting)
        self.assertEqual("Bibliothek vergleichen …",
                         waiting.primary.text_value)
        self.assertFalse(waiting.primary.enabled)
        self.assertIn("anmelden", waiting.primary.tooltip)
        self.assertEqual("Nicht angemeldet.", waiting.status.text_value)
        self.assertEqual("Bei tolino anmelden",
                         waiting.login_button.text_value)
        ready = self._dashboard("token-1")
        update_status(ready)
        self.assertEqual("Bibliothek vergleichen …",
                         ready.primary.text_value)
        self.assertTrue(ready.primary.enabled)
        self.assertEqual("Angemeldet.", ready.status.text_value)
        self.assertEqual("Anmeldung prüfen", ready.login_button.text_value)

    def test_a_running_sync_turns_the_button_into_cancel(self):
        update_status = self._update_status()
        dashboard = self._dashboard("token-1", running=True)
        update_status(dashboard)
        self.assertEqual("Abbrechen", dashboard.primary.text_value)
        self.assertTrue(dashboard.primary.enabled)
        self.assertTrue(dashboard.progress.visible)
        self.assertIn("Synchronisierung läuft", dashboard.status.text_value)

class SettingsLockTests(unittest.TestCase):
    """Der Einstellungs-Speicher wird aus zwei Threads benutzt.

    Der Dialog speichert im GUI-Thread, der Keep-alive rotiert den Token im
    Hintergrund. `save_account` liest UND schreibt den ganzen Konto-Block --
    ohne Lock kann ein Lesen-Aendern-Schreiben die Rotation des anderen
    Threads verlieren.
    """

    def setUp(self):
        self.original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({
            "accounts": [{"name": "konto", "partner_id": 4,
                          "hardware_id": "hw", "refresh_token": "tok", "state": {}}],
            "active_account": "konto",
        })
        self.addCleanup(setattr, config, "PREFERENCES", self.original)

    def test_two_new_accounts_do_not_share_the_state_dict(self):
        """dict(DEFAULT_ACCOUNT) kopierte den Zustand nur flach."""
        first = config._account({"name": "eins"})
        second = config._account({"name": "zwei"})
        self.assertIsNot(first["state"], second["state"])
        first["state"]["u1"] = {"tolino_id": "t1"}
        self.assertEqual({}, second["state"])
        self.assertEqual({}, config.DEFAULT_ACCOUNT["state"])

    def test_save_account_waits_for_the_settings_lock(self):
        import threading
        started = threading.Event()
        done = threading.Event()

        def worker():
            started.set()
            config.save_account("zweites", {"hardware_id": "hw-2"})
            done.set()

        with config._SETTINGS_LOCK:
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(started.wait(5))
            # Solange der Lock haelt, kommt der Hintergrund-Thread nicht durch.
            self.assertFalse(done.wait(0.3))
        thread.join(5)
        # Nach dem Freigeben laeuft er durch.
        self.assertTrue(done.is_set())
        self.assertEqual("hw-2",
                         config.account_by_name("zweites")["hardware_id"])

    def test_settings_lock_is_reentrant(self):
        """save_account ruft intern settings() und save_account -- ohne
        Reentranz wuerde das Plugin in einen Deadlock laufen."""
        lock = config._SETTINGS_LOCK
        self.assertTrue(lock.acquire(blocking=False))
        try:
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()
        finally:
            lock.release()

    def test_parallel_writes_never_lose_an_account(self):
        import threading
        errors = []

        def worker(index):
            try:
                name = "konto-%d" % index
                for _ in range(20):
                    config.save_account(name, {"hardware_id": "hw-%d" % index})
            except Exception as exc:  # pragma: no cover - unerwarteter Testfehler
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,))
                   for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual([], errors)
        names = {item["name"] for item in config.settings()["accounts"]}
        self.assertEqual({"konto"} | {"konto-%d" % index for index in range(8)},
                         names)

class TokenRotationWriteBackTests(unittest.TestCase):
    """Der Keep-alive rotiert den gespeicherten Zugang im Hintergrund,
    das Eingabefeld weiss davon nichts.

    Der Dialog schrieb den alten Token beim Schliessen oder Kontowechsel
    zurueck -- danach lehnt Tolino ihn als verbraucht ab («invalid_grant» /
    «reuse exceeded»). Ein UNVERAENDERTES Feld darf die Rotation deshalb nie
    ueberschreiben; nur eine eigene Eingabe zaehlt.
    """

    Combo = DashboardAccountSwitchTests.Combo

    def _ui(self, token="alt-token"):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        original = (config.PREFERENCES, ui_module.dashboard.hardware_id)
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({
            "accounts": [{"name": "konto", "partner_id": 4,
                          "hardware_id": "hw-1", "refresh_token": token, "state": {}}],
            "active_account": "konto",
        })
        ui_module.dashboard.hardware_id = lambda: "auto-hw"

        def restore():
            (config.PREFERENCES, ui_module.dashboard.hardware_id) = original
            ui_module._restore_stubs_for_tests()

        self.addCleanup(restore)
        dashboard = type("Dashboard", (), {})()
        for name in ("account_select", "account_quick", "partner", "hardware",
                     "refresh", "formats", "account_quick_label", "status",
                     "progress", "progress_label", "primary", "covers",
                     "deletions", "tolino_column"):
            setattr(dashboard, name, self.Combo())
        dashboard.thread = None
        dashboard.partner.items = [(str(pid), pid)
                                   for pid in sorted(PARTNERS)]
        # Die ECHTEN Methoden an das gefaelschte Fenster binden.
        for name in ("load_values", "values", "_selected_account",
                     "update_status", "set_refresh_token",
                     "_save_visible_account"):
            setattr(dashboard, name,
                    getattr(ui_module.dashboard.SyncDashboard, name).__get__(dashboard))
        dashboard.load_values()
        return ui_module, dashboard

    def _rotate(self, token):
        config.save_account("konto", {"refresh_token": token}, active="konto")

    def _stored(self, name="konto"):
        return config.account_by_name(name)["refresh_token"]

    def test_untouched_field_keeps_the_rotated_token(self):
        _ui_module, dashboard = self._ui("alt-token")
        self.assertEqual("alt-token", dashboard.refresh.text())
        self._rotate("rotiert")
        # Das Feld ist unveraendert -- gerechnet wird mit dem gespeicherten
        # (frisch rotierten) Zugang, nicht mit der alten Kopie.
        self.assertEqual("alt-token", dashboard.refresh.text())
        self.assertEqual("rotiert", dashboard.values()["refresh_token"])
        # Und das Speichern beim Schliessen verbrennt die Rotation nicht.
        dashboard._save_visible_account()
        self.assertEqual("rotiert", self._stored())

    def test_own_input_wins_over_the_stored_one(self):
        """Nur eine eigene Eingabe zählt – auch wenn sie leer ist."""
        _ui_module, dashboard = self._ui("alt-token")
        self._rotate("rotiert")
        dashboard.refresh.setText("selbst-eingetragen")
        self.assertEqual("selbst-eingetragen",
                         dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("selbst-eingetragen", self._stored())
        # Feld geleert: das Löschen muss stehen bleiben.
        dashboard.refresh.setText("")
        self.assertEqual("", dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("", self._stored())

    def test_adopted_token_survives_a_later_rotation(self):
        """Frisch gelesen heisst gewollt: der Wert steht danach im Feld UND
        im Speicher (so macht es `persist_refresh_token`). Eine spaetere
        Rotation im Hintergrund darf auch er nicht ueberschreiben."""
        _ui_module, dashboard = self._ui("alt-token")
        dashboard.set_refresh_token("frisch-gelesen")
        config.save_account("konto",
                            {"refresh_token": dashboard.refresh.text()},
                            active="konto")
        self.assertEqual("frisch-gelesen", dashboard.values()["refresh_token"])
        self._rotate("rotiert-2")
        self.assertEqual("rotiert-2", dashboard.values()["refresh_token"])
        dashboard._save_visible_account()
        self.assertEqual("rotiert-2", self._stored())

    def test_untouched_hardware_field_keeps_the_adopted_id(self):
        _ui_module, dashboard = self._ui()
        config.save_account("konto", {"hardware_id": "adoptiert"},
                            active="konto")
        self.assertEqual("adoptiert", dashboard.values()["hardware_id"])

class LoginPreflightTests(unittest.TestCase):
    """Der Anmeldeknopf prueft zuerst den GESPEICHERTEN Zugang.

    Feldwunsch: «Zuerst pruefen, ob die aktuellen Logindaten noch
    funktionieren, bevor ein neuer Loginprozess beginnt.» Die Pruefung
    laeuft still im Hintergrund; das Browser-Fenster geht nur auf, wenn
    Tolino den Zugang ablehnt oder er nicht pruefbar war.
    """

    # --- tolino.check_stored_login --------------------------------------

    @staticmethod
    def _patch_client(records, error=None, rotate=True):
        """Replace the vault's network client with a deterministic fake."""
        from contextlib import contextmanager

        @contextmanager
        def login(store, _timeout=5):
            token = store.refresh_token
            records.append((store.values.get("partner_id"),
                            store.hardware_id, token))
            class FakeClient:
                refresh = token

                def login(self):
                    if error is not None:
                        raise error
                    if rotate:
                        self.refresh = token + "-rotated"
                        store.save_refresh(self.refresh)
                        records.append(("persisted", self.refresh))
                    return self.refresh

            client = FakeClient()
            if rotate:
                client.login()
            yield client

        return patch("calibre_plugin.token_vault.TOKEN_VAULT.login", login)

    def test_a_working_access_is_renewed_and_saved(self):
        records = []
        store = config.CredentialStore(
            {"partner_id": 4, "hardware_id": "hw-1",
             "refresh_token": "tok-1"}, "konto")
        with self._patch_client(records):
            status, detail = vault_module.check_stored_login(store)
        self.assertEqual("valid", status)
        self.assertEqual("tok-1-rotated", detail)
        self.assertEqual((4, "hw-1", "tok-1"), records[0])
        self.assertEqual("tok-1-rotated", store.refresh_token)

    def test_an_unchanged_token_is_not_written_again(self):
        records = []
        store = config.CredentialStore(
            {"partner_id": 4, "hardware_id": "hw-1",
             "refresh_token": "tok-1"}, "konto")
        with self._patch_client(records, rotate=False):
            status, detail = vault_module.check_stored_login(store)
        self.assertEqual(("valid", "tok-1"), (status, detail))
        self.assertEqual([], records[1:])

    def test_a_rejected_access_reports_the_reason_without_the_token(self):
        records = []
        error = TolinoAuthError(
            "invalid_grant: refresh_token tok-1 wurde abgelehnt")
        with self._patch_client(records, error=error):
            status, detail = vault_module.check_stored_login(
                config.CredentialStore({"partner_id": 4,
                                        "hardware_id": "hw-1",
                                        "refresh_token": "tok-1"}, "konto"))
        self.assertEqual("invalid", status)
        self.assertNotIn("tok-1", detail)
        self.assertIn("[REDACTED]", detail)
        self.assertIn("invalid_grant", detail)

    def test_without_a_stored_token_nothing_is_asked(self):
        store = config.CredentialStore(
            {"partner_id": 4, "hardware_id": "hw-1", "refresh_token": ""},
            "konto")
        # Andere Tests hinterlassen ein gespeichertes Konto «konto» mit Token;
        # ohne diese Abschirmung loeste der Test einen echten Login aus.
        with patch("calibre_plugin.config.account_by_name",
                   lambda *_args, **_kwargs: {}):
            status, detail = vault_module.check_stored_login(store)
        self.assertEqual("invalid", status)
        self.assertIn("Bitte bei tolino anmelden", detail)

    def test_a_running_sync_blocks_the_check(self):
        records = []
        with vault_module.TOKEN_VAULT._lock:
            status, detail = vault_module.check_stored_login(
                config.CredentialStore({"partner_id": 4,
                                        "hardware_id": "hw-1",
                                        "refresh_token": "tok-1"}, "konto"))
        # NICHT „invalid": der Zugang ist ungeprueft, nicht abgelehnt.
        self.assertEqual(("busy", ""), (status, detail))
        self.assertEqual([], records)

    def test_a_parallel_rotation_blocks_the_check(self):
        records = []
        with vault_module.TOKEN_VAULT._lock:
            status, detail = vault_module.check_stored_login(
                config.CredentialStore({"partner_id": 4,
                                        "hardware_id": "hw-1",
                                        "refresh_token": "tok-1"}, "konto"),
                lock_timeout=0)
        self.assertEqual(("busy", ""), (status, detail))
        self.assertEqual([], records)

    def test_a_late_401_uses_the_latest_stored_refresh_token(self):
        store = config.CredentialStore(
            {"account_name": "konto", "partner_id": 4,
             "hardware_id": "hw-1", "refresh_token": "saved-old"}, "konto")
        requested = []

        class RefreshClient:
            def __init__(self, partner, hardware, refresh, **callbacks):
                self.refresh = refresh
                self.hardware = hardware
                self.token_callback = callbacks.get("token_callback")

            def login(self):
                if getattr(self, "token_lock_held", False):
                    return self._rotate()
                with self.token_lock:
                    return self._rotate()

            def _rotate(self):
                requested.append(self.refresh)
                if len(requested) == 1:
                    self.refresh = "rotated"
                    self.token_callback(self.refresh)
                return self.refresh

        from ..token_vault import TOKEN_VAULT
        with patch("calibre_plugin.token_vault.TolinoClient", RefreshClient), \
             patch("calibre_plugin.config.account_by_name",
                   side_effect=lambda _name: {
                       "refresh_token": store.refresh_token,
                       "hardware_id": store.hardware_id}):
            with TOKEN_VAULT.login(store) as client:
                store.save_refresh("rotated-by-keepalive")
                client.access = None
                client.login()
        self.assertEqual(["saved-old", "rotated"], requested)

    def test_the_rotation_lock_is_released_afterwards(self):
        with self._patch_client([]):
            vault_module.check_stored_login(config.CredentialStore(
                {"partner_id": 4, "hardware_id": "hw-1",
                 "refresh_token": "tok-1"}, "konto"))
        self.assertTrue(vault_module.TOKEN_VAULT._lock.acquire(blocking=False))
        vault_module.TOKEN_VAULT._lock.release()

    # --- Der Anmeldeknopf -----------------------------------------------

    class Field:
        def __init__(self, value=""):
            self.value = value
            self.enabled = True

        def text(self):
            return self.value

        def setText(self, value):
            self.value = value

        def setEnabled(self, value):
            self.enabled = value

        def setToolTip(self, value):
            self.tooltip = value

    def _ui(self):
        ui_module = ui_module_for(self)
        ui_module.dashboard.hardware_id = lambda: "auto-hw"
        return ui_module

    @staticmethod
    def _fake_qt(ui_module):
        """QThread und beide Worker durch Recorder ersetzen."""

        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def fire(self, *args):
                for slot in list(self.slots):
                    slot(*args)

        class Thread:
            def __init__(self, parent=None):
                self.started = Signal()
                self.finished = Signal()
                self.starts = 0

            def start(self):
                self.starts += 1
                self.started.fire()

            def isRunning(self):
                return self.starts > 0

            def quit(self, *args):
                self.quits = getattr(self, "quits", 0) + 1

            def deleteLater(self, *args):
                self.deleted = True

        class CheckWorker:
            def __init__(self, settings):
                self.settings = settings
                self.valid = Signal()
                self.invalid = Signal()
                self.busy = Signal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        class LoginWorker:
            def __init__(self, partner_id, hardware):
                self.arguments = (partner_id, hardware)
                self.progress = Signal()
                self.completed = Signal()
                self.failed = Signal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        ui_module.dashboard.QThread = Thread
        ui_module.dashboard.SessionCheckWorker = CheckWorker
        ui_module.dashboard.BrowserLoginWorker = LoginWorker
        return Thread, CheckWorker, LoginWorker

    @staticmethod
    def _boxes(ui_module, seen):
        """QMessageBox.information/warning als Recorder.

        Der Aufruf im Dialog beginnt mit `self`, danach folgen Titel und
        Text -- die Zusicherungen lesen deshalb Index 1 und 2.
        """

        def record(*args, **kwargs):
            seen.append(args)

        ui_module.dashboard.QMessageBox = type("QMessageBox", (), {
            "information": staticmethod(record),
            "warning": staticmethod(record),
        })

    def _assert_one_line(self, box, title):
        """Die neuen Infofenster bleiben EINE Zeile (Feldwunsch)."""
        self.assertEqual(title, box[1])
        text = box[2]
        self.assertNotIn("\n", text)
        self.assertLessEqual(len(text), 100, text)

    def _dashboard(self, ui_module, token="tok-1"):
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "konto"
        dashboard.login_thread = None
        dashboard.login_worker = None
        dashboard.refresh = self.Field(token)
        dashboard.hardware = self.Field("hw-1")
        dashboard.primary = self.Field()
        dashboard.status = self.Field()
        dashboard.update_status = lambda: None
        dashboard.settings_values = {
            "account_name": "konto", "partner_id": 4,
            "hardware_id": "hw-1", "refresh_token": token,
        }
        dashboard.values = lambda: dashboard.settings_values
        # Die beiden Ergebnis-Slots des Anmeldefensters: hier nur Empfaenger.
        dashboard.login_completed = lambda *args: None
        dashboard.login_failed = lambda *args: None
        names = ("browser_login", "_check_stored_login",
                 "_start_login_window", "_login_running",
                 "login_still_valid", "login_rejected", "login_check_busy",
                 "_login_cleanup", "set_refresh_token",
                 "persist_refresh_token", "_login_progress")
        for name in names:
            setattr(dashboard, name,
                    getattr(ui_module.dashboard.SyncDashboard, name).__get__(dashboard))
        return dashboard

    def test_without_a_stored_token_the_window_opens_right_away(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module, token="")
        opened = []
        dashboard._start_login_window = lambda values: opened.append(values)
        dashboard.browser_login()
        self.assertEqual([""], [values["refresh_token"] for values in opened])
        self.assertIsNone(dashboard.login_thread)

    def test_with_a_token_the_stored_access_is_checked_first(self):
        ui_module = self._ui()
        Thread, CheckWorker, _ = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened = []
        dashboard._start_login_window = lambda values: opened.append(values)
        dashboard.browser_login()
        self.assertEqual([], opened)
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsInstance(dashboard.login_worker, CheckWorker)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertIs(dashboard.settings_values,
                      dashboard.login_worker.settings)
        self.assertIs(dashboard.login_thread,
                      dashboard.login_worker.moved_to)
        self.assertIn("geprüft", dashboard.status.text())
        self.assertFalse(dashboard.primary.enabled)

    def test_a_working_access_skips_the_login_window(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened, saved, boxes = [], [], []
        dashboard._start_login_window = lambda values: opened.append(values)
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui_dashboard.save_credentials",
                   lambda *args: saved.append(args)):
            dashboard.browser_login()
            dashboard.login_worker.valid.fire("tok-rotated")
        self.assertEqual([], opened)
        self.assertEqual([("konto", "tok-rotated", "hw-1")], saved)
        self.assertEqual("tok-rotated", dashboard.refresh.text())
        self.assertIn("Angemeldet.", dashboard.status.text())
        # Knopf und Fenster sind wieder frei, die Pruefung ist beendet.
        self.assertIsNone(dashboard.login_thread)
        dashboard.refresh.setText("tok-rotated")
        dashboard.progress = type("Widget", (), {"setVisible": lambda self, value: None})()
        dashboard.progress_label = type("Widget", (), {"setVisible": lambda self, value: None})()
        ui_module.dashboard.SyncDashboard.update_status(dashboard)
        self.assertTrue(dashboard.primary.enabled)
        self.assertIn("Angemeldet.", dashboard.status.text())
        self.assertEqual([], boxes)

    def test_a_rejected_access_opens_the_login_window(self):
        ui_module = self._ui()
        Thread, CheckWorker, LoginWorker = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        boxes, logged = [], []
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui_dashboard.log_warning",
                   lambda message: logged.append(message)):
            dashboard.browser_login()
            check_thread = dashboard.login_thread
            self.assertIsInstance(dashboard.login_worker, CheckWorker)
            dashboard.login_worker.invalid.fire("invalid_grant (ohne Token)")
        self.assertEqual([], boxes)
        self.assertEqual(1, len(logged))
        self.assertIn("invalid_grant", logged[0])
        self.assertIn("Bitte im Browserfenster anmelden", dashboard.status.text())
        # Der neue Anlauf ist ein eigener Thread, nicht der Pruef-Thread:
        # die Ablehnung oeffnet wirklich das Anmeldefenster.
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsNot(check_thread, dashboard.login_thread)
        self.assertIsInstance(dashboard.login_worker, LoginWorker)
        self.assertEqual((4, "hw-1"), dashboard.login_worker.arguments)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertIn("Bitte im Browserfenster anmelden", dashboard.status.text())

    def test_a_busy_check_opens_nothing_and_keeps_the_token(self):
        ui_module = self._ui()
        self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        opened, saved, boxes = [], [], []
        dashboard._start_login_window = lambda values: opened.append(values)
        self._boxes(ui_module, boxes)
        with patch("calibre_plugin.ui_dashboard.save_credentials",
                   lambda *args: saved.append(args)):
            dashboard.browser_login()
            dashboard.login_worker.busy.fire("")
        self.assertEqual([], opened)
        self.assertEqual([], saved)
        self.assertEqual("tok-1", dashboard.refresh.text())
        self.assertIsNone(dashboard.login_thread)
        self.assertIn("Anmeldung wird gerade erneuert", dashboard.status.text())
        dashboard.refresh.setText("tok-1")
        dashboard.progress = type("Widget", (), {"setVisible": lambda self, value: None})()
        dashboard.progress_label = type("Widget", (), {"setVisible": lambda self, value: None})()
        ui_module.dashboard.SyncDashboard.update_status(dashboard)
        self.assertTrue(dashboard.primary.enabled)
        self.assertEqual([], boxes)

    def test_a_second_click_while_checking_is_ignored(self):
        ui_module = self._ui()
        check_thread = type("Thread", (), {
            "isRunning": lambda self: True})()
        dashboard = self._dashboard(ui_module)
        dashboard.login_thread = check_thread
        calls = []
        dashboard._check_stored_login = lambda values: calls.append(values)
        dashboard._start_login_window = lambda values: calls.append(values)
        dashboard.browser_login()
        self.assertEqual([], calls)
        self.assertIs(check_thread, dashboard.login_thread)

    def test_the_window_step_starts_its_own_sign_in(self):
        ui_module = self._ui()
        Thread, _, LoginWorker = self._fake_qt(ui_module)
        dashboard = self._dashboard(ui_module)
        dashboard._start_login_window(dashboard.values())
        self.assertIsInstance(dashboard.login_thread, Thread)
        self.assertIsInstance(dashboard.login_worker, LoginWorker)
        self.assertEqual((4, "hw-1"), dashboard.login_worker.arguments)
        self.assertEqual(1, dashboard.login_thread.starts)
        self.assertFalse(dashboard.primary.enabled)
        self.assertIn("Bitte im Browserfenster anmelden", dashboard.status.text())

    def _run_check_worker(self, ui_module, result):
        """`SessionCheckWorker.run()` mit gestubbter Pruefung laufen lassen.

        `result` ist das, was `check_stored_login` liefert -- oder eine
        Exception, die es stattdessen wirft. Zurueck kommen der Worker, die
        gemeldeten Signale und die Argumente der Pruefung.
        """

        def check(*args, **kwargs):
            calls.append((args, kwargs))
            if isinstance(result, Exception):
                raise result
            return result

        def signal(name):
            return type("Signal", (), {
                "emit": lambda self, value: emitted.append((name, value))})()

        emitted, calls = [], []
        settings = {"partner_id": 4, "hardware_id": "hw-1",
                    "refresh_token": "tok-1", "account_name": "konto"}
        worker = ui_module.auth_workers.SessionCheckWorker(settings)
        worker.valid, worker.invalid, worker.busy = (signal("valid"),
                                                     signal("invalid"),
                                                     signal("busy"))
        ui_module.auth_workers.check_stored_login = check
        worker.run()
        return worker, emitted, calls

    def test_the_worker_hands_the_stored_account_to_the_check(self):
        ui_module = self._ui()
        _worker, emitted, calls = self._run_check_worker(
            ui_module, ("valid", "tok-2"))
        args, kwargs = calls[0]
        self.assertEqual(1, len(args))
        self.assertEqual("konto", args[0].account_name)
        self.assertEqual("tok-1", args[0].refresh_token)
        self.assertEqual({}, kwargs)
        self.assertEqual([("valid", "tok-2")], emitted)

    def test_every_check_result_reaches_the_matching_slot(self):
        ui_module = self._ui()
        results = (("valid", "tok-2"), ("invalid", "Grund"), ("busy", ""))
        reported = [self._run_check_worker(ui_module, result)[1]
                    for result in results]
        self.assertEqual([[result] for result in results], reported)

    def test_a_failing_check_is_reported_as_a_rejection(self):
        ui_module = self._ui()
        _worker, emitted, _calls = self._run_check_worker(
            ui_module, RuntimeError("kaputt mit tok-1"))
        self.assertEqual(1, len(emitted))
        self.assertEqual("invalid", emitted[0][0])
        self.assertIn("kaputt", emitted[0][1])
        # Auch der unerwartete Fehler verraet den Zugang nicht.
        self.assertNotIn("tok-1", emitted[0][1])

    def test_the_vault_persists_rotations_into_the_worker_snapshot(self):
        settings = {"account_name": "konto", "partner_id": 4,
                    "hardware_id": "hw-1", "refresh_token": "tok-1"}
        store = config.CredentialStore(settings, "konto")
        callback = vault_module.TOKEN_VAULT._callback_for_value(
            store.refresh_token, store.save_refresh)
        with patch("calibre_plugin.config.save_credentials") as save:
            callback("tok-2")
        self.assertEqual("tok-2", settings["refresh_token"])
        save.assert_called_once_with("konto", "tok-2", "hw-1")

class LoginMessageTests(unittest.TestCase):
    """P2: Erfolg in der Statuszeile, Ursachen klar getrennt."""

    def _ui(self):
        return ui_module_for(self)

    def test_completion_needs_no_modal_dialog(self):
        ui_module = self._ui()
        boxes = []

        dashboard = type("Dashboard", (), {})()
        dashboard.status = FakeText()
        dashboard.hardware = FakeText()
        dashboard.set_refresh_token = lambda refresh: None
        dashboard.persist_refresh_token = lambda refresh: None
        dashboard.update_status = lambda: None
        dashboard._login_cleanup = lambda: None
        dashboard.login_completed = ui_module.dashboard.SyncDashboard.login_completed.__get__(
            dashboard)
        ui_module.dashboard.QMessageBox = type("Box", (), {
            "information": staticmethod(lambda *args: boxes.append(args)),
            "warning": staticmethod(lambda *args: boxes.append(args)),
        })
        try:
            dashboard.login_completed("tok-neu", "hw-neu")
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], boxes)
        self.assertIn("Angemeldet.", dashboard.status.value)

    def test_successful_login_without_open_window_has_no_close_warning(self):
        """Die UI fragt den Fensterzustand ab, statt ein zweites Mal zu schliessen."""
        ui_module = self._ui()
        self.addCleanup(ui_module._restore_stubs_for_tests)

        dashboard = type("Dashboard", (), {})()
        dashboard.status = FakeText()
        dashboard.hardware = FakeText()
        dashboard.set_refresh_token = lambda refresh: None
        dashboard.persist_refresh_token = lambda refresh: None
        dashboard.update_status = lambda: None
        dashboard._login_cleanup = lambda: None
        dashboard.login_completed = ui_module.dashboard.SyncDashboard.login_completed.__get__(
            dashboard)

        with patch.object(ui_module.dashboard, "devtools_port_alive",
                          return_value=False, create=True) as alive, \
             patch.object(ui_module.dashboard, "close_grabber_window",
                          create=True) as close:
            dashboard.login_completed("tok-neu", "hw-neu")
        alive.assert_called_once_with()
        close.assert_not_called()
        self.assertNotIn("von Hand", dashboard.status.value)
        self.assertIn("Angemeldet.", dashboard.status.value)

    def test_the_primary_button_becomes_the_next_step(self):
        """Kein Dialog nach der Anmeldung -- der Hauptknopf ist der
        naechste Schritt und hat den Fokus."""
        ui_module = self._ui()

        class Primary:
            def __init__(self):
                self.calls = []

            def setDefault(self, value):
                self.calls.append(("default", value))

            def setFocus(self):
                self.calls.append(("focus",))

        dashboard = type("Dashboard", (), {})()
        dashboard.status = FakeText()
        dashboard.hardware = FakeText()
        dashboard.primary = Primary()
        dashboard.set_refresh_token = lambda refresh: None
        dashboard.persist_refresh_token = lambda refresh: None
        dashboard.update_status = lambda: None
        dashboard._login_cleanup = lambda: None
        dashboard.login_completed = ui_module.dashboard.SyncDashboard.login_completed.__get__(
            dashboard)
        dashboard.login_still_valid = ui_module.dashboard.SyncDashboard.login_still_valid.__get__(
            dashboard)
        try:
            dashboard.login_completed("tok-neu", "hw-neu")
            self.assertEqual([("default", True), ("focus",)],
                             dashboard.primary.calls)
            dashboard.primary.calls = []
            dashboard.login_still_valid("tok-neu")
            self.assertEqual([("default", True), ("focus",)],
                             dashboard.primary.calls)
            self.assertIn("Angemeldet.", dashboard.status.value)
        finally:
            ui_module._restore_stubs_for_tests()

    def test_each_cause_gets_its_own_next_step(self):
        ui_module = self._ui()
        try:
            guide = ui_module.common._login_failure_guidance
            titles = {}
            for code in ("B05", "B06", "B08", "N01", "A02", "B07", "B03", "B04", "A06", "A05"):
                exc = tolino_module.TolinoAuthError("egal", code=code)
                title, step = guide(exc)
                titles[code] = title
                self.assertNotIn("\n", step)
                self.assertLessEqual(len(step), 100, step)
                self.assertLessEqual(len(title), 100, title)
            self.assertEqual(len(titles), len(set(titles.values())),
                             "jede Ursache braucht einen eigenen Titel")
            self.assertEqual(
                "Kein unterstützter Browser gefunden", titles["B05"])
            self.assertEqual("Flatpak: Freigabe fehlt", titles["B08"])
            self.assertEqual("Verbindung fehlgeschlagen", titles["N01"])
            self.assertEqual("Zugang verbraucht", titles["A02"])
            self.assertEqual("Anmeldung abgebrochen", titles["B03"])
            self.assertEqual("Zeitüberschreitung bei der Anmeldung",
                             titles["B04"])
            self.assertEqual("Anmeldung abgelehnt", titles["A06"])
            # Bereits formatierte Meldungen (ältere Worker) tragen den Code
            # im Text.
            self.assertEqual(titles["B04"],
                             guide("Zeitüberschreitung (Code B04)")[0])
            self.assertEqual("Anmeldung nicht abgeschlossen",
                             guide(RuntimeError("kaputt"))[0])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_failure_shows_the_step_but_never_the_technical_detail(self):
        ui_module = self._ui()
        boxes, logged = [], []

        class Refresh:
            @staticmethod
            def text():
                return "tok-geheim"

        dashboard = type("Dashboard", (), {})()
        dashboard.status = FakeText()
        dashboard.refresh = Refresh()
        dashboard._login_cleanup = lambda: None
        dashboard.login_failed = ui_module.dashboard.SyncDashboard.login_failed.__get__(
            dashboard)
        ui_module.dashboard.QMessageBox = type("Box", (), {
            "information": staticmethod(lambda *args: boxes.append(args)),
            "warning": staticmethod(lambda *args: boxes.append(args)),
        })
        try:
            with patch("calibre_plugin.ui_dashboard.log_warning",
                       lambda text: logged.append(text)):
                dashboard.login_failed(tolino_module.TolinoAuthError(
                    "Kein Chromium-Browser gefunden (tok-geheim)", code="B05"))
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(boxes))
        self.assertEqual("Kein unterstützter Browser gefunden (Code B05)", boxes[0][1])
        self.assertIn("installieren", boxes[0][2])
        self.assertNotIn("tok-geheim", boxes[0][2])
        self.assertNotIn("tok-geheim", " ".join(logged))
        self.assertIn("Anmeldung fehlgeschlagen.", dashboard.status.value)

class AccountStateBindingTests(unittest.TestCase):
    """Das im Dialog angezeigte Konto IST das Konto, dessen State
    gelesen und geschrieben wird – nur eine Quelle je Konto, und die
    Tolino-ID-Spalte folgt EINER Regel (nur bei genau einem Konto)."""

    def test_account_by_name_ignores_the_active_account(self):
        accounts = [
            {"name": "alpha", "state": {"u1": {"tolino_id": "id-a"}}},
            {"name": "beta", "state": {"u2": {"tolino_id": "id-b"}}},
        ]
        self.assertEqual({"u1": {"tolino_id": "id-a"}},
                         config.account_by_name("alpha", accounts)["state"])
        self.assertEqual({"u2": {"tolino_id": "id-b"}},
                         config.account_by_name("beta", accounts)["state"])
        self.assertEqual({}, config.account_by_name("gone", accounts))

    def test_set_active_account_switches_without_touching_state(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.settings()
            config.save_account(
                "alpha", {"state": {"u1": {"tolino_id": "id-a"}}},
                active="alpha")
            config.save_account(
                "beta", {"state": {"u2": {"tolino_id": "id-b"}}},
                active="beta")
            config.set_active_account("alpha")
            values = config.settings()
            self.assertEqual("alpha", values["active_account"])
            self.assertEqual({"u1": {"tolino_id": "id-a"}}, values["state"])
            # Das andere Konto bleibt unberuehrt.
            self.assertEqual({"u2": {"tolino_id": "id-b"}},
                             config.account_by_name("beta")["state"])
        finally:
            config.PREFERENCES = original

    def test_tolino_column_active_supports_multiple_accounts(self):
        from ..library import tolino_column_active

        class Db:
            field_metadata = {TOLINO_COLUMN: {}}

        class NoColumn:
            field_metadata = {}

        values = {"use_tolino_column": True,
                  "accounts": [{"name": "a"}, {"name": "b"}]}
        # Die Spalte enthält pro Buch ein Mapping, daher bleibt sie bei
        # mehreren Konten nutzbar.
        self.assertTrue(tolino_column_active(values, Db()))
        values["accounts"] = [{"name": "a"}]
        self.assertTrue(tolino_column_active(values, Db()))
        values["use_tolino_column"] = False
        self.assertFalse(tolino_column_active(values, Db()))
        values["use_tolino_column"] = True
        self.assertFalse(tolino_column_active(values, NoColumn()))
        self.assertFalse(tolino_column_active({}, Db()))

    def test_settings_dialog_keeps_required_widgets_without_diagnostic_ui(self):
        base = Path(__file__).resolve().parent.parent
        settings = (base / "ui_settings.py").read_text(encoding="utf-8")
        self.assertIn("QGroupBox", settings.split("class SettingsDialog", 1)[0])
        self.assertIn("accounts_form", settings)
        self.assertNotIn("diagnostics_form", settings)

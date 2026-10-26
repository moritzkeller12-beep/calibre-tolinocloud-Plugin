"""Ablauf vom Vergleichsfenster bis zum Sync-Start."""

import os
import tempfile
import unittest
from . import ui_base_tests
from .. import config
from ._support import start_cdp_stubs, stop_cdp_stubs
from unittest.mock import patch


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


def _preparation(**overrides):
    """Leerer Sync-Plan; Tests überschreiben nur, was sie brauchen."""
    preparation = {"jobs": [], "removals": [], "removal_titles": {},
                   "meta_updates": [], "series_map": {},
                   "skipped_uploads": [], "selected_read": [],
                   "selected_unread": [], "selected_without_action": {}}
    preparation.update(overrides)
    return preparation


class StartSyncOrchestrationTests(unittest.TestCase):
    """`start_sync` bleibt Ablaufsteuerung, die Arbeit steckt in Schritten.

    Vorher las, anmeldete, verglich, plante und startete eine einzige
    160-Zeilen-Methode. Diese Tests halten Reihenfolge, Fehlergrenze und
    die Weitergabe der Werte an die neuen Schritte fest.
    """

    @staticmethod
    def _ui_module():
        return ui_base_tests.ToolbarIconTests._load_ui_module(lambda *args, **kwargs: None)

    @staticmethod
    def _settings():
        return {
            "refresh_token": "token-1", "partner_id": "thalia",
            "hardware_id": "hw-1",
            "state": {}, "preferred_formats": ("EPUB",),
        }

    def _dashboard(self, ui_module, settings, thread=None):
        dashboard = ui_module.dashboard.SyncDashboard.__new__(ui_module.dashboard.SyncDashboard)
        dashboard.thread = thread
        dashboard.values = lambda: settings
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = None
        return dashboard

    @staticmethod
    def _stub_warning(ui_module, warnings):
        """QMessageBox.warning durch einen Recorder ersetzen."""

        def warning(*args, **kwargs):
            warnings.append(args)

        ui_module.dashboard.QMessageBox = type(
            "QMessageBox", (), {"warning": staticmethod(warning)})

    @staticmethod
    def _install_fake_qt(ui_module):
        """QThread/SyncWorker durch Recorder ersetzen (Thread-Start messbar)."""

        class FakeSignal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

        class FakeThread:
            def __init__(self, parent=None):
                self.started = FakeSignal()
                self.finished = FakeSignal()
                self.starts = 0

            def start(self):
                self.starts += 1

            def deleteLater(self):
                self.deleted = True

        class FakeWorker:
            # `titles` wird bewusst nur entgegengenommen: die
            # Tests pruefen die Uebergabe, nicht die Anzeige.
            def __init__(self, settings, jobs, removals, meta_updates=None,
                         series_map=None, selected_read=(),
                         selected_unread=(), titles=None):
                self.arguments = (settings, jobs, removals, meta_updates,
                                  series_map, selected_read, selected_unread)
                self.titles = titles
                self.progress = FakeSignal()
                self.completed = FakeSignal()
                self.failed = FakeSignal()

            def moveToThread(self, thread):
                self.moved_to = thread

            def run(self):
                pass

        ui_module.dashboard.QThread = FakeThread
        ui_module.dashboard.SyncWorker = FakeWorker
        return FakeThread, FakeWorker

    def test_the_work_goes_to_the_named_steps(self):
        """Lokaler Snapshot im GUI, Netz-Vorbereitung im Hintergrund."""
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            ui_module.dashboard.tolino_column_active = lambda *args: True
            dashboard = self._dashboard(ui_module, settings)
            dashboard.gui.current_db = type(
                "Database", (), {"field_metadata": {"#tolino_id": {}}})()
            seen = []
            metadata = {1: {"title": "A"}}
            dashboard._local_metadata = lambda: (
                seen.append(("metadata",)) or metadata)
            dashboard._start_prepare_worker = lambda given, meta: seen.append(
                ("prepare", given, meta))
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(["metadata", "prepare"], [item[0] for item in seen])
        self.assertIs(settings, seen[1][1])
        self.assertIs(metadata, seen[1][2])
        self.assertTrue(settings["sync_column_enabled"])

    def test_sync_column_availability_accepts_calibre_field_metadata(self):
        ui_module = self._ui_module()
        original_preferences = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES.update({
                "accounts": [{"name": "konto", "partner_id": 4,
                              "hardware_id": "hw", "refresh_token": "token",
                              "state": {}}],
                "active_account": "konto",
                "use_tolino_column": True,
            })

            class FieldMetadata:
                def __contains__(self, key):
                    return key == "#tolino_id"

            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            dashboard.gui.current_db = type(
                "Database", (), {"field_metadata": FieldMetadata()})()
            dashboard._save_visible_account = lambda _values: None
            dashboard._local_metadata = lambda: {}
            dashboard._start_prepare_worker = lambda *_args: None
            dashboard.start_sync()
            self.assertTrue(dashboard.sync_column_enabled)
        finally:
            config.PREFERENCES = original_preferences
            ui_module._restore_stubs_for_tests()

    def test_sync_column_uses_global_settings_not_visible_values(self):
        ui_module = self._ui_module()
        original_preferences = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES.update({
                "accounts": [{"name": "konto", "partner_id": 4,
                              "hardware_id": "hw", "refresh_token": "token",
                              "state": {}}],
                "active_account": "konto",
                "use_tolino_column": True,
            })
            cases = (({"#tolino_id": {}}, 1, True),
                     ({"#tolino_id": {}}, 2, True),
                     ({}, 1, False))
            for fields, account_count, expected in cases:
                with self.subTest(fields=fields, account_count=account_count):
                    config.PREFERENCES["accounts"] = [
                        {"name": "konto-%d" % index, "partner_id": 4,
                         "hardware_id": "hw", "refresh_token": "token",
                         "state": {}}
                        for index in range(account_count)]
                    config.PREFERENCES["active_account"] = "konto-0"
                    settings = self._settings()
                    dashboard = self._dashboard(ui_module, settings)
                    dashboard.gui.current_db = type(
                        "Database", (), {"field_metadata": fields})()
                    dashboard._save_visible_account = lambda _values: None
                    dashboard._local_metadata = lambda: {}
                    dashboard._start_prepare_worker = lambda *_args: None
                    dashboard.start_sync()
                    self.assertIs(expected, dashboard.sync_column_enabled)
                    self.assertIs(expected, settings["sync_column_enabled"])
        finally:
            config.PREFERENCES = original_preferences
            ui_module._restore_stubs_for_tests()

    def test_without_a_token_it_does_not_start_comparison(self):
        ui_module = self._ui_module()
        try:
            warnings = []
            self._stub_warning(ui_module, warnings)
            ui_module.dashboard.tolino_column_active = lambda *args: True
            settings = dict(self._settings(), refresh_token="")
            dashboard = self._dashboard(ui_module, settings)
            prepared = []
            dashboard._start_prepare_worker = lambda given, meta: prepared.append(given)
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], warnings)
        self.assertEqual([], prepared)

    def test_a_failed_local_snapshot_is_reported_and_stops_everything(self):
        ui_module = self._ui_module()
        try:
            errors = []
            ui_module.dashboard.tolino_column_active = lambda *args: True
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)

            def boom():
                raise RuntimeError("kaputt")

            dashboard._local_metadata = boom
            dashboard._show_prepare_error = lambda exc, given: errors.append(
                (exc, given))
            started = []
            dashboard._start_prepare_worker = lambda given, meta: started.append(meta)
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(errors))
        self.assertIn("kaputt", str(errors[0][0]))
        self.assertEqual([], started)

    def test_a_cancelled_dialog_starts_nothing(self):
        """Abbruch im Bestandsvergleich startet keinen Sync-Worker."""
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            dashboard = ui_module.dashboard.SyncDashboard.__new__(
                ui_module.dashboard.SyncDashboard)
            dashboard.account_name = "konto"
            dashboard.thread = None
            dashboard.prepare_thread = None
            dashboard.prepare_worker = None
            dashboard._finish_prepare = lambda: None
            dashboard.set_refresh_token = lambda value: None
            dashboard._client_for = lambda given: object()
            dashboard._confirm_comparison = (
                lambda comp, given, client_factory=None, automatic=0: None)
            planned, started = [], []
            dashboard._plan_actions = lambda *args: planned.append(args)
            dashboard._start_sync_worker = lambda *args: started.append(args)
            dashboard._prepare_ready({
                "account_name": "konto", "settings": settings,
                "metadata": {}, "state": {}, "comparison": [],
                "refresh": "tok", "hardware": "hw",
            })
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], planned)
        self.assertEqual([], started)

    def test_a_running_sync_ignores_the_second_click(self):
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings, thread=object())
            calls = []
            dashboard.values = lambda: calls.append("values")
            dashboard._start_sync_worker = lambda given, prep: calls.append("worker")
            dashboard.start_sync()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], calls)

    def test_the_worker_gets_the_finished_plan(self):
        ui_module = self._ui_module()
        try:
            self._install_fake_qt(ui_module)
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            ranges, values, labels, status_updates = [], [], [], []
            dashboard.progress = type("Bar", (), {
                "setRange": lambda self, *args: ranges.append(args),
                "setValue": lambda self, value: values.append(value),
            })()
            dashboard.progress_label = type("Label", (), {
                "setText": lambda self, text: labels.append(text),
            })()
            dashboard.update_status = lambda: status_updates.append(True)
            for name in ("progress_changed", "sync_completed", "sync_failed"):
                setattr(dashboard, name, lambda *args: None)
            jobs = ["job-1", "job-2"]
            preparation = {
                "jobs": jobs, "removals": ["remove-1"],
                "meta_updates": [("uuid-1", 7)],
                "series_map": {"uuid-serie": "Reihe"},
                "skipped_uploads": [], "selected_read": {"t-read"},
                "selected_unread": {"t-unread"},
            }
            # Offene Serien-Zuordnung: das Buch hat eine Tolino-ID und noch
            # keinen gemerkten Sammlungs-Eintrag.
            dashboard._start_sync_worker(
                settings, preparation, {"uuid-serie": {"tolino_id": "t-serie"}})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(settings, dashboard.worker.arguments[0])
        self.assertEqual(jobs, dashboard.worker.arguments[1])
        self.assertEqual(["remove-1"], dashboard.worker.arguments[2])
        self.assertEqual([("uuid-1", 7)], dashboard.worker.arguments[3])
        self.assertEqual({"uuid-serie": "Reihe"}, dashboard.worker.arguments[4])
        self.assertEqual({"t-read"}, dashboard.worker.arguments[5])
        self.assertEqual({"t-unread"}, dashboard.worker.arguments[6])
        self.assertIs(dashboard.thread, dashboard.worker.moved_to)
        self.assertEqual(1, dashboard.thread.starts)
        self.assertEqual(1, len(dashboard.worker.progress.slots))
        self.assertEqual(1, len(dashboard.worker.completed.slots))
        self.assertEqual(1, len(dashboard.worker.failed.slots))
        # 2 Uploads + 1 Löschung + 1 Metadaten + 1 Serie + Lesen + Ungelesen.
        self.assertEqual([(0, 7)], ranges)
        self.assertEqual([0, 0], values)
        # Die Statuszeile nennt den Plan, bevor die erste Aktion läuft.
        self.assertIn("2 Uploads", labels[0])
        self.assertIn("1 Löschung", labels[0])
        self.assertEqual([True], status_updates)

    def test_prepare_ready_hands_the_rotated_token_to_the_plan(self):
        """Der SyncWorker darf nie den verbrauchten Token der Vorbereitung
        bekommen: die Rotation muss vor dem Plan in settings stehen."""
        ui_module = self._ui_module()
        try:
            settings = self._settings()
            dashboard = ui_module.dashboard.SyncDashboard.__new__(
                ui_module.dashboard.SyncDashboard)
            dashboard.account_name = "konto"
            dashboard.thread = None
            dashboard.prepare_thread = None
            dashboard.prepare_worker = None
            dashboard._finish_prepare = lambda: None
            dashboard.set_refresh_token = lambda value: None
            dashboard._client_for = lambda given: object()
            dashboard._confirm_comparison = (
                lambda comp, given, client_factory=None, automatic=0: {
                    "uploads": [], "metadata": [], "read": [], "unread": [],
                    "cloud_deletions": [],
                })
            planned, started = [], []

            def plan(given, metadata, state, selection):
                planned.append(dict(given))
                return {"jobs": [], "removals": [], "removal_titles": {},
                        "meta_updates": [], "series_map": {},
                        "skipped_uploads": [], "selected_read": [],
                        "selected_unread": []}

            dashboard._plan_actions = plan
            dashboard._start_sync_worker = (
                lambda given, prep, state=None: started.append(dict(given)))
            dashboard._prepare_ready({
                "account_name": "konto", "settings": settings,
                "metadata": {}, "state": {}, "comparison": [],
                "refresh": "tok-rotated", "hardware": "hw-rotated",
            })
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual("tok-rotated", planned[0]["refresh_token"])
        self.assertEqual("tok-rotated", started[0]["refresh_token"])
        self.assertEqual("hw-rotated", started[0]["hardware_id"])

    def test_skipped_uploads_are_reported_before_the_run(self):
        ui_module = self._ui_module()
        try:
            warnings = []
            self._stub_warning(ui_module, warnings)
            self._install_fake_qt(ui_module)
            settings = self._settings()
            dashboard = self._dashboard(ui_module, settings)
            ranges, values, labels = [], [], []
            dashboard.progress = type("Bar", (), {
                "setRange": lambda self, *args: ranges.append(args),
                "setValue": lambda self, value: values.append(value),
            })()
            dashboard.progress_label = type("Label", (), {
                "setText": lambda self, text: labels.append(text),
            })()
            dashboard.refresh = type("Edit", (), {"text": lambda self: "t"})()
            dashboard.update_status = lambda: None
            dashboard.persist_refresh_token = lambda value: None
            for name in ("progress_changed", "sync_completed", "sync_failed"):
                setattr(dashboard, name, lambda *args: None)
            dashboard._start_sync_worker(settings, {
                "jobs": [], "removals": [], "meta_updates": [], "series_map": {},
                "skipped_uploads": ["Buch 1 (Titel), Format EPUB: fehlt"],
                "selected_read": set(), "selected_unread": set(),
            })
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(warnings))
        self.assertIn("nicht hochgeladen", warnings[0][1])
        self.assertIn("Buch 1 (Titel), Format EPUB: fehlt", warnings[0][2])
        self.assertIn("Buch 1", warnings[0][2])
        # Ohne weitere Aktion startet kein Lauf: die Meldung ist das Ende.
        # Mit übrigen Büchern läuft er normal weiter.
        self.assertIsNone(dashboard.thread)
        self.assertIsNone(getattr(dashboard, "worker", None))

class NoSilentDeadEndTests(unittest.TestCase):
    """Keine stille Sackgasse: jeder Ausgang des Vergleichs ist sichtbar.

    Feldbefund: «Synchronisierung starten» schloss den Bestandsvergleich,
    ohne dass etwas hochgeladen wurde -- und ohne jede Rückmeldung. Drei
    Antworten darauf werden hier festgehalten: der Vergleich schliesst
    ohne Auswahl gar nicht erst, ein leerer Plan startet keinen Lauf, und
    ein Fehler nach dem Fenster wird gemeldet.
    """

    # --- Bestandsvergleich: «Synchronisierung starten» -------------------

    def _ui_with_messages(self, messages):
        """Echter InventoryDialog auf dem ehrlichen Qt-Stub der Suite.

        `accept()` des Dialogs und alle Meldungsfenster landen in Recordern,
        damit sichtbar wird, ob das Fenster wirklich schliesst.
        """

        class BaseDialog(ui_base_tests.InventoryDialogBuildTests._qt_stub()["QDialog"]):
            closed = []

            def accept(self):
                BaseDialog.closed.append(True)

        class MessageBox:
            @staticmethod
            def information(*args, **_kwargs):
                messages.append(("information",) + args)

            @staticmethod
            def warning(*args, **_kwargs):
                messages.append(("warning",) + args)

            @staticmethod
            def question(*args, **_kwargs):
                messages.append(("question",) + args)
                return 0

        stub = ui_base_tests.InventoryDialogBuildTests._qt_stub()
        stub["QDialog"] = BaseDialog
        stub["QMessageBox"] = MessageBox
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub=stub)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _rows():
        return [{"title": "Neu", "authors": "A", "status": "new_in_calibre",
                 "book_id": 1, "tolino_id": "", "isbn": "111",
                 "explanation": "Noch nicht in der Cloud."}]

    def test_a_selected_upload_still_closes_the_dialog(self):
        messages = []
        ui_module = self._ui_with_messages(messages)
        dialog = ui_module.dialogs.InventoryDialog(self._rows(), options={})
        self.assertEqual(1, dialog.selected_work_count())
        dialog.accept()
        self.assertEqual([True], ui_module.common.QDialog.closed)
        self.assertEqual([], messages)

    def test_without_a_selection_the_dialog_stays_open_and_explains(self):
        messages = []
        ui_module = self._ui_with_messages(messages)
        dialog = ui_module.dialogs.InventoryDialog(self._rows(), options={})
        dialog.clear_all_selection()
        self.assertEqual(0, dialog.selected_work_count())
        dialog.accept()
        # Kein stilles Schliessen: das Fenster bleibt offen und sagt warum.
        self.assertEqual([], ui_module.common.QDialog.closed)
        self.assertEqual(1, len(messages))
        # QMessageBox.information(self, Titel, Text)
        self.assertEqual("Nichts ausgewählt", messages[0][2])
        self.assertIn("Wähle mindestens eine Aktion", messages[0][3])

    def test_activated_automatics_may_close_without_a_selection(self):
        messages = []
        ui_module = self._ui_with_messages(messages)
        dialog = ui_module.dialogs.InventoryDialog(self._rows(),
                                           options={"automatic": 1})
        dialog.clear_all_selection()
        dialog.accept()
        self.assertEqual([True], ui_module.common.QDialog.closed)
        self.assertEqual([], messages)

    # --- Dashboard: was nach dem Fenster passiert ------------------------

    def _ui_module(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _dashboard(ui_module, settings):
        """Nacktes Dashboard mit den Feldern, die der Sync-Schritt anfasst."""
        dashboard = ui_module.dashboard.SyncDashboard.__new__(ui_module.dashboard.SyncDashboard)
        dashboard.thread = None
        dashboard.values = lambda: settings
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = None
        dashboard.account_name = "konto"
        dashboard.prepare_thread = None
        dashboard.prepare_worker = None
        dashboard._finish_prepare = lambda: None
        dashboard.set_refresh_token = lambda value: None
        return dashboard

    def _planning_dashboard(self, ui_module, **extra):
        StartSyncOrchestrationTests._install_fake_qt(ui_module)
        dashboard = self._dashboard(ui_module,
                                   StartSyncOrchestrationTests._settings())
        dashboard.progress = type("Bar", (), {
            "setRange": lambda self, *args: None,
            "setValue": lambda self, value: None,
        })()
        dashboard.progress_label = type("Label", (), {
            "setText": lambda self, text: setattr(self, "text_value", text),
        })()
        dashboard.update_status = lambda: None
        for name, value in extra.items():
            setattr(dashboard, name, value)
        return dashboard

    def test_an_empty_plan_never_starts_a_silent_run(self):
        ui_module = self._ui_module()
        infos = []
        ui_module.dashboard.info_dialog = (
            lambda *args, **kwargs: infos.append(args))
        dashboard = self._planning_dashboard(ui_module)
        dashboard._start_sync_worker(dashboard.values(), _preparation())
        self.assertIsNone(dashboard.thread)
        self.assertIsNone(getattr(dashboard, "worker", None))
        self.assertEqual(1, len(infos))
        self.assertEqual("Nichts zu tun", infos[0][1])
        self.assertIn("nichts zu übertragen", infos[0][2].casefold())

    def test_an_empty_plan_removes_the_planned_cover_files(self):
        """Temp-Cover entstehen beim Planen; endet der Lauf vorher, dürfen
        sie nicht bis zum nächsten Start in /tmp liegen bleiben."""
        ui_module = self._ui_module()
        ui_module.dashboard.info_dialog = lambda *args, **kwargs: None
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"jpg")
            cover = tmp.name
        self.addCleanup(lambda: os.path.exists(cover) and os.unlink(cover))
        dashboard = self._planning_dashboard(ui_module)
        dashboard.temp_files = [cover]
        dashboard._start_sync_worker(dashboard.values(), _preparation())
        self.assertIsNone(dashboard.thread)
        self.assertFalse(os.path.exists(cover))
        self.assertEqual([], dashboard.temp_files)

    def test_a_planning_failure_removes_the_planned_cover_files(self):
        """Auch ein Fehler IN `_plan_actions` darf keine Cover-Datei behalten."""
        ui_module = self._ui_module()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"jpg")
            cover = tmp.name
        self.addCleanup(lambda: os.path.exists(cover) and os.unlink(cover))
        dashboard = self._planning_dashboard(ui_module)
        dashboard.temp_files = [cover]
        dashboard._automatic_action_count = lambda *args: 0
        dashboard._confirm_comparison = lambda *args, **kwargs: {
            "uploads": set(), "metadata": set(), "read": [], "unread": [],
            "cloud_deletions": [],
        }

        def fail(*args, **kwargs):
            raise RuntimeError("Planung kaputt")

        dashboard._plan_actions = fail
        errors = []
        dashboard._show_prepare_error = (
            lambda exc, settings: errors.append(str(exc)))
        dashboard._prepare_ready({
            "account_name": "konto", "settings": dashboard.values(),
            "metadata": {}, "state": {}, "comparison": [],
        })
        self.assertEqual(["Planung kaputt"], errors)
        self.assertFalse(os.path.exists(cover))
        self.assertEqual([], dashboard.temp_files)

    def test_the_sync_worker_starts_with_the_stored_token(self):
        """Zwischen Vergleichsfenster und Start kann der Keep-alive den
        Token rotiert haben. Der Worker muss den GESPEICHERTEN nehmen --
        sonst loest er einen verbrauchten Token ein (A02)."""
        ui_module = self._ui_module()
        settings = dict(StartSyncOrchestrationTests._settings(),
                        account_name="konto", refresh_token="alt-token",
                        hardware_id="hw-alt")
        dashboard = self._planning_dashboard(ui_module)
        with patch("calibre_plugin.config.account_by_name",
                   lambda name: {"name": "konto", "refresh_token": "neu-token",
                                 "hardware_id": "hw-neu"}):
            dashboard._start_sync_worker(
                settings, _preparation(jobs=["job-1"]))
        self.assertEqual(1, dashboard.thread.starts)
        passed = dashboard.worker.arguments[0]
        self.assertEqual("neu-token", passed["refresh_token"])
        self.assertEqual("hw-neu", passed["hardware_id"])
        self.assertEqual("neu-token", settings["refresh_token"])

    def test_the_plan_is_named_before_the_run_starts(self):
        ui_module = self._ui_module()
        dashboard = self._planning_dashboard(ui_module)
        dashboard._start_sync_worker(
            dashboard.values(),
            _preparation(jobs=["job-1", "job-2"]))
        self.assertEqual(1, dashboard.thread.starts)
        self.assertIn("2", dashboard.progress_label.text_value)
        self.assertIn("Upload", dashboard.progress_label.text_value)

    def test_the_accepting_window_really_produces_an_upload_job(self):
        """Ende zu Ende: Fenster bestaetigen -> Auswahl -> Plan -> Upload-Job.

        Genau diese Kette war der Feldbefund («Fenster schliesst, aber der
        Upload findet nicht statt»). Der Test faehrt sie mit dem echten
        Dialog, der echten Auswahl und dem echten Plan durch -- nur die
        Cloud und die Dateipfade sind ersetzt.
        """
        from ..comparison import compare_inventory
        ui_module = self._ui_with_messages([])
        ui_module.common.QDialog.Accepted = 1
        settings = dict(StartSyncOrchestrationTests._settings(),
                        account_name="konto")
        metadata = {
            1: {"uuid": "u1", "title": "Neu", "authors": "A", "isbn": "111",
                "formats": ["EPUB"], "last_modified": "1", "tolino_id": "",
                "series": ""},
            # Zweites Buch liegt schon in der Cloud: der Vergleich zeigt
            # beides zusammen, und nur das fehlende Buch wird geplant.
            2: {"uuid": "u2", "title": "Alt", "authors": "B", "isbn": "222",
                "formats": ["EPUB"], "last_modified": "2",
                "tolino_id": "t-2", "series": ""},
        }
        state = {"u2": {"tolino_id": "t-2", "calibre_id": 2}}
        inventory = [{"deliverableId": "t-2", "uuid": "u2", "title": "Alt",
                      "authors": "B", "isbn": "222"}]
        rows = compare_inventory(metadata, state, inventory, ["EPUB"])
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(b"EPUB")
            book_path = tmp.name
        self.addCleanup(os.unlink, book_path)
        ui_module.dashboard.safe_format_path = (
            lambda _db, _book_id, _fmt: book_path)
        dialog = ui_module.dialogs.InventoryDialog(rows, options={})
        dialog.exec = lambda: 1  # der Nutzer drueckt «Synchronisierung starten»
        ui_module.dashboard.InventoryDialog = lambda *args, **kwargs: dialog

        dashboard = self._dashboard(ui_module, settings)
        dashboard.temp_files = []
        self.assertEqual(2, len(rows))
        selection = dashboard._confirm_comparison(
            rows, settings, client_factory=lambda: None)
        # Nur das fehlende Buch: die vorhandene Cloud-Kopie bleibt unberuehrt.
        self.assertEqual({1}, set(selection["uploads"]))
        self.assertEqual(set(), set(selection["metadata"]))
        preparation = dashboard._plan_actions(settings, metadata, state, selection)
        self.assertEqual(1, len(preparation["jobs"]))
        self.assertEqual([], preparation["skipped_uploads"])
        self.assertEqual(1, ui_module.dashboard._dashboard_plan.planned_actions(
            preparation, 1, state))
        # Und der geplante Lauf nennt den Upload auch im Fenster.
        self.assertIn(
            "1 Upload",
            ui_module.dashboard._dashboard_plan.plan_text(
                preparation, 1, state))
        dashboard.progress = type("Bar", (), {
            "setRange": lambda self, *args: None,
            "setValue": lambda self, value: None,
        })()
        dashboard.progress_label = type("Label", (), {
            "setText": lambda self, text: setattr(self, "text_value", text),
        })()
        dashboard.update_status = lambda: None
        StartSyncOrchestrationTests._install_fake_qt(ui_module)
        dashboard._start_sync_worker(settings, preparation)
        self.assertEqual(1, dashboard.thread.starts)
        self.assertIn("Läuft: 1 Upload",
                      dashboard.progress_label.text_value)
        self.assertEqual(1, len(dashboard.worker.arguments[1]))

    def test_a_crashing_comparison_window_is_reported(self):
        ui_module = self._ui_module()
        errors = []
        dashboard = self._dashboard(ui_module,
                                    StartSyncOrchestrationTests._settings())
        dashboard._show_prepare_error = lambda exc, given: errors.append(exc)
        dashboard._client_for = lambda given: object()

        def boom(*_args, **_kwargs):
            raise RuntimeError("Fenster kaputt")

        dashboard._confirm_comparison = boom
        planned, started = [], []
        dashboard._plan_actions = lambda *args: planned.append(args)
        dashboard._start_sync_worker = lambda *args: started.append(args)
        dashboard._prepare_ready({
            "account_name": "konto", "settings": dashboard.values(),
            "metadata": {}, "state": {}, "comparison": [],
            "refresh": "tok", "hardware": "hw",
        })
        self.assertEqual(1, len(errors))
        self.assertIn("Fenster kaputt", str(errors[0]))
        self.assertEqual([], planned)
        self.assertEqual([], started)

    def test_a_failed_bookkeeping_still_reports_the_result(self):
        ui_module = self._ui_module()
        boxes = []
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "A"
        dashboard.refresh = type("F", (), {"text": lambda self: "tok",
                                           "setText": lambda self, v: None})()
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = False
        dashboard.finish_thread = lambda: None
        worker = type("Worker", (), {})()
        worker.account_name = "A"
        worker.account = {"hardware_id": "hw"}
        worker.state = {"u1": {"tolino_id": "new-1"}}
        worker.updates = [(1, "new-1")]
        worker.outcome = {"uploaded": 1, "updated": 0, "downloaded": 0,
                          "skipped": 0, "failed": 0, "not_started": 0}
        dashboard.worker = worker

        def boom(*_args, **_kwargs):
            raise OSError("Platte voll")

        with patch("calibre_plugin.ui_dashboard.save_credentials", boom), \
             patch("calibre_plugin.ui_dashboard.save_account",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.ui_dashboard.update_tolino_ids",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.ui_dashboard.info_dialog",
                   lambda *args, **kwargs: boxes.append(args)):
            ui_module.dashboard.SyncDashboard.sync_completed(
                dashboard, {"u1": {"tolino_id": "new-1"}}, "rot-A", [(1, "new-1")])
        # Der Abschlussdialog kommt trotz Speicherfehler: der Upload ist
        # gelaufen und darf nicht als «nichts passiert» erscheinen.
        self.assertEqual(1, len(boxes))
        self.assertIn("1 hochgeladen", boxes[0][2])

class CloudCopyAssignmentPlanTests(unittest.TestCase):
    """Die Zuordnung landet im Plan und im Zustand."""

    @staticmethod
    def _metadata():
        return {1: {"uuid": "u1", "title": "Broken Homes",
                    "authors": "Ben Aaronovitch", "isbn": "",
                    "formats": ["EPUB"], "last_modified": "1",
                    "series": "", "tolino_id": ""}}

    @staticmethod
    def _settings():
        return {"refresh_token": "t", "partner_id": 4, "hardware_id": "hw", "state": {},
                "preferred_formats": ["EPUB"],
                "account_name": "konto"}

    @staticmethod
    def _selection(**overrides):
        selection = {"uploads": set(), "metadata": set(), "read": [],
                     "unread": [], "cloud_deletions": []}
        selection.update(overrides)
        return selection

    def _ui(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    def _dashboard(self, ui_module, settings):
        StartSyncOrchestrationTests._install_fake_qt(ui_module)
        dashboard = ui_module.dashboard.SyncDashboard.__new__(
            ui_module.dashboard.SyncDashboard)
        dashboard.thread = None
        dashboard.values = lambda: settings
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = None
        dashboard.account_name = "konto"
        dashboard.temp_files = []
        dashboard.prepare_thread = None
        dashboard.prepare_worker = None
        dashboard.update_status = lambda: None
        dashboard.progress = type("Bar", (), {
            "setRange": lambda self, *args: None,
            "setValue": lambda self, value: None,
        })()
        dashboard.progress_label = type("Label", (), {
            "setText": lambda self, text: setattr(self, "text_value", text),
        })()
        return dashboard

    def test_assignment_plans_a_metadata_update_without_upload(self):
        ui_module = self._ui()
        settings = self._settings()
        dashboard = self._dashboard(ui_module, settings)
        state = {"u1": {"tolino_id": "t-9", "calibre_id": 1}}
        preparation = dashboard._plan_actions(
            settings, self._metadata(), state,
            self._selection(metadata={1}))
        self.assertEqual([], preparation["jobs"])
        self.assertEqual(1, len(preparation["meta_updates"]))
        self.assertEqual("t-9", preparation["meta_updates"][0][2])
        self.assertEqual("t-9", settings["state"]["u1"]["tolino_id"])
        self.assertEqual("Broken Homes", preparation["titles"]["u1"])

class ReplacementConfirmationTests(unittest.TestCase):
    """Ein Upload mit alter Kopie loescht sie -- das wird vorher gefragt.

    Der Tooltip am Haekchen sagte es bisher nur; die Bestaetigung schwieg.
    Standardantwort ist «Nein», und ein «Nein» startet nichts.
    """

    def _ui(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module

    @staticmethod
    def _job(ui_module, book_id, title, old_id):
        return ui_module.common.SyncJob(book_id, "u%d" % book_id, "EPUB",
                                        old_id, "/library/b.epub", None, title)

    def _run(self, ui_module, answer, jobs, temp_files=()):
        settings = CloudCopyAssignmentPlanTests._settings()
        dashboard = CloudCopyAssignmentPlanTests._dashboard(
            self, ui_module, settings)
        dashboard.temp_files = list(temp_files)
        dashboard.status = type("Label", (), {
            "text_value": "",
            "setText": lambda self, text: setattr(self, "text_value", text),
        })()
        asked = []

        class Box:
            Yes = 1
            No = 0

            @staticmethod
            def question(*args):
                asked.append(args)
                return answer

        ui_module.dashboard.QMessageBox = Box
        dashboard._start_sync_worker(
            settings, _preparation(jobs=jobs))
        return dashboard, asked

    def test_no_means_no_run_and_no_temp_files(self):
        ui_module = self._ui()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            tmp.write(b"jpg")
            cover = tmp.name
        self.addCleanup(lambda: os.path.exists(cover) and os.unlink(cover))
        dashboard, asked = self._run(
            ui_module, 0,
            [self._job(ui_module, 1, "Der Schwarm", "t-0")], [cover])
        self.assertEqual(1, len(asked))
        # Titel statt Kennung, und die Frage nennt das Loeschen der Kopie.
        self.assertIn("Der Schwarm", asked[0][2])
        self.assertNotIn("u1", asked[0][2])
        self.assertIn("gehen verloren", asked[0][2])
        self.assertIsNone(dashboard.thread)
        self.assertFalse(os.path.exists(cover))
        self.assertEqual([], dashboard.temp_files)
        self.assertIn("unverändert", dashboard.status.text_value)

    def test_yes_starts_the_run(self):
        ui_module = self._ui()
        dashboard, asked = self._run(
            ui_module, 1, [self._job(ui_module, 1, "Der Schwarm", "t-0")])
        self.assertEqual(1, len(asked))
        self.assertEqual(1, dashboard.thread.starts)

    def test_a_first_upload_asks_nothing(self):
        ui_module = self._ui()
        dashboard, asked = self._run(
            ui_module, 0, [self._job(ui_module, 1, "Der Schwarm", None)])
        self.assertEqual([], asked)
        self.assertEqual(1, dashboard.thread.starts)

    def test_more_than_twenty_replacements_are_summarised(self):
        ui_module = self._ui()
        jobs = [self._job(ui_module, index, "Buch %d" % index, "t-%d" % index)
                for index in range(1, 23)]
        _dashboard, asked = self._run(ui_module, 0, jobs)
        self.assertEqual(1, len(asked))
        self.assertIn("22 Bücher", asked[0][2])
        self.assertIn("… und 2 weitere", asked[0][2])

class DroppedSelectionCountTests(unittest.TestCase):
    """Der Hinweis zählt Auswahlen, die sich nicht planen ließen."""

    def test_a_missing_preferred_format_is_reported_and_not_scheduled(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        dashboard = CloudCopyAssignmentPlanTests._dashboard(
            self, ui_module, CloudCopyAssignmentPlanTests._settings())
        settings = CloudCopyAssignmentPlanTests._settings()
        metadata = {1: {"uuid": "u1", "title": "PDF only", "formats": ["PDF"]}}
        selection = {"uploads": {1}, "metadata": set(), "read": [], "unread": [],
                     "cloud_deletions": []}
        preparation = dashboard._plan_actions(settings, metadata, {}, selection)
        self.assertEqual([], preparation["jobs"])
        self.assertEqual(1, len(preparation["skipped_uploads"]))
        self.assertIn("PDF only", preparation["skipped_uploads"][0])
        self.assertIn("EPUB", preparation["skipped_uploads"][0])
        self.assertNotIn("u1", settings["state"])

class VisibleResultTests(unittest.TestCase):
    """Calibre zeigt ein info_dialog nur mit show=True – sonst nie.

    ``calibre.gui2.info_dialog(parent, title, msg, det_msg='', show=False,
    ...)`` baut das Fenster und gibt es ungesehen zurück. Jeder Aufruf ohne
    ``show=True`` ist damit eine stille Sackgasse: der Lauf endet, und der
    Nutzer sieht weder Ergebnis noch Hinweis.
    """

    @staticmethod
    def _calls(text):
        """Alle info_dialog-Aufrufe als Quelltext-Stück."""
        calls = []
        start = 0
        while True:
            index = text.find("info_dialog(", start)
            if index < 0:
                return calls
            depth = 0
            end = index
            for offset in range(index, len(text)):
                character = text[offset]
                if character == "(":
                    depth += 1
                elif character == ")":
                    depth -= 1
                    if depth == 0:
                        end = offset + 1
                        break
            calls.append(text[index:end])
            start = end

    def test_the_result_dialog_is_shown_after_a_sync(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        seen = []
        ui_module.dashboard.info_dialog = (
            lambda *args, **kwargs: seen.append(kwargs))
        dashboard = NoSilentDeadEndTests._dashboard(
            ui_module, StartSyncOrchestrationTests._settings())
        dashboard.worker = type("Worker", (), {
            "outcome": {"uploaded": 1, "updated": 0, "downloaded": 0,
                        "skipped": 0, "failed": 0, "cancelled": False,
                        "operations": []},
            "account": {}, "account_name": "konto"})()
        dashboard.finish_thread = lambda: None
        dashboard.sync_completed({}, None, [])
        self.assertEqual(1, len(seen))
        self.assertIs(True, seen[0].get("show"))

    def test_nothing_to_do_is_shown(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        # Installiert QThread/SyncWorker als Recorder (Nebenwirkung).
        StartSyncOrchestrationTests._install_fake_qt(ui_module)
        seen = []
        ui_module.dashboard.info_dialog = (
            lambda *args, **kwargs: seen.append(kwargs))
        dashboard = NoSilentDeadEndTests._dashboard(
            ui_module, StartSyncOrchestrationTests._settings())
        dashboard.start_sync = lambda: None
        dashboard._start_sync_worker(
            StartSyncOrchestrationTests._settings(),
            _preparation())
        self.assertEqual(1, len(seen))
        self.assertIs(True, seen[0].get("show"))

class EmptyRunAccountingTests(unittest.TestCase):
    """Ein Lauf darf nur starten, wenn er wirklich etwas tut.

    Feldbefund: «Abgleich abgeschlossen: 0 hochgeladen, 0
    aktualisiert …», obwohl 5 Uploads und 400 Metadaten ausgewählt waren.
    Ursache: `_planned_actions` zählte den ROHEN Serien-Zustand
    (`series_map` = jedes Buch mit Serie) als auszuführende Aktion. Der
    Lauf startete damit auch dann, wenn alle Serien längst geschrieben und
    Uploads/Metadaten nicht geplant waren – und der Worker meldete
    wahrheitsgemäss 0/0/0.
    """

    def _ui(self):
        ui_module = StartSyncOrchestrationTests._ui_module()
        self.addCleanup(ui_module._restore_stubs_for_tests)
        return ui_module


    def test_remembered_collections_are_not_a_planned_action(self):
        ui_module = self._ui()
        preparation = _preparation(series_map={"u1": "Reihe", "u2": "Reihe"})
        state = {"u1": {"tolino_id": "t1", "collections": ["Reihe"]},
                 "u2": {"tolino_id": "t2", "collections": ["Reihe"]}}
        self.assertEqual(0, ui_module.dashboard._dashboard_plan.planned_actions(
            preparation, 0, state))

    def test_open_collections_still_count(self):
        ui_module = self._ui()
        preparation = _preparation(series_map={"u1": "Reihe"})
        state = {"u1": {"tolino_id": "t1"}}
        self.assertEqual(1, ui_module.dashboard._dashboard_plan.planned_actions(
            preparation, 0, state))

    def test_plan_text_does_not_promise_remembered_collections(self):
        ui_module = self._ui()
        preparation = _preparation(series_map={"u1": "Reihe"})
        state = {"u1": {"tolino_id": "t1", "collections": ["Reihe"]}}
        text = ui_module.dashboard._dashboard_plan.plan_text(preparation, 0, state)
        self.assertNotIn("Serie", text)

    def test_selected_actions_without_a_plan_are_counted(self):
        ui_module = self._ui()
        dashboard = NoSilentDeadEndTests._dashboard(
            ui_module, StartSyncOrchestrationTests._settings())
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        # Ohne UUID kann plan_sync das Buch nicht einplanen.
        metadata = {1: {"uuid": "", "title": "Kaputt", "formats": ["EPUB"]}}
        selection = {"uploads": {1}, "metadata": {1}, "read": [], "unread": [],
                     "cloud_deletions": []}
        preparation = dashboard._plan_actions(
            StartSyncOrchestrationTests._settings(), metadata, {}, selection)
        self.assertEqual([], preparation["jobs"])
        self.assertEqual([], preparation["meta_updates"])
        self.assertEqual(
            {"uploads": 1, "metadata": 1},
            {key: value for key, value in
             preparation["selected_without_action"].items()
             if key in ("uploads", "metadata")})

    def test_the_nothing_to_do_text_reports_unplanned_actions(self):
        """Ohne Plan wird nur die Anzahl der ausgefallenen Aktionen genannt."""
        ui_module = self._ui()
        seen = []
        ui_module.dashboard.info_dialog = (
            lambda *args, **kwargs: seen.append(args))
        dashboard = NoSilentDeadEndTests._dashboard(
            ui_module, StartSyncOrchestrationTests._settings())
        preparation = _preparation(
            selected_without_action={"uploads": 5, "metadata": 400})
        dashboard._start_sync_worker(
            StartSyncOrchestrationTests._settings(), preparation, {})
        self.assertEqual(1, len(seen))
        text = " ".join(str(part) for part in seen[0])
        self.assertIn("405 ausgewählte Aktionen konnten nicht eingeplant werden.", text)

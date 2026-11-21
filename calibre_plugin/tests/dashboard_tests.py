"""Was nach einem Sync-Lauf gesichert und dem Nutzer gemeldet wird.

Der Lauf ist vorbei (Client und Thread), geprüft werden Ergebnis-Sicherung,
Konto-Zuordnung der Spalten und die Rückmeldung je Ausgang. Worker und
Dashboard sind echte Objekte bzw. das vorhandene Dashboard-Doppel.
"""
from contextlib import contextmanager
import types
import unittest
import uuid
from unittest.mock import patch

from .. import config
from ..comparison import compare_inventory
from ..library import (TOLINO_COLUMN, TOLINO_PROGRESS_COLUMN,
                      TOLINO_READ_COLUMN, library_scope)
from ..run_context import RunContext
from ._support import (CalibreDatabase, SyncThread, break_settings,
                       dashboard_stub, real_qt_stub, ui_module_for)

UUID = "11111111-1111-4111-8111-111111111111"
OTHER = "22222222-2222-4222-8222-222222222222"


class OrchestrationCase(unittest.TestCase):
    """Frischer Einstellungsspeicher und geladenes UI-Modul je Test."""

    def setUp(self):
        self._original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)
        self.addCleanup(setattr, config, "PREFERENCES", self._original)
        # Ein Sync-Lauf setzt gespeicherte Konten voraus; nur so kann der
        # Zustand am Ende auch geschrieben werden.
        for name in ("A", "B"):
            config.save_account(name, {"refresh_token": "tok-%s" % name})
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def worker(self, account="A", outcome=None, updates=((1, "neu-1"),)):
        """Echter SyncWorker nach dem Lauf: nur sein Ergebnis zählt."""
        context = RunContext.from_values(
            {"account_name": account, "preferred_formats": ["EPUB"]},
            columns={"id": True, "read": True, "progress": True})
        worker = self.ui.workers.SyncWorker(context, [], [], state={})
        worker.account_name = account
        worker.outcome = dict(outcome if outcome is not None
                              else {"uploaded": 1})
        worker.updates = list(updates)
        worker.read_updates = [(1, True)]
        worker.progress_updates = [(1, 42)]
        return worker

    def dashboard(self, worker, database, account="A", visible=None):
        dashboard = dashboard_stub()
        dashboard.account_name = account
        dashboard.worker = worker
        dashboard.gui = types.SimpleNamespace(current_db=database)
        dashboard.finished = []
        dashboard.finish_thread = lambda: dashboard.finished.append(True)
        if visible is not None:
            dashboard.isVisible = lambda: visible
        return dashboard

    def stored_ids(self, account="A", tolino_id="neu-1"):
        """Die Konto-Zuordnung eintragen, die der Lauf gesichert hat."""
        config.save_account(account, {"state": {
            UUID: {"tolino_id": tolino_id}}})

    def method(self, name):
        """Echte Dashboard-Methode an ein Doppel binden."""
        return self.ui.dashboard.SyncDashboard.__dict__[name].__get__

    def sync_completed(self, dashboard, updates):
        method = self.ui.dashboard.SyncDashboard.sync_completed
        return method.__get__(dashboard)(updates)

    def sync_failed(self, dashboard, message):
        method = self.ui.dashboard.SyncDashboard.sync_failed
        return method.__get__(dashboard)(message)


def full_database(fail=()):
    database = CalibreDatabase(
        {TOLINO_COLUMN: {"datatype": "text"},
         TOLINO_READ_COLUMN: {"datatype": "bool"},
         TOLINO_PROGRESS_COLUMN: {"datatype": "int"}},
        fail=set(fail), books=[1])
    database.api.values[("uuid", 1)] = UUID
    return database


class PersistTests(OrchestrationCase):
    """Calibre-Spalten schreiben nur unter dem Konto des Laufs."""

    def test_reading_columns_only_for_the_displayed_account(self):
        worker = self.worker(account="B")
        self.stored_ids("B")
        database = full_database()
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database, account="A"), worker.updates)
        self.assertEqual([], failures)
        self.assertEqual([TOLINO_COLUMN],
                         [column for column, _values in database.api.writes])

        database = full_database()
        worker = self.worker(account="A")
        self.stored_ids("A")
        self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), worker.updates)
        self.assertEqual([TOLINO_COLUMN, TOLINO_READ_COLUMN,
                          TOLINO_PROGRESS_COLUMN],
                         [column for column, _values in database.api.writes])

    def test_a_failing_column_is_named(self):
        worker = self.worker()
        self.stored_ids()
        dashboard = self.dashboard(worker, full_database(fail=[TOLINO_COLUMN]))
        failures = self.ui.sync_results.persist_sync_result(
            dashboard, worker.updates)
        self.assertEqual(["tolino-IDs"], failures)

    def test_the_cleanup_callback_runs_even_when_a_column_fails(self):
        """Geprueft wird `finish_sync`; das Thread-Ende deckt ui_thread_tests ab."""
        worker = self.worker()
        self.stored_ids()
        dashboard = self.dashboard(worker, full_database(fail=[TOLINO_COLUMN]))
        outcome, failures = self.ui.sync_results.finish_sync(
            dashboard, worker.updates)
        self.assertEqual({"uploaded": 1}, outcome)
        self.assertEqual(["tolino-IDs"], failures)
        self.assertTrue(dashboard.finished)

    def test_a_late_result_keeps_the_finished_worker(self):
        """closeEvent leert `worker`; das Ergebnis des Laufs zaehlt trotzdem."""
        worker = self.worker()
        self.stored_ids()
        dashboard = self.dashboard(worker, full_database())
        dashboard.worker = None
        outcome, failures = self.ui.sync_results.finish_sync(
            dashboard, worker.updates, worker)
        self.assertEqual({"uploaded": 1}, outcome)
        self.assertEqual([], failures)
        self.assertTrue(dashboard.finished)

    def test_a_book_without_a_stored_id_clears_its_column(self):
        """Nach dem Loeschen steht im Konto keine Zuordnung mehr."""
        worker = self.worker(updates=((1, ""),))
        database = full_database()
        database.api.values[(TOLINO_COLUMN, 1)] = '{"A": "alt"}'
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), worker.updates)
        self.assertEqual([], failures)
        self.assertEqual((TOLINO_COLUMN, {1: ""}), database.api.writes[0])

    def test_an_unknown_column_is_simply_skipped(self):
        worker = self.worker()
        self.stored_ids()
        database = CalibreDatabase({TOLINO_COLUMN: {"datatype": "text"}},
                                   books=[1])
        database.api.values[("uuid", 1)] = UUID
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), worker.updates)
        self.assertEqual([], failures)
        self.assertEqual([TOLINO_COLUMN],
                         [column for column, _values in database.api.writes])

    def test_a_changed_library_is_not_written_and_the_abort_is_reported(self):
        worker = self.worker()
        worker.context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]},
            columns={"id": True, "read": True, "progress": True},
            library_scope="fremde-bibliothek")
        database = full_database()
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), worker.updates)
        self.assertEqual(["Bibliothek gewechselt – Lauf abgebrochen"], failures)
        self.assertEqual([], database.api.writes)

    def test_the_library_of_the_start_snapshot_is_written(self):
        worker = self.worker()
        self.stored_ids()
        database = full_database()
        worker.context = RunContext.from_values(
            {"account_name": "A", "preferred_formats": ["EPUB"]},
            columns={"id": True, "read": True, "progress": True},
            library_scope=library_scope(database))
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), worker.updates)
        self.assertEqual([], failures)
        self.assertEqual([TOLINO_COLUMN, TOLINO_READ_COLUMN,
                          TOLINO_PROGRESS_COLUMN],
                         [column for column, _values in database.api.writes])


class MirrorTests(OrchestrationCase):
    """Kontowechsel zeigt genau ein Konto; unklare Werte leeren nichts."""

    def reading_database(self):
        database = CalibreDatabase(
            {TOLINO_READ_COLUMN: {"datatype": "bool"},
             TOLINO_PROGRESS_COLUMN: {"datatype": "int"}}, books=[1, 2])
        database.api.values[("uuid", 1)] = UUID
        database.api.values[("uuid", 2)] = OTHER
        return database

    def test_an_account_switch_clears_the_progress_of_the_previous_one(self):
        config.save_account("A", {"refresh_token": "tok-A", "state": {
            UUID: {"tolino_id": "d1",
                   "reading": {"progress": {"percent": 42}}}}})
        config.save_account("B", {"refresh_token": "tok-B", "state": {}})
        database = self.reading_database()
        dashboard = self.dashboard(self.worker(), database)

        self.ui.sync_results.mirror_account_reading(dashboard, "A")
        self.assertEqual(42,
                         database.api.values[(TOLINO_PROGRESS_COLUMN, 1)])
        self.ui.sync_results.mirror_account_reading(dashboard, "B")
        self.assertIsNone(
            database.api.values[(TOLINO_PROGRESS_COLUMN, 1)])

    def test_an_unknown_comparison_progress_keeps_the_column(self):
        database = self.reading_database()
        database.api.values[(TOLINO_PROGRESS_COLUMN, 1)] = 42
        dashboard = self.dashboard(self.worker(), database)
        rows = compare_inventory(
            {1: {"uuid": UUID, "title": "T", "authors": "A",
                 "isbn": "", "formats": ["EPUB"]}},
            {UUID: {"tolino_id": "d1"}},
            [{"tolino_id": "d1", "title": "T", "authors": "A",
              "isbn": ""}], ["EPUB"])
        self.ui.sync_results.mirror_comparison_reading(dashboard, rows)
        self.assertEqual(42,
                         database.api.values[(TOLINO_PROGRESS_COLUMN, 1)])


class ReportTests(OrchestrationCase):
    """Jeder Ausgang hat seine eigene Rückmeldung; geschlossen bleibt still."""

    @contextmanager
    def recorded(self):
        """Beide Dialoge mitschneiden, statt sie anzuzeigen."""
        calls = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: calls["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: calls["error"].append(args)):
            yield calls

    def test_a_successful_run_reports_its_result(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database())
        with self.recorded() as recorded:
            self.sync_completed(dashboard, worker.updates)
        self.assertEqual("Fertig", recorded["info"][0][1])
        self.assertIn("hochgeladen", recorded["info"][0][2])
        self.assertEqual([], recorded["error"])
        self.assertTrue(dashboard.finished)

    def test_a_cancelled_run_reports_a_cancellation(self):
        worker = self.worker(outcome={"cancelled": True})
        dashboard = self.dashboard(worker, full_database())
        with self.recorded() as recorded:
            self.sync_completed(dashboard, worker.updates)
        self.assertIn("Abgebrochen", recorded["info"][0][2])
        self.assertEqual([], recorded["error"])

    def test_a_partial_failure_gets_its_own_heading(self):
        worker = self.worker(outcome={"uploaded": 1, "failed": 1})
        dashboard = self.dashboard(worker, full_database())
        with self.recorded() as recorded:
            self.sync_completed(dashboard, worker.updates)
        self.assertEqual("Teilweise fehlgeschlagen", recorded["error"][0][1])
        self.assertIn("Teilweise abgeschlossen", recorded["error"][0][2])

    def test_an_error_message_wins_over_a_cancel_flag(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database())
        with self.recorded() as recorded:
            self.ui.sync_results.report_sync(
                dashboard, {"failed": 1, "cancelled": True}, [],
                "Netz weg", True)
        self.assertEqual("Fehlgeschlagen", recorded["error"][0][1])
        self.assertEqual([], recorded["info"])

    def test_a_cancel_without_a_message_stays_a_cancel(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database())
        with self.recorded() as recorded:
            self.ui.sync_results.report_sync(
                dashboard, {}, [], self.ui.common.CANCEL_MESSAGE, True)
        self.assertIn("Abgebrochen", recorded["info"][0][1])
        self.assertEqual([], recorded["error"])

    def test_a_closed_window_gets_no_dialog(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database(), visible=False)
        with self.recorded() as recorded:
            self.sync_completed(dashboard, worker.updates)
        self.assertEqual(([], []), (recorded["info"], recorded["error"]))
        self.assertTrue(dashboard.finished)

    def test_a_failed_run_keeps_the_ids_of_completed_uploads(self):
        worker = self.worker(outcome={"failed": 1})
        self.stored_ids()
        database = full_database()
        dashboard = self.dashboard(worker, database)
        with self.recorded() as recorded:
            self.sync_failed(dashboard, "Die Verbindung ist fehlgeschlagen.")
        self.assertIn(TOLINO_COLUMN,
                      [column for column, _values in database.api.writes])
        self.assertIn("Die Verbindung ist fehlgeschlagen",
                      recorded["error"][0][2])
        self.assertTrue(dashboard.finished)


class FakeField:
    """Ein minimales Eingabefeld: merkt sich, ob es geleert wurde."""

    def __init__(self, value):
        self.value = value

    def text(self):
        return self.value

    def setText(self, value):
        self.value = value


class SettingsSaveTests(OrchestrationCase):
    """Ein nicht gespeicherter Stand meldet keinen Erfolg."""

    def test_a_token_that_cannot_be_saved_stays_in_the_field(self):
        config.save_account("A", {"refresh_token": ""})
        field = FakeField("tok-neu")
        dashboard = types.SimpleNamespace(
            account_name="A", refresh=field,
            partner=types.SimpleNamespace(currentData=lambda: 1),
            _partner_chosen=True, _hardware_edited=False,
            hardware=types.SimpleNamespace(text=lambda: "hw"),
            update_status=lambda: None)
        break_settings(self)
        store = self.ui.dashboard.SyncDashboard.store_refresh.__get__(dashboard)
        with patch("calibre_plugin.ui_dashboard.error_dialog") as shown:
            self.assertFalse(store("tok-neu"))
        self.assertTrue(shown.called)
        self.assertEqual("tok-neu", field.value)

    def test_a_failed_settings_save_does_not_switch_the_account(self):
        config.save_account("A", {"refresh_token": "tok-A"}, active="A")
        config.save_account("B", {"refresh_token": "tok-B"})
        combo = types.SimpleNamespace(
            signals=[], index=1,
            blockSignals=lambda value: combo.signals.append(value),
            findData=lambda value: 0 if value == "A" else -1,
            setCurrentIndex=lambda index: setattr(combo, "index", index))
        loaded = []
        dashboard = self.dashboard_for_save(
            account_name="A", account_names=["A", "B"], login_thread=None,
            account_select=combo, _hardware_edited=False,
            values=lambda: {"account_name": "A", "hardware_id": "hw",
                            config.PREFERRED_FORMATS: ["EPUB"]},
            load_values=lambda: loaded.append(True))
        # Der echte Speicherpfad scheitert; nur `_save_visible_account`
        # bleibt hier echt, damit Wertaufbereitung und Commit geprueft sind.
        dashboard._save_visible_account = self.method(
            "_save_visible_account")(dashboard)
        break_settings(self)
        with patch.object(self.ui.dashboard, "error_dialog"):
            self.assertFalse(dashboard.switch_account("B"))
        self.assertEqual("A", dashboard.account_name)
        # Die Auswahl geht zurueck, ohne die Felder neu zu laden.
        self.assertEqual(([True, False], 0), (combo.signals, combo.index))
        self.assertEqual([], loaded)
        self.assertEqual("A", config.settings()["active_account"])

    def dashboard_for_save(self, **attrs):
        """Doppel mit den echten Speicher-Methoden; `_save_visible_account`
        bleibt stubbar (die echten Pfade decken die Tests oben ab)."""
        dashboard = types.SimpleNamespace(**attrs)
        dashboard._save_visible_account = lambda: attrs.get("saved", True)
        for name in ("_save_failed", "_save_settings", "close_settings",
                     "new_account", "switch_account", "_partner_selected",
                     "_restore_account_choice", "logout"):
            setattr(dashboard, name, self.method(name)(dashboard))
        return dashboard

    def test_a_failed_save_keeps_the_settings_window_open(self):
        accepted = []
        dashboard = self.dashboard_for_save(
            account_name="A", saved=False, _typed_refresh=lambda: "",
            settings_dialog=types.SimpleNamespace(
                accept=lambda: accepted.append(True)))
        with patch.object(self.ui.dashboard, "error_dialog") as shown:
            dashboard.close_settings()
        self.assertTrue(shown.called)
        self.assertEqual([], accepted)

    def test_a_new_account_is_not_created_when_the_save_fails(self):
        loaded = []
        dashboard = self.dashboard_for_save(
            account_name="A", account_names=["A"], saved=True,
            load_values=lambda: loaded.append(True))
        break_settings(self)
        with patch.object(self.ui.dashboard.QInputDialog, "getText",
                          lambda *_a: ("Neu", True)), \
                patch.object(self.ui.dashboard, "error_dialog") as shown:
            dashboard.new_account()
        self.assertTrue(shown.called)
        self.assertEqual([], loaded)

    def test_a_partner_choice_does_not_touch_the_stored_login(self):
        config.save_account("A", {"refresh_token": "tok-A", "partner_id": 1})
        dashboard = self.dashboard_for_save(
            account_name="A", _partner_chosen=False,
            partner=types.SimpleNamespace(currentData=lambda: 2))
        dashboard._partner_selected(1)
        self.assertTrue(dashboard._partner_chosen)
        # Gespeichert wird die Auswahl erst mit einem Ersatz-Token; der
        # vorhandene Thalia-Zugang bleibt unangetastet.
        self.assertEqual(1, config.account_summary("A")["partner_id"])
        self.assertEqual("tok-A", config.account_summary("A")["refresh_token"])

    def test_a_switch_that_cannot_be_stored_reloads_the_stored_account(self):
        config.save_account("A", {"refresh_token": "tok-A"}, active="A")
        loaded = []
        dashboard = self.dashboard_for_save(
            account_name="A", account_names=["A", "Weg"], saved=True,
            _hardware_edited=False, login_thread=None,
            values=lambda: {"account_name": "A", "hardware_id": "hw",
                            config.PREFERRED_FORMATS: ["EPUB"]},
            load_values=lambda: loaded.append(True))
        # «Weg» steht nur in der Liste der Oberflaeche: die Auswahl scheitert.
        with patch.object(self.ui.dashboard, "error_dialog") as shown:
            self.assertFalse(dashboard.switch_account("Weg"))
        self.assertTrue(shown.called)
        self.assertEqual([True], loaded)

    def test_a_logout_that_cannot_be_stored_keeps_the_token(self):
        shown = []
        dashboard = self.dashboard_for_save(
            account_name="A", refresh=FakeField("tok-A"),
            update_status=lambda: shown.append("status"))
        break_settings(self)
        with patch.object(self.ui.dashboard, "error_dialog") as reported:
            dashboard.logout()
        self.assertTrue(reported.called)
        # Nicht gesichert heisst: der Token gilt weiter, nichts meldet Erfolg.
        self.assertEqual("tok-A", dashboard.refresh.value)
        self.assertEqual([], shown)


class LoginFlowTests(OrchestrationCase):
    """Der Anmeldeknopf prüft zuerst und öffnet den Browser nur bei Bedarf."""

    def dashboard(self, stored):
        opened, checked = [], []
        dashboard = types.SimpleNamespace(
            account_name="A", login_thread=None, refresh=FakeField(""),
            hardware=FakeField("hw"), formats=FakeField("EPUB"),
            status=FakeField(""),
            _selected_account=lambda: {"name": "A", "refresh_token": stored},
            _check_stored_login=checked.append,
            _start_login_window=opened.append)
        for name in ("browser_login", "_login_running", "_stored_refresh",
                     "_typed_refresh", "_effective_refresh", "values",
                     "login_still_valid", "login_rejected", "_login_cleanup"):
            setattr(dashboard, name, self.method(name)(dashboard))
        dashboard.update_status = lambda: None
        return dashboard, opened, checked

    def test_without_a_stored_token_the_browser_window_opens(self):
        config.save_account("A", {"refresh_token": ""})
        dashboard, opened, checked = self.dashboard("")
        dashboard.browser_login()
        self.assertEqual(1, len(opened))
        self.assertEqual([], checked)

    def test_a_stored_token_is_checked_before_the_browser_opens(self):
        dashboard, opened, checked = self.dashboard("tok-A")
        dashboard.browser_login()
        self.assertEqual([], opened)
        self.assertEqual(1, len(checked))

    def test_a_rejected_check_opens_the_browser(self):
        dashboard, opened, _checked = self.dashboard("tok-A")
        dashboard.login_rejected("abgelehnt")
        self.assertEqual(1, len(opened))
        self.assertIn("neu anmelden", dashboard.status.value)


class ChainCase(OrchestrationCase):
    """Gemeinsamer Aufbau der Kettentests -- selbst kein Testfall."""

    def dashboard(self, account="A"):
        database = full_database()
        dashboard = dashboard_stub(account_name=account,
                                   hardware=FakeField("hw"),
                                   refresh=FakeField(""))
        dashboard.gui = types.SimpleNamespace(current_db=database)
        dashboard.status = FakeField("")
        dashboard.progress = types.SimpleNamespace(
            setRange=lambda *args: None, setVisible=lambda *args: None,
            setValue=lambda *args: None)
        dashboard.progress_label = types.SimpleNamespace(
            setText=lambda *args: None, setVisible=lambda *args: None)
        dashboard.update_status = lambda: None
        dashboard.values = lambda: {"account_name": account,
                                    "hardware_id": "hw",
                                    config.PREFERRED_FORMATS: ["EPUB"]}
        dashboard._effective_refresh = lambda: "tok-%s" % account
        dashboard._save_visible_account = lambda *args: True
        dashboard.snapshots, dashboard.prepared = [], []
        dashboard.confirmed, dashboard.started = [], []
        dashboard._start_local_snapshot = (
            lambda *args: dashboard.snapshots.append(args))
        dashboard._start_prepare_worker = (
            lambda *args: dashboard.prepared.append(args))
        dashboard._start_sync_worker = (
            lambda *args: dashboard.started.append(args))
        def confirm(*_args, **_kwargs):
            dashboard.confirmed.append(True)
            return choice()

        dashboard._confirm_comparison = confirm
        for name in ("start_sync", "_snapshot_ready", "_prepare_ready",
                     "_finish_progress_thread", "_finish_snapshot",
                     "_finish_prepare", "_snapshot_failed", "_prepare_failed",
                     "_failed"):
            setattr(dashboard, name, self.method(name)(dashboard))
        return dashboard, database

    def context(self, account="A"):
        return RunContext.from_values(
            {"account_name": account, "hardware_id": "hw",
             "preferred_formats": ["EPUB"]})

    def payload(self, context):
        return {"context": context, "metadata": {}, "state": {},
                "comparison": [], "hardware": None}

class ChainTests(ChainCase):
    """Der Weg vom lokalen Snapshot zum Worker -- einmal ganz, zweimal nicht."""

    def test_the_chain_from_the_library_to_the_worker(self):
        dashboard, database = self.dashboard()
        dashboard.start_sync()
        self.assertEqual([database], [args[0] for args in dashboard.snapshots])

        dashboard._snapshot_ready(self.payload(self.context()))
        self.assertEqual(1, len(dashboard.prepared))

        dashboard._prepare_ready(self.payload(self.context()))
        self.assertEqual(1, len(dashboard.confirmed))
        self.assertEqual(1, len(dashboard.started))

    def test_a_failed_settings_save_stops_the_comparison(self):
        dashboard, _database = self.dashboard()
        shown = []
        dashboard._save_visible_account = lambda *args: False
        dashboard._save_failed = shown.append
        dashboard.start_sync()
        self.assertEqual([], dashboard.snapshots)
        self.assertEqual(["Einstellungen nicht gespeichert"], shown)

    def test_a_cancelled_comparison_starts_no_worker(self):
        dashboard, _database = self.dashboard()
        dashboard._confirm_comparison = lambda *args, **kwargs: None
        dashboard._prepare_ready(self.payload(self.context()))
        self.assertEqual([], dashboard.started)

    def test_a_comparison_of_another_account_is_not_shown(self):
        dashboard, _database = self.dashboard(account="B")
        dashboard._prepare_ready(self.payload(self.context("A")))
        self.assertEqual(([], []), (dashboard.confirmed, dashboard.started))
        self.assertIn("A", dashboard.status.value)
        self.assertIn("B", dashboard.status.value)


class StageFailureTests(ChainCase):
    """Fehler und Abbruch in der Kette: eine Meldung, kein Rest, klare Zeile."""

    def failed_stage(self, stage, message, cancelled=False):
        dashboard, _database = self.dashboard()
        setattr(dashboard, stage + "_thread", SyncThread())
        setattr(dashboard, stage + "_worker",
                types.SimpleNamespace(cancelled=cancelled))
        messages = []
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: messages.append(("info", args[1]))), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs:
                      messages.append(("error", args[1]))):
            getattr(dashboard, "_%s_failed" % stage)(message)
        return dashboard, messages

    def test_a_cancelled_stage_is_reported_and_leaves_no_thread(self):
        dashboard, messages = self.failed_stage(
            "snapshot", self.ui.dashboard.CANCEL_MESSAGE)
        self.assertEqual([("info", "Abgebrochen")], messages)
        self.assertIsNone(dashboard.snapshot_thread)
        self.assertIsNone(dashboard.snapshot_worker)
        self.assertEqual("", dashboard.status.value)

    def test_a_rejected_login_moves_the_status_line_to_relogin(self):
        dashboard, messages = self.failed_stage(
            "prepare", "tolino hat die Anmeldung abgelehnt. (Code A01)")
        self.assertEqual([("error", "Vergleich fehlgeschlagen")], messages)
        self.assertEqual(self.ui.dashboard.NEEDS_LOGIN_STATUS,
                         dashboard.status.value)
        self.assertIsNone(dashboard.prepare_thread)
        self.assertIsNone(dashboard.prepare_worker)

    def test_another_error_leaves_the_status_line_alone(self):
        dashboard, messages = self.failed_stage("prepare", "Kein Netz")
        self.assertEqual([("error", "Vergleich fehlgeschlagen")], messages)
        self.assertEqual("", dashboard.status.value)

    def test_a_rejected_login_of_another_account_keeps_the_status_line(self):
        dashboard, _database = self.dashboard()
        dashboard.account_name = "B"
        dashboard.prepare_thread = SyncThread()
        dashboard.prepare_worker = types.SimpleNamespace(
            cancelled=False, account_name="A")
        messages = []
        with patch("calibre_plugin.sync_results.error_dialog",
                   lambda *args, **kwargs:
                   messages.append(("error", args[1]))):
            dashboard._prepare_failed("Anmeldung abgelehnt. (Code A01)")
        self.assertEqual([("error", "Vergleich fehlgeschlagen")], messages)
        self.assertEqual("", dashboard.status.value)


def choice():
    """Leere, aber vollständige Auswahl aus dem Vergleichsfenster."""
    return {"uploads": set(), "metadata": set(), "read_changes": [],
            "cloud_deletions": []}


if __name__ == "__main__":
    unittest.main()

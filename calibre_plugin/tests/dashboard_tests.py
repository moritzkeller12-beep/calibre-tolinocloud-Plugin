"""Was nach einem Sync-Lauf gesichert und dem Nutzer gemeldet wird.

Der Lauf ist vorbei (Client und Thread), geprüft werden Ergebnis-Sicherung,
Konto-Zuordnung der Spalten und die Rückmeldung je Ausgang. Worker und
Dashboard sind echte Objekte bzw. das vorhandene Dashboard-Doppel.
"""
import types
import unittest
import uuid
from unittest.mock import patch

from .. import config
from ..library import TOLINO_COLUMN, TOLINO_PROGRESS_COLUMN, TOLINO_READ_COLUMN
from ..run_context import RunContext
from ._support import (CalibreDatabase, dashboard_stub, real_qt_stub,
                       ui_module_for)

UUID = "11111111-1111-4111-8111-111111111111"


class OrchestrationCase(unittest.TestCase):
    """Frischer Einstellungsspeicher und geladenes UI-Modul je Test."""

    def setUp(self):
        self._original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test-%s" % uuid.uuid4().hex)
        self.addCleanup(setattr, config, "PREFERENCES", self._original)
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

    def sync_completed(self, dashboard, state, updates):
        method = self.ui.dashboard.SyncDashboard.sync_completed
        return method.__get__(dashboard)(state, updates)

    def sync_failed(self, dashboard, message):
        method = self.ui.dashboard.SyncDashboard.sync_failed
        return method.__get__(dashboard)(message)


def full_database(fail=()):
    return CalibreDatabase(
        {TOLINO_COLUMN: {"datatype": "text"},
         TOLINO_READ_COLUMN: {"datatype": "bool"},
         TOLINO_PROGRESS_COLUMN: {"datatype": "int"}}, fail=set(fail))


class PersistTests(OrchestrationCase):
    """Gespeichert wird unter dem Konto des Laufs, Spalten nur sichtbar."""

    def test_the_state_is_saved_under_the_account_of_the_run(self):
        config.save_account("A", {"refresh_token": "tok-A",
                                  "state": {"alt": {}}})
        config.save_account("B", {"refresh_token": "tok-B"})
        worker = self.worker(account="B")
        dashboard = self.dashboard(worker, full_database())
        failures = self.ui.sync_results.persist_sync_result(
            dashboard, {UUID: {"tolino_id": "d1"}}, worker.updates)
        self.assertEqual([], failures)
        self.assertEqual({UUID: {"tolino_id": "d1"}},
                         config.account_by_name("B")["state"])
        self.assertEqual({"alt": {}}, config.account_by_name("A")["state"])

    def test_reading_columns_only_for_the_displayed_account(self):
        worker = self.worker(account="B")
        database = full_database()
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database, account="A"),
            {UUID: {"tolino_id": "d1"}}, worker.updates)
        self.assertEqual([], failures)
        self.assertEqual([TOLINO_COLUMN],
                         [column for column, _values in database.api.writes])

        database = full_database()
        worker = self.worker(account="A")
        self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), {UUID: {"tolino_id": "d1"}},
            worker.updates)
        self.assertEqual([TOLINO_COLUMN, TOLINO_READ_COLUMN,
                          TOLINO_PROGRESS_COLUMN],
                         [column for column, _values in database.api.writes])

    def test_a_failing_column_is_named_but_the_state_is_saved(self):
        # Ein gelaufenes Konto existiert; `save_account` legt keines neu an.
        config.save_account("A", {"refresh_token": "tok-A"})
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database(fail=[TOLINO_COLUMN]))
        failures = self.ui.sync_results.persist_sync_result(
            dashboard, {UUID: {"tolino_id": "d1"}}, worker.updates)
        self.assertEqual(["tolino-IDs"], failures)
        self.assertEqual({UUID: {"tolino_id": "d1"}},
                         config.account_by_name("A")["state"])

    def test_the_cleanup_callback_runs_even_when_saving_fails(self):
        """Geprueft wird `finish_sync`; das Thread-Ende deckt ui_thread_tests ab."""
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database(fail=[TOLINO_COLUMN]))
        outcome, failures = self.ui.sync_results.finish_sync(
            dashboard, {UUID: {"tolino_id": "d1"}}, worker.updates)
        self.assertEqual({"uploaded": 1}, outcome)
        self.assertEqual(["tolino-IDs"], failures)
        self.assertTrue(dashboard.finished)

    def test_an_unknown_column_is_simply_skipped(self):
        worker = self.worker()
        database = CalibreDatabase({TOLINO_COLUMN: {"datatype": "text"}})
        failures = self.ui.sync_results.persist_sync_result(
            self.dashboard(worker, database), {}, worker.updates)
        self.assertEqual([], failures)
        self.assertEqual([TOLINO_COLUMN],
                         [column for column, _values in database.api.writes])


class ReportTests(OrchestrationCase):
    """Jeder Ausgang hat seine eigene Rückmeldung; geschlossen bleibt still."""

    def test_a_successful_run_reports_its_result(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database())
        recorded = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: recorded["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: recorded["error"].append(args)):
            self.sync_completed(dashboard, {UUID: {"tolino_id": "d1"}},
                                worker.updates)
        self.assertEqual("Fertig", recorded["info"][0][1])
        self.assertIn("hochgeladen", recorded["info"][0][2])
        self.assertEqual([], recorded["error"])
        self.assertTrue(dashboard.finished)

    def test_a_cancelled_run_reports_a_cancellation(self):
        worker = self.worker(outcome={"cancelled": True})
        dashboard = self.dashboard(worker, full_database())
        recorded = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: recorded["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: recorded["error"].append(args)):
            self.sync_completed(dashboard, {}, worker.updates)
        self.assertIn("Abgebrochen", recorded["info"][0][2])
        self.assertEqual([], recorded["error"])

    def test_a_partial_failure_gets_its_own_heading(self):
        worker = self.worker(outcome={"uploaded": 1, "failed": 1})
        dashboard = self.dashboard(worker, full_database())
        recorded = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: recorded["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: recorded["error"].append(args)):
            self.sync_completed(dashboard, {}, worker.updates)
        self.assertEqual("Teilweise fehlgeschlagen", recorded["error"][0][1])
        self.assertIn("Teilweise abgeschlossen", recorded["error"][0][2])

    def test_a_closed_window_gets_no_dialog(self):
        worker = self.worker()
        dashboard = self.dashboard(worker, full_database(), visible=False)
        recorded = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: recorded["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: recorded["error"].append(args)):
            self.sync_completed(dashboard, {}, worker.updates)
        self.assertEqual(([], []), (recorded["info"], recorded["error"]))
        self.assertTrue(dashboard.finished)

    def test_a_failed_run_keeps_the_ids_of_completed_uploads(self):
        worker = self.worker(outcome={"failed": 1})
        database = full_database()
        dashboard = self.dashboard(worker, database)
        recorded = {"info": [], "error": []}
        with patch("calibre_plugin.sync_results.info_dialog",
                   lambda *args, **kwargs: recorded["info"].append(args)), \
                patch("calibre_plugin.sync_results.error_dialog",
                      lambda *args, **kwargs: recorded["error"].append(args)):
            self.sync_failed(dashboard, "Die Verbindung ist fehlgeschlagen.")
        self.assertIn(TOLINO_COLUMN,
                      [column for column, _values in database.api.writes])
        self.assertIn("Die Verbindung ist fehlgeschlagen",
                      recorded["error"][0][2])
        self.assertTrue(dashboard.finished)


if __name__ == "__main__":
    unittest.main()

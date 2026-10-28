"""Hintergrundarbeit: Sync, Bestand und Keep-alive."""

import calibre_plugin.tolino as tolino_module
import calibre_plugin.token_vault as vault_module
import calibre_plugin.client as client_module
import unittest
from . import ui_base_tests, worker_base_tests
from .. import config
from ..plan import plan_sync
from ..tolino import TolinoApiError, TolinoAuthError, TolinoClient
from ._support import (compare_inventory, parsed_inventory, start_cdp_stubs,
                       stop_cdp_stubs, ui_module_for)
from unittest.mock import patch


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class CloudDeletionWorkerTests(unittest.TestCase):
    """Der Worker führt ausdrücklich markierte Cloud-Löschungen direkt aus."""

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def test_explicitly_marked_deletion_is_executed(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal": self.Signal})
        calls = []
        emitted = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated"
                self.access = "access"

            def login(self):
                return self.refresh

            def delete(self, tolino_id):
                calls.append(("delete", tolino_id))

        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "state": {"u-1": {"tolino_id": "t-1",
                                      "calibre_id": 7,
                                      "title": "Titel"}}}
        try:
            with patch("calibre_plugin.client.TolinoClient", FakeClient), \
                 patch("calibre_plugin.ui_workers.save_account",
                       lambda *args, **kwargs: None):
                worker = ui_module.workers.SyncWorker(
                    settings, [], [("u-1", "t-1")], [], {}, [], [])
                worker.completed.connect(
                    lambda *args: emitted.update(completed=args))
                worker.failed.connect(
                    lambda *args: emitted.update(failed=args))
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("delete", "t-1")], calls)
        self.assertNotIn("u-1", worker.state)
        # Die Calibre-Spalte zeigt danach nicht mehr auf die tote Kopie.
        self.assertEqual([(7, "")], worker.updates)
        self.assertEqual(1, worker.outcome["deleted"])
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))

class SyncWorkerPartialResultTests(unittest.TestCase):
    """Teilresultate, Abbruch und Kontobindung im SyncWorker (P1/C/E/H)."""

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    @staticmethod
    def _settings(state, name="A"):
        return {"account_name": name, "partner_id": 4,
                "hardware_id": "hw-" + name,
                "refresh_token": "tok-" + name,
                "state": state}

    @staticmethod
    def _job(ui_module, book_id, uuid, cover=None, title=""):
        return ui_module.common.SyncJob(book_id, uuid, "EPUB", None,
                                 "/library/%s.epub" % uuid, cover, title)

    def _run(self, ui_module, settings, jobs, fake_client,
             removals=(), meta_updates=(), read_marks=(), read_unmarks=(),
             series_map=None):
        emitted = {"completed": None, "failed": None, "progress": []}
        saved, credentials = [], []
        with patch("calibre_plugin.client.TolinoClient", fake_client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, active=None: saved.append(
                       (name, values))), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args: credentials.append(args)):
            worker = ui_module.workers.SyncWorker(
                settings, jobs, removals, meta_updates, series_map or {},
                read_marks, read_unmarks)
            worker.progress.connect(
                lambda *args: emitted["progress"].append(args))
            worker.completed.connect(
                lambda *args: emitted.update(completed=args))
            worker.failed.connect(
                lambda *args: emitted.update(failed=args))
            worker.run()
        return worker, emitted, saved, credentials

    def _metadata(self, uuid="u1", title="A"):
        return {"uuid": uuid, "title": title, "formats": ["EPUB"],
                "last_modified": "1"}

    def test_first_upload_survives_a_failing_second_upload(self):
        ui_module = self._ui()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def upload(self, path):
                if "u2" in path:
                    raise RuntimeError("Netz weg")
                return "new-1"

        metadata = {1: self._metadata("u1"), 2: self._metadata("u2", "B")}
        # Der Worker setzt den bestätigten Zustand nach erfolgreichem Upload.
        settings = self._settings({"u2": {"tolino_id": None}})
        settings["metadata"] = metadata
        try:
            worker, emitted, saved, _credentials = self._run(
                ui_module, settings,
                [self._job(ui_module, 1, "u1"), self._job(ui_module, 2, "u2")],
                FakeClient)
        finally:
            ui_module._restore_stubs_for_tests()
        # endet der Lauf regulaer: u2 ist gezaehlt, nicht fatal.
        self.assertIsNotNone(emitted["completed"])
        self.assertIsNone(emitted["failed"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertIsNone(worker.state["u2"]["tolino_id"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual([(1, "new-1")], worker.updates)
        # Der geprüfte Teilstand wurde sofort unter Konto A gespeichert.
        self.assertTrue(any(name == "A" and "u1" in values.get("state", {})
                            for name, values in saved))
        # Folgelauf: u1 ist erledigt, u2 bleibt offen -> kein Duplikat.
        uploads = plan_sync(
            metadata, worker.state, ["EPUB"], upload_book_ids={2})
        self.assertEqual([(2, "u2", "EPUB", None)], uploads)
        self.assertIsNone(worker.state["u2"]["tolino_id"])

    def test_failed_cover_keeps_the_uploaded_id(self):
        ui_module = self._ui()
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"

            def login(self):
                return self.refresh

            def upload(self, path):
                return "new-1"

            def upload_cover(self, tolino_id, path):
                raise RuntimeError("cover kaputt")

        try:
            worker, emitted, saved, _credentials = self._run(
                ui_module, self._settings(state),
                [self._job(ui_module, 1, "u1", cover="/tmp/c.jpg")],
                FakeClient)
        finally:
            ui_module._restore_stubs_for_tests()
        # Das Cover ist gezaehlt, der Lauf bleibt erfolgreich.
        self.assertIsNotNone(emitted["completed"])
        self.assertIsNone(emitted["failed"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertTrue(any(name == "A" for name, _values in saved))

    def test_a_failed_upload_does_not_stop_the_next_one(self):
        """Ein einzelner Fehler beendet den Lauf nicht mehr."""
        ui_module = self._ui()
        metadata = {1: self._metadata("u1"), 2: self._metadata("u2", "B"),
                    3: self._metadata("u3", "C")}
        state = {"u2": {"tolino_id": None}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"
                self.sent = []

            def login(self):
                return self.refresh

            def upload(self, path):
                if "u2" in path:
                    raise RuntimeError("Netz weg")
                self.sent.append(path)
                return "new-%d" % len(self.sent)

        settings = self._settings(state)
        settings["metadata"] = metadata
        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, settings,
                [self._job(ui_module, 1, "u1"), self._job(ui_module, 2, "u2"),
                 self._job(ui_module, 3, "u3")], FakeClient)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted["failed"])
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(2, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(0, worker.outcome["not_started"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertIsNone(worker.state["u2"]["tolino_id"])
        self.assertEqual("new-2", worker.state["u3"]["tolino_id"])

    def test_a_failed_cover_does_not_stop_the_next_book(self):
        ui_module = self._ui()
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"
                self.uploads = 0

            def login(self):
                return self.refresh

            def upload(self, path):
                self.uploads += 1
                return "new-%d" % self.uploads

            def upload_cover(self, tolino_id, path):
                raise RuntimeError("cover kaputt")

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state),
                [self._job(ui_module, 1, "u1", cover="/tmp/c.jpg"),
                 self._job(ui_module, 2, "u2")], FakeClient)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted["failed"])
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(2, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual("new-2", worker.state["u2"]["tolino_id"])

    def test_a_failed_delete_keeps_its_entry_and_the_run_continues(self):
        ui_module = self._ui()
        state = {"u-1": {"tolino_id": "t-1", "calibre_id": 1},
                 "u-2": {"tolino_id": "t-2", "calibre_id": 2}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def delete(self, tolino_id):
                if tolino_id == "t-1":
                    raise RuntimeError("Cloud sagt nein")

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state), [],
                FakeClient, removals=[("u-1", "t-1"), ("u-2", "t-2")])
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted["failed"])
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(1, worker.outcome["deleted"])
        self.assertEqual(1, worker.outcome["failed"])
        # Die gescheiterte Löschung bleibt im Zustand -- der nächste Lauf
        # versucht sie erneut.
        self.assertIn("u-1", worker.state)
        self.assertNotIn("u-2", worker.state)

    def test_a_failed_metadata_update_does_not_stop_the_uploads(self):
        ui_module = self._ui()
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def update_metadata(self, tolino_id, fields):
                raise RuntimeError("meta kaputt")

            def upload(self, path):
                return "new-1"

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state),
                [self._job(ui_module, 2, "u2")], FakeClient,
                meta_updates=[(1, "u1", "t-1", {"title": "Neu"})])
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])

    def test_a_failed_collection_is_not_recorded_as_success_and_retries(self):
        ui_module = self._ui()
        state = {"u1": {"tolino_id": "t-1", "calibre_id": 1}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"
                self.calls = 0

            def login(self):
                return self.refresh

            def collection_pairs(self):
                self.calls += 1
                return set()

            def add_to_collection(self, tolino_id, series, revision=None):
                raise RuntimeError("Sammlung nicht erreichbar")

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state), [], FakeClient,
                series_map={"u1": "S1"})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(0, worker.outcome["collections"])
        self.assertEqual(0, worker.outcome["updated"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertNotIn("collections", worker.state["u1"])

    def test_a_failed_series_does_not_stop_the_other_series(self):
        ui_module = self._ui()
        state = {"u1": {"tolino_id": "t-1", "calibre_id": 1},
                 "u2": {"tolino_id": "t-2", "calibre_id": 2}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"
                self.added = []

            def login(self):
                return self.refresh

            def collection_pairs(self):
                return set()

            def add_to_collection(self, tolino_id, series, revision=None):
                if series == "S1":
                    raise RuntimeError("Sammlung kaputt")
                self.added.append((tolino_id, series))
                return 7

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state), [], FakeClient,
                series_map={"u1": "S1", "u2": "S2"})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNotNone(emitted["completed"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(1, worker.outcome["collections"])
        self.assertEqual(["S2"], worker.state["u2"].get("collections"))
        self.assertNotIn("collections", worker.state["u1"])

    def test_an_auth_error_still_stops_the_run(self):
        """Anmeldeprobleme bleiben fatal -- jeder weitere Aufruf scheiterte."""
        ui_module = self._ui()
        state = {}
        attempts = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def upload(self, path):
                attempts.append(path)
                raise TolinoAuthError("Sitzung beendet.", code="A01")

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state),
                [self._job(ui_module, 1, "u1"), self._job(ui_module, 2, "u2")],
                FakeClient)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted["completed"])
        self.assertIsNotNone(emitted["failed"])
        self.assertEqual(1, len(attempts))
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(1, worker.outcome["not_started"])

    def test_progress_and_report_name_the_title_not_the_uuid(self):
        """«Lade hoch: 8f3c…» sagt niemandem etwas."""
        ui_module = self._ui()
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def upload(self, path):
                return "new-1"

        worker, emitted, _saved, _credentials = self._run(
            ui_module, self._settings(state),
            [self._job(ui_module, 1, "u1", title="Der Schwarm")], FakeClient)

        texts = [text for _done, _total, text in emitted["progress"]]
        labels = [item["label"] for item in worker.operations]
        for text in texts + labels:
            self.assertNotIn("u1", text)
        self.assertIn("Der Schwarm", "\n".join(texts))
        self.assertIn("Hochladen: Der Schwarm (EPUB)", labels)
        # Die Kennung bleibt fuer Technik und Log erhalten.
        self.assertEqual("u1", worker.operations[0]["book_uuid"])

    def test_a_book_without_title_is_named_by_its_number(self):
        """Ohne Titel steht die Calibre-Nummer da -- nie eine UUID."""
        ui_module = self._ui()
        self.assertEqual("Buch 7", ui_module.common.display_title("", 7))
        self.assertEqual("Buch ohne Titel",
                         ui_module.common.display_title("", ""))
        self.assertEqual("Titel", ui_module.common.display_title(" Titel "))
        worker = ui_module.workers.SyncWorker(
            self._settings({}), [self._job(ui_module, 7, "u7")], [])
        self.assertEqual("Buch 7", worker._label("u7", 7))
        self.assertEqual("Buch ohne Titel", worker._label("u7"))
        self.assertEqual("Buch 7", worker.jobs[0].label())

    def test_the_detail_text_lists_every_failed_step(self):
        """Max. 5 im Text, alle im Detail -- sonst fehlt der Rest."""
        ui_module = self._ui()
        operations = [{"kind": "upload", "label": "Hochladen: Buch %d" % index,
                       "status": "failed", "error": "Netz weg"}
                      for index in range(1, 8)]
        outcome = {"uploaded": 0, "updated": 0, "failed": 7,
                   "operations": operations}
        text = ui_module.common.sync_result_text(outcome)
        detail = ui_module.common.sync_result_detail(outcome)
        self.assertIn("… und 2 weitere", text)
        for index in range(1, 8):
            self.assertIn("Hochladen: Buch %d: Netz weg" % index, detail)
        no_detail = {"uploaded": 1, "updated": 0, "failed": 0,
                     "operations": []}
        self.assertEqual("", ui_module.common.sync_result_detail(no_detail))
        self.assertEqual("", ui_module.common.sync_result_text({}, ""))
        self.assertEqual("Fertig", ui_module.common.sync_result_text({}))

    def test_success_summary_keeps_action_types_separate(self):
        ui_module = self._ui()
        outcome = {"uploaded": 1, "metadata": 2, "read_marked": 3,
                   "read_unmarked": 4, "collections": 5, "deleted": 6,
                   "operations": []}
        text = ui_module.common.sync_result_text(outcome)
        for count_label in ("1 hochgeladen", "2 Metadaten",
                            "3 als gelesen markiert",
                            "4 als ungelesen markiert", "5 Sammlungen",
                            "6 gelöscht"):
            self.assertIn(count_label, text)
        self.assertNotIn("6 aktualisiert", text)

    def test_failure_summary_appends_error_code_to_safe_error_text(self):
        ui_module = self._ui()
        outcome = {"uploaded": 0, "updated": 0, "failed": 1,
                   "operations": [{"kind": "upload", "label": "Upload: Buch A",
                                   "status": "failed",
                                   "error": "Netz nicht erreichbar (Code N01)"}]}
        text = ui_module.common.sync_result_text(outcome)
        self.assertIn("Netz nicht erreichbar (Code N01)", text)

    def test_the_result_line_names_the_failed_step(self):
        ui_module = self._ui()
        outcome = {"uploaded": 2, "updated": 0, "failed": 1,
                   "operations": [
                       {"kind": "upload", "label": "Hochladen: u2",
                        "status": "failed", "error": "Netz weg"}]}
        text = ui_module.common.sync_result_text(outcome)
        self.assertIn("1 Fehler", text)
        self.assertIn("Hochladen: u2", text)

    def test_cancel_finishes_gracefully_and_marks_remaining_as_not_started(self):
        ui_module = self._ui()
        state = {}
        emitted = {"completed": None, "failed": None}
        saved = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.calls = 0

            def login(self):
                return self.refresh

            def upload(self, path):
                self.calls += 1
                if self.calls == 1:
                    holder["worker"].cancel()
                return "new-1"

        holder = {}
        fake = FakeClient()
        with patch("calibre_plugin.client.TolinoClient", lambda *a, **k: fake), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, active=None: saved.append(
                       (name, values))):
            worker = ui_module.workers.SyncWorker(
                self._settings(state),
                [self._job(ui_module, 1, "u1"), self._job(ui_module, 2, "u2")],
                [])
            holder["worker"] = worker
            worker.completed.connect(
                lambda *args: emitted.update(completed=args))
            worker.failed.connect(lambda *args: emitted.update(failed=args))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted["failed"])
        self.assertIsNotNone(emitted["completed"])
        self.assertTrue(worker.outcome["cancelled"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["not_started"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(1, fake.calls)  # u2 wurde nicht begonnen

    def test_later_read_failure_keeps_the_upload_and_counts_both(self):
        ui_module = self._ui()
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"

            def login(self):
                return self.refresh

            def upload(self, path):
                return "new-1"

            def mark_read_many(self, book_ids, finished=True):
                raise RuntimeError("sync-data kaputt")

        try:
            worker, emitted, _saved, _credentials = self._run(
                ui_module, self._settings(state),
                [self._job(ui_module, 1, "u1")], FakeClient,
                read_marks=["new-1"])
        finally:
            ui_module._restore_stubs_for_tests()
        # wird der Fehler gezaehlt; der Upload bleibt bestehen.
        self.assertIsNotNone(emitted["completed"])
        self.assertIsNone(emitted["failed"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(0, worker.outcome["not_started"])

    def test_account_switch_during_a_run_never_writes_to_the_new_account(self):
        ui_module = self._ui()
        original = config.PREFERENCES
        config.PREFERENCES = config.JSONConfig("test")
        self.addCleanup(setattr, config, "PREFERENCES", original)
        config.settings()
        config.save_account("A", {"refresh_token": "tok-A"}, active="A")
        config.save_account("B", {"refresh_token": "tok-B"}, active="B")
        state = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"

            def login(self):
                return self.refresh

            def upload(self, path):
                # Waehrend des Laufs wird B aktiv.
                config.save_account("B", {"refresh_token": "tok-B"}, active="B")
                return "new-1"

            def mark_read_many(self, book_ids, finished=True):
                return None

        settings = self._settings(state, name="A")
        try:
            worker, _emitted, saved, credentials = self._run(
                ui_module, settings, [self._job(ui_module, 1, "u1")],
                FakeClient, read_marks=["new-1"])
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertTrue(saved)
        self.assertEqual({"A"}, {name for name, _values in saved})
        # Auch die Tokenrotation geht an das ERFASSTE Konto A.
        with patch("calibre_plugin.config.save_credentials",
                   lambda *args: credentials.append(args)):
            worker.store.save_refresh("rot-A")
        self.assertEqual({"A"}, {args[0] for args in credentials})
        # Der Worker-Kontext bleibt A, auch wenn settings() jetzt B liefert.
        self.assertEqual("A", worker.account_name)

    def test_results_are_persisted_to_the_captured_account_in_the_ui(self):
        ui_module = self._ui()
        saved, credentials, column_writes, boxes = [], [], [], []

        class FakeField:
            def __init__(self, text):
                self.value = text

            def text(self):
                return self.value

            def setText(self, value):
                self.value = value

        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "B"
        dashboard.refresh = FakeField("tok-B")
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = True
        dashboard.finish_thread = lambda: None
        worker = type("Worker", (), {})()
        worker.account_name = "A"
        worker.outcome = {"uploaded": 1, "updated": 0, "downloaded": 0,
                          "skipped": 0, "failed": 0, "not_started": 0}
        worker.state = {"u1": {"tolino_id": "new-1"}}
        worker.updates = [(1, "new-1")]
        dashboard.worker = worker
        with patch("calibre_plugin.ui_dashboard.save_account",
                   lambda name, values, active=None: saved.append((name, values))), \
             patch("calibre_plugin.ui_dashboard.save_credentials",
                   lambda *args: credentials.append(args)), \
             patch("calibre_plugin.ui_dashboard.update_tolino_ids",
                   lambda *args, **kwargs: column_writes.append((args, kwargs))), \
             patch("calibre_plugin.ui_dashboard.info_dialog",
                   lambda *args, **kwargs: boxes.append(args)):
            ui_module.dashboard.SyncDashboard.sync_completed(
                dashboard, {"u1": {"tolino_id": "new-1"}}, [(1, "new-1")])
        ui_module._restore_stubs_for_tests()
        self.assertEqual({"A"}, {name for name, _values in saved})
        self.assertEqual([], credentials)
        self.assertEqual(1, len(column_writes))
        self.assertEqual("A", column_writes[0][1]["account_name"])
        self.assertEqual("tok-B", dashboard.refresh.text())  # Feld bleibt B
        self.assertEqual(1, len(boxes))
        self.assertIn("1 hochgeladen", boxes[0][2])

    def test_cancel_is_shown_as_cancelled_not_as_a_failure(self):
        ui_module = self._ui()
        infos, errors = [], []
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "A"
        dashboard.refresh = type("F", (), {"text": lambda self: "tok",
                                           "setText": lambda self, v: None})()
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = False
        dashboard.finish_thread = lambda: None
        worker = type("Worker", (), {})()
        worker.account_name = "A"
        worker.cancelled = True
        worker.state = {"u1": {"tolino_id": "new-1"}}
        worker.updates = [(1, "new-1")]
        worker.outcome = {"uploaded": 1, "updated": 0, "downloaded": 0,
                          "skipped": 0, "failed": 0, "not_started": 1,
                          "cancelled": True}
        dashboard.worker = worker
        with patch("calibre_plugin.ui_dashboard.save_account", lambda *a, **k: None), \
             patch("calibre_plugin.ui_dashboard.save_credentials", lambda *a, **k: None), \
             patch("calibre_plugin.ui_dashboard.update_tolino_ids", lambda *a, **k: None), \
             patch("calibre_plugin.ui_dashboard.info_dialog",
                   lambda *args, **kwargs: infos.append(args)), \
             patch("calibre_plugin.ui_dashboard.error_dialog",
                   lambda *args, **kwargs: errors.append(args)):
            ui_module.dashboard.SyncDashboard.sync_failed(
                dashboard, ui_module.common.CANCEL_MESSAGE)
        ui_module._restore_stubs_for_tests()
        self.assertEqual([], errors)
        self.assertEqual(1, len(infos))
        self.assertEqual("Abgebrochen", infos[0][1])
        self.assertIn("hochgeladen", infos[0][2])

class PrepareWorkerTests(unittest.TestCase):
    """Netz-Vorbereitung im Hintergrund, gebunden an das Konto (P2/G)."""

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    def test_ready_payload_carries_comparison_and_captured_account(self):
        ui_module = self._ui()
        captured, credentials = [], []

        class FakeClient:
            def __init__(self, partner_id, hardware, refresh, token_callback=None,
                         hardware_callback=None):
                self.refresh = "rot-A"
                self.hardware = hardware
                self.token_callback = token_callback

            def login(self):
                self.token_callback("rot-A")
                return self.refresh

            def inventory(self):
                return []

            def read_ids(self):
                return {"t-1"}

        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-A", "refresh_token": "tok-A", "state": {},
                    "preferred_formats": ["EPUB"],
                    "sync_column_enabled": False}
        payloads = []
        with patch("calibre_plugin.client.TolinoClient", FakeClient), \
             patch("calibre_plugin.config.account_by_name", lambda _name: None), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args: credentials.append(args)), \
             patch("calibre_plugin.ui_workers.compare_inventory",
                   lambda *args, **kwargs: captured.append(
                       (args, kwargs)) or [{"status": "review"}]):
            worker = ui_module.workers.PrepareWorker(settings, {1: {"title": "A"}})
            worker.ready.connect(payloads.append)
            worker.failed.connect(lambda text: payloads.append(text))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        payload = payloads[0]
        self.assertEqual("A", payload["account_name"])
        self.assertEqual([{"status": "review"}], payload["comparison"])
        self.assertNotIn("refresh", payload)
        # Der Token wird ausschließlich über den Client-Callback gespeichert.
        self.assertEqual("rot-A", payload["settings"]["refresh_token"])
        self.assertEqual({("A", "rot-A", "hw-A")}, set(credentials))
        # Der Vergleich bekommt die Einstellungen (Vergleichsfelder,
        # Tolino-ID-Spalte) mit.
        _args, kwargs = captured[0]
        self.assertEqual(("authors", "title", "isbn"), tuple(args_kw for args_kw
                         in ("authors", "title", "isbn")))
        self.assertIn("read_ids", kwargs)

    def test_cancel_between_login_and_inventory_fails_softly(self):
        ui_module = self._ui()
        holder = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"

            def login(self):
                return "rot"

            def inventory(self):
                holder["worker"].cancel()
                return []

        messages = []
        with patch("calibre_plugin.client.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok",
                 "state": {}, "preferred_formats": ["EPUB"]}, {})
            holder["worker"] = worker
            worker.failed.connect(messages.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual([ui_module.common.CANCEL_MESSAGE], messages)
        # Auch der Abbruch gibt den Keep-alive wieder frei.
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_an_unreadable_inventory_names_the_seen_fields(self):
        """N02 ergänzt den Nutzertext mit der erkannten Antwortform."""
        ui_module = self._ui()
        messages = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"
                self.inventory_ok = False

            def login(self):
                return "rot"

            def inventory(self):
                return []

        with patch("calibre_plugin.client.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok",
                 "state": {}, "preferred_formats": ["EPUB"]}, {})
            worker.failed.connect(messages.append)
            worker.ready.connect(lambda payload: messages.append("ready"))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(messages))
        self.assertIn("enthält kein PublicationInventory", messages[0])
        self.assertIn("N02", messages[0])
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_complete_inventory_clears_stale_cloud_id_in_worker_state(self):
        ui_module = self._ui()
        payloads = []

        class FakeClient:
            inventory_ok = True
            refresh = "rot"
            hardware = "hw"

            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return self.refresh

            def inventory(self):
                return []

            def read_ids(self):
                return set()

        metadata = {1: {"uuid": "u-stale", "title": "A",
                        "formats": ["EPUB"]}}
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "state": {"u-stale": {"tolino_id": "gone"}},
                    "preferred_formats": ["EPUB"]}
        with patch("calibre_plugin.client.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(settings, metadata)
            worker.ready.connect(payloads.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        self.assertNotIn("tolino_id", payloads[0]["state"]["u-stale"])

    def test_complete_inventory_clears_stale_cloud_id_even_with_review_records(self):
        ui_module = self._ui()
        payloads = []

        class FakeClient:
            inventory_ok = True
            refresh = "rot"
            hardware = "hw"

            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return self.refresh

            def inventory(self):
                return parsed_inventory([{"has_deliverable_id": False}])

            def read_ids(self):
                return set()

        metadata = {1: {"uuid": "u-stale", "title": "A",
                        "formats": ["EPUB"]}}
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "state": {"u-stale": {"tolino_id": "gone"}},
                    "preferred_formats": ["EPUB"]}
        with patch("calibre_plugin.client.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(settings, metadata)
            worker.ready.connect(payloads.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertNotIn("tolino_id", payloads[0]["state"]["u-stale"])

    def test_a_partly_readable_inventory_still_compares(self):
        """Einzelne nicht zuordenbare Cloud-Datensätze verhindern den Vergleich nicht."""
        ui_module = self._ui()
        payloads, compared = [], []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"
                self.inventory_ok = True

            def login(self):
                return "rot"

            def inventory(self):
                return parsed_inventory([{"deliverableId": "d1"}])

            def read_ids(self):
                return set()

        with patch("calibre_plugin.client.TolinoClient", FakeClient), \
                patch("calibre_plugin.ui_workers.compare_inventory",
                      lambda *args, **kwargs: compared.append(args[2]) or []):
            worker = ui_module.workers.PrepareWorker(
                {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok",
                 "state": {}, "preferred_formats": ["EPUB"]}, {})
            worker.failed.connect(payloads.append)
            worker.ready.connect(payloads.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        self.assertIsInstance(payloads[0], dict)
        self.assertEqual([[{"tolino_id": "d1", "has_deliverable_id": True,
                            "identifier": "", "title": "", "authors": "",
                            "isbn": "", "uuid": ""}]], compared)
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_login_failure_is_reported_without_starting_a_sync(self):
        ui_module = self._ui()

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "tok"
                self.hardware = "hw"

            def login(self):
                raise RuntimeError("Netz weg")

        messages = []
        with patch("calibre_plugin.client.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok",
                 "state": {}, "preferred_formats": ["EPUB"]}, {})
            worker.failed.connect(messages.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(messages))
        self.assertIn("Netz weg", messages[0])
        self.assertNotIn("tok", messages[0])
        # Auch der Fehlerpfad gibt den Keep-alive wieder frei.
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_a_running_rotation_is_waited_for_and_reported(self):
        """Laeuft gerade eine Keep-alive-Rotation, wird NICHT eingeloest.

        Der Refresh-Token ist single-use; zwei Einloesungen beenden die
        Sitzung. Statt zu warten bis der Nutzer denkt, nichts passiere,
        nennt die Meldung den Grund samt Code.
        """
        ui_module = self._ui()
        messages = []
        with vault_module.TOKEN_VAULT._lock:
            worker = ui_module.workers.PrepareWorker(
                {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok",
                 "state": {}, "preferred_formats": ["EPUB"]}, {},
                lock_timeout=0)
            worker.failed.connect(messages.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(messages))
        self.assertIn("S91", messages[0])
        self.assertIn("kurz warten", messages[0])
        self.assertNotIn("tok", messages[0])
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_a_rotated_stored_token_wins_before_the_login(self):
        """Was der Keep-alive inzwischen gespeichert hat, ist neuer als der
        Stand im Dialog -- der Lauf muss den gespeicherten Token nehmen."""
        ui_module = self._ui()
        seen = []

        class FakeClient:
            def __init__(self, partner_id, hardware, refresh, *args, **kwargs):
                seen.append((refresh, hardware))
                self.refresh = refresh
                self.hardware = hardware

            def login(self):
                return self.refresh

            def inventory(self):
                return []

            def read_ids(self):
                return set()

        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-alt", "refresh_token": "alt-token", "state": {},
                    "preferred_formats": ["EPUB"]}
        with patch("calibre_plugin.client.TolinoClient", FakeClient), \
                patch("calibre_plugin.config.account_by_name",
                      lambda name: {"name": "A", "refresh_token": "neu-token",
                                    "hardware_id": "hw-neu"}):
            worker = ui_module.workers.PrepareWorker(settings, {})
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual([("neu-token", "hw-neu")], seen)
        # Der Worker arbeitet auf einer Kopie; massgeblich ist, was er
        # weitergibt (der Aufrufer liest vor dem Sync erneut).
        self.assertEqual("neu-token", worker.settings["refresh_token"])
        self.assertEqual("hw-neu", worker.settings["hardware_id"])
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_client_factory_keeps_rotated_token_for_the_sync_worker(self):
        ui_module = self._ui()
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-A", "refresh_token": "tok-A",}
        updated, saved = [], []

        class FakeClient:
            def __init__(self, partner_id, hardware, refresh, token_callback=None,
                         hardware_callback=None):
                self.refresh = refresh
                self.hardware = hardware
                self.token_callback = token_callback

            def login(self):
                self.refresh = "rot-A"
                self.token_callback("rot-A")

        try:
            dashboard = ui_module.dashboard.SyncDashboard.__new__(
                ui_module.dashboard.SyncDashboard)
            dashboard.account_name = "A"
            with patch("calibre_plugin.client.TolinoClient", FakeClient), \
                 patch("calibre_plugin.config.save_credentials",
                       lambda *args: saved.append(args)):
                dashboard._worker_client(settings)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual("rot-A", settings["refresh_token"])
        # Kein Widget-Zugriff mehr: der Client entsteht im Worker-Thread.
        # Gespeichert wird trotzdem sofort.
        self.assertEqual([], updated)
        self.assertIn(("A", "rot-A", "hw-A"), saved)

class SyncKeepsKeepaliveQuietTests(unittest.TestCase):
    """Der Sync markiert sich als aktiv -- waehrend er laeuft,
    loest der Keep-alive keinen Token ein (und danach wieder)."""

    def test_worker_marks_the_sync_active_for_its_whole_run(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]})
        seen = []

        class FakeClient(object):
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated"

            def login(self):
                # Der einmalige Login läuft innerhalb des Vault-Locks.
                seen.append(int(vault_module.TOKEN_VAULT._lock.locked()))
                return "rotated"

        settings = {"partner_id": 4, "hardware_id": "hw",
                    "refresh_token": "tok",
                    "state": {}}
        try:
            with patch("calibre_plugin.client.TolinoClient", FakeClient):
                worker = ui_module.workers.SyncWorker(settings, [], [])
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([1], seen)
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_a_cancelled_run_also_releases_the_keepalive(self):
        """Ein Abbruch verlaesst `run` ueber eine Ausnahme -- der Zaehler
        muss trotzdem auf 0 zurueck (sonst bliebe der Keep-alive fuer immer
        gesperrt)."""
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]})
        completed = []

        class FakeClient(object):
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated"
                self.hardware = "hw"

            def login(self):
                return "rotated"

        settings = {"partner_id": 4, "hardware_id": "hw",
                    "refresh_token": "tok",
                    "state": {}}
        try:
            with patch("calibre_plugin.client.TolinoClient", FakeClient):
                worker = ui_module.workers.SyncWorker(
                    settings, [], [],
                    meta_updates=[(1, "u1", "t1", {"title": "A"})])
                worker.completed.connect(lambda *args: completed.append(True))
                worker.cancel()
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([True], completed)
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

class KeepaliveSafetyTests(unittest.TestCase):
    """Keep-alive und Sync duerfen den Refresh-Token nie gleichzeitig
    einloesen -- der Reuse-Schutz von Keycloak beendet die Sitzung sonst
    sofort. Und ein Fehler darf nicht still verschwinden."""

    def test_keepalive_skips_while_a_refresh_is_locked(self):
        with patch("calibre_plugin.token_vault.TolinoClient") as client_mock:
            with vault_module.TOKEN_VAULT._lock:
                result = vault_module.TOKEN_VAULT.keepalive_refresh(
                    lambda: {"accounts": [{"name": "A", "partner_id": 4,
                                            "hardware_id": "hw",
                                            "refresh_token": "tok"}]})
            self.assertIsNone(result)
            client_mock.assert_not_called()
            client_mock.return_value.login.return_value = None
            client_mock.return_value.refresh = "rotiert"
            self.assertEqual("rotiert", vault_module.TOKEN_VAULT.keepalive_refresh(
                lambda: {"accounts": [{"name": "A", "partner_id": 4,
                                        "hardware_id": "hw",
                                        "refresh_token": "tok"}]}))
        client_mock.assert_called_once()

    def test_token_vault_serializes_refreshes_and_releases_on_failure(self):
        from ..token_vault import TOKEN_VAULT
        store = config.CredentialStore(
            {"account_name": "A", "partner_id": 4,
             "hardware_id": "hw", "refresh_token": "tok"}, "A")
        with TOKEN_VAULT._lock:
            with self.assertRaises(TolinoAuthError) as caught:
                client_module.make_client(store, lock_timeout=0)
        self.assertEqual("S91", caught.exception.code)

        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                raise RuntimeError("kaputt")

        with patch("calibre_plugin.client.TolinoClient", FailingClient):
            with self.assertRaises(RuntimeError):
                client_module.make_client(store)
        self.assertFalse(TOKEN_VAULT._lock.locked())

    def test_a_successful_rotation_is_logged(self):
        """Ohne Erfolgszeile ist im Log nicht zu sehen, ob er laeuft."""
        logged = []

        class FakeClient(object):
            def __init__(self, *args, **kwargs):
                self.refresh = "neu"

            def login(self):
                return self.refresh

        with patch("calibre_plugin.token_vault.log_info", logged.append), \
                patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
            result = vault_module.TOKEN_VAULT.keepalive_refresh(
                lambda: {"accounts": [{"name": "A", "partner_id": 4,
                                        "hardware_id": "hw",
                                        "refresh_token": "alt"}]})
        self.assertEqual("neu", result)
        self.assertTrue(any("Keep-alive" in line for line in logged))

    def test_keepalive_failure_is_logged_without_the_token(self):
        logged = []

        class BoomClient(object):
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                raise RuntimeError("kaputt mit refresh_token=sk-geheim-token")

        with patch("calibre_plugin.token_vault.log_warning", logged.append), \
                patch("calibre_plugin.token_vault.TolinoClient", BoomClient):
            result = vault_module.TOKEN_VAULT.keepalive_refresh(
                lambda: {"accounts": [{"name": "A", "partner_id": 4,
                                        "hardware_id": "hw",
                                        "refresh_token": "sk-geheim-token"}]})
        self.assertIsNone(result)
        self.assertEqual(1, len(logged))
        self.assertIn("Keep-alive", logged[0])
        self.assertNotIn("sk-geheim-token", logged[0])

    def test_log_warning_never_raises_without_calibre(self):
        from ..tolino import log_warning
        log_warning("nur ein Test -- ohne Calibre muss das still bleiben")

class BackgroundCallTokenTests(unittest.TestCase):
    """Einzelaufrufe laufen ohne globalen Refresh-Token-Zähler."""

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    def test_the_factory_refuses_without_a_stored_token(self):
        """A03 vor jedem Netzaufruf -- wie die Dialogpruefung."""
        from ..token_vault import TOKEN_VAULT
        self._ui()
        store = config.CredentialStore(
            {"partner_id": 4, "refresh_token": ""})
        with self.assertRaises(tolino_module.TolinoAuthError) as caught:
            TOKEN_VAULT.login(store)
        self.assertEqual("A03", caught.exception.code)

class InventoryFailureDeletionTests(unittest.TestCase):
    """Ein nicht deutbarer Bestand loescht nichts.

    Der Lauf macht mit Uploads und Metadaten weiter; jede Loeschung und jede
    Zustandsbereinigung unterbleibt und steht als Fehler im Bericht.
    """

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    @staticmethod
    def _client(deleted):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def delete(self, tolino_id):
                if getattr(self, "fail_delete", False):
                    raise TolinoApiError("kaputt", code="N02")
                deleted.append(tolino_id)

            def upload(self, path):
                return "new-1"

        return FakeClient()

    def _settings(self, state):
        return {"account_name": "A", "partner_id": 4, "hardware_id": "hw-A",
                "refresh_token": "tok-A",
                "state": state}

    def _run(self, ui_module, settings, jobs, client):
        emitted, saved = {}, []
        with patch("calibre_plugin.client.TolinoClient",
                   lambda *args, **kwargs: client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, active=None: saved.append(values)), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args, **kwargs: None):
            worker = ui_module.workers.SyncWorker(settings, jobs, [("u1", "t1")])
            worker.completed.connect(
                lambda *args: emitted.update(completed=args))
            worker.failed.connect(lambda *args: emitted.update(failed=args))
            worker.run()
        return worker, emitted, saved

    def test_an_inventory_error_deletes_nothing_and_keeps_the_state(self):
        ui_module = self._ui()
        deleted = []
        state = {"u1": {"tolino_id": "t1", "calibre_id": 1, "title": "T"}}
        client = self._client(deleted)
        client.fail_delete = True
        worker, emitted, _saved = self._run(
            ui_module, self._settings(state), [], client)
        self.assertEqual([], deleted)
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        self.assertEqual(state, worker.state)
        self.assertEqual(1, worker.outcome["failed"])
        reasons = [item for item in worker.operations
                   if item["kind"] == "delete"]
        self.assertEqual(1, len(reasons))
        self.assertIn("kaputt", reasons[0]["error"])

    def test_a_replacement_upload_still_runs_without_deleting_the_old_copy(
            self):
        ui_module = self._ui()
        deleted = []
        job = ui_module.common.SyncJob(1, "u1", "EPUB", "t-alt",
                                       "/library/u1.epub", None, "Titel")
        state = {"u1": {"tolino_id": "t-alt", "calibre_id": 1, "title": "T"}}
        worker, emitted, _saved = self._run(
            ui_module, self._settings(state), [job], self._client(deleted))
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        # Ersatz und markierte Löschung werden explizit direkt ausgeführt.
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(["t-alt", "t1"], deleted)
        kinds = {item["kind"]: item["status"] for item in worker.operations}
        self.assertEqual("succeeded", kinds["upload"])

    def test_a_replacement_with_an_already_missing_old_copy_is_skipped(self):
        """HTTP 404 beim Aufräumen ist ein übersprungener, kein fehlgeschlagener Schritt."""
        ui_module = self._ui()
        deleted = []
        job = ui_module.common.SyncJob(1, "u1", "EPUB", "t-alt",
                                       "/library/u1.epub", None, "Titel")
        state = {"u1": {"tolino_id": "t-alt", "calibre_id": 1, "title": "T"}}
        client = self._client(deleted)
        def delete_old_only(tolino_id):
            if tolino_id == "t-alt":
                raise TolinoApiError("tolino-Fehler (HTTP 404)", code="N03")
            deleted.append(tolino_id)
        client.delete = delete_old_only
        worker, emitted, _saved = self._run(
            ui_module, self._settings(state), [job], client)
        self.assertIsNone(emitted.get("failed"))
        self.assertEqual(0, worker.outcome["failed"])
        self.assertEqual(1, worker.outcome["skipped"])
        self.assertEqual(["t1"], deleted)
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])


    def test_a_marked_deletion_already_missing_is_skipped_and_state_cleared(self):
        """Ein bestätigtes HTTP 404 bedeutet, dass die Kopie bereits fehlt."""
        ui_module = self._ui()
        deleted = []
        client = self._client(deleted)
        client.delete = lambda _tolino_id: (_ for _ in ()).throw(
            TolinoApiError("tolino-Fehler (HTTP 404)", code="N03"))
        state = {"u1": {"tolino_id": "t1", "calibre_id": 1, "title": "T"}}
        worker, emitted, _saved = self._run(
            ui_module, self._settings(state), [], client)
        self.assertEqual([], deleted)
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        self.assertNotIn("u1", worker.state)
        self.assertEqual([(1, "")], worker.updates)
        self.assertEqual(1, worker.outcome["skipped"])
        delete_ops = [item for item in worker.operations
                      if item["kind"] == "delete"]
        self.assertEqual(1, len(delete_ops))
        self.assertEqual("skipped", delete_ops[0]["status"])

    def test_a_failed_replacement_cleanup_is_counted_as_failed(self):
        """Ein Fehler beim direkten Löschaufruf wird im Bericht gezählt."""
        ui_module = self._ui()
        deleted = []
        job = ui_module.common.SyncJob(1, "u1", "EPUB", "t-alt",
                                       "/library/u1.epub", None, "Titel")
        client = self._client(deleted)
        def fail_old_delete(tolino_id):
            if tolino_id == "t-alt":
                raise TolinoApiError("Netzfehler beim Löschen", code="N01")
            deleted.append(tolino_id)
        client.delete = fail_old_delete
        state = {"u1": {"tolino_id": "t-alt", "calibre_id": 1, "title": "T"}}
        worker, emitted, _saved = self._run(
            ui_module, self._settings(state), [job], client)
        self.assertIsNone(emitted.get("failed"))
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(["t1"], deleted)
        kinds = {item["kind"]: item["status"] for item in worker.operations}
        self.assertEqual("succeeded", kinds["upload"])
        self.assertEqual("failed", kinds["delete"])
        delete_ops = [item for item in worker.operations
                      if item["kind"] == "delete"]
        self.assertTrue(any("Alte Kopie" in item["label"]
                            for item in delete_ops))
        self.assertEqual(1, worker.outcome["failed"])

class InventorySchemaTests(unittest.TestCase):
    """Eine unbekannte Bestandsantwort ist nie ein leerer Bestand.

    Der Umschlag entscheidet: fehlt er, scheitert der Vergleich (sonst
    saehe die Cloud leer aus). Einzelne undeutbare Datensaetze erlauben die
    Anzeige, sperren aber jede Loeschung.
    """

    @staticmethod
    def _client(data):
        client = TolinoClient(4, "hw", "refresh-token")
        client._request = lambda *args, **kwargs: data
        return client

    def test_a_confirmed_empty_inventory_is_empty(self):
        client = self._client({"PublicationInventory": {"edata": [],
                                                         "ebook": []}})
        records = client.inventory()
        self.assertEqual([], records)
        self.assertTrue(client.inventory_ok)

    def test_confirmed_records_are_extracted(self):
        data = {"PublicationInventory": {
            "edata": [{"deliverableId": "d1"}],
            "ebook": [{"ebook": {"deliverableId": "d2"}}]}}
        client = self._client(data)
        records = client.inventory()
        self.assertEqual(2, len(records))
        self.assertEqual({"d1", "d2"},
                         {record["tolino_id"] for record in records})

    def test_an_id_is_found_in_nested_lists_and_meta_data(self):
        """``epubMetaData.deliverable`` ist eine Liste -- und ein Kauf-Ebook

        traegt statt der Deliverable-Kennung nur ``identifier``. Beides darf
        nicht als «nicht mehr in der Cloud» gelten.
        """
        client = self._client({"PublicationInventory": {"ebook": [
            {"epubMetaData": {"title": "T",
                              "deliverable": [{"deliverableId": "d5"}]}}]}})
        self.assertEqual(["d5"], [record["tolino_id"]
                                   for record in client.inventory()])
        # Nur ``identifier``: lesbar, aber keine Loesch-/Download-Grundlage.
        bought = self._client({"PublicationInventory": {"ebook": [
            {"resellerId": 3,
             "epubMetaData": {"identifier": "978-3-16-148410-0",
                              "title": "Gekauft"}}]}})
        bought_records = bought.inventory()
        self.assertEqual("", bought_records[0]["tolino_id"])
        self.assertFalse(bought_records[0]["has_deliverable_id"])

    def test_an_unreadable_envelope_never_looks_like_an_empty_library(self):
        for data in ({}, {"unexpected": "shape"},
                     {"PublicationInventory": {}},
                     {"PublicationInventory": "leer"},
                     {"PublicationInventory": None},
                     {"PublicationInventory": 17}):
            with self.subTest(data=data):
                client = self._client(data)
                self.assertEqual([], client.inventory())
                self.assertFalse(client.inventory_ok)
        # Der Grund nennt die gesehenen Felder -- nie deren Werte.
        client = self._client({"unexpected": "shape"})
        client.inventory()
        self.assertFalse(client.inventory_ok)

    def test_a_record_without_any_id_blocks_only_the_deletion(self):
        client = self._client({"PublicationInventory": {"edata": [
            {"deliverableId": "d1"},
            {"etwas": "anderes"}]}})
        self.assertEqual(2, len(client.inventory()))
        self.assertEqual(1, sum(not record["has_deliverable_id"]
                                for record in client.inventory()))
        self.assertEqual({"d1"}, {record["tolino_id"]
                                   for record in client.inventory()
                                   if record["tolino_id"]})

    def test_a_record_with_its_own_ebook_key_is_not_a_container(self):
        """Ein Datensatz mit ``ebook``/``edata`` behaelt seine Felder."""
        client = self._client({"PublicationInventory": {"edata": [
            {"ebook": {"hinweis": True}, "deliverableId": "d7",
             "epubMetaData": {"title": "Titel"}}]}})
        records = client.inventory()
        self.assertEqual(1, len(records))
        self.assertEqual("Titel", records[0]["title"])
        self.assertTrue(records[0]["has_deliverable_id"])
        self.assertEqual("d7", client.inventory()[0]["tolino_id"])

    def test_the_client_returns_normalised_records_only(self):
        """EINE Form fuer Vergleich und sichere Loeschlogik."""
        client = self._client({"PublicationInventory": {"ebook": [
            {"deliverableId": "d1",
             "epubMetaData": {"title": "T"}},
            {"epubMetaData": {"identifier": "978-3-16-148410-0",
                              "title": "K"}}]}})
        records = client.inventory()
        self.assertEqual(
            ["authors", "has_deliverable_id", "identifier", "isbn",
             "title", "tolino_id", "uuid"], sorted(records[0]))
        self.assertTrue(records[0]["has_deliverable_id"])
        self.assertEqual("d1", records[0]["tolino_id"])
        self.assertFalse(records[1]["has_deliverable_id"])
        self.assertEqual("", records[1]["tolino_id"])
        self.assertEqual("978-3-16-148410-0", records[1]["identifier"])

    def test_an_identifier_only_record_makes_the_ids_incomplete(self):
        """Keine Deliverable-Kennung heisst: kein Beweis fuer Loeschungen.

        ``inventory_ids()`` gibt die ISBN bewusst NICHT zurueck -- sonst
        gaelte eine nicht gefundene Zuordnung als geloescht, obwohl die
        Kopie (nur unter ``identifier`` lesbar) weiter existiert.
        """
        client = self._client({"PublicationInventory": {"ebook": [
            {"epubMetaData": {"identifier": "978-3-16-148410-0",
                              "title": "Gekauft"}}]}})
        records = client.inventory()
        self.assertEqual("", records[0]["tolino_id"])
        self.assertFalse(records[0]["has_deliverable_id"])

class ReadStatusUnknownTests(unittest.TestCase):
    """P2: «unbekannt» ist weder gelesen noch ungelesen."""

    @staticmethod
    def _metadata():
        return {1: {"uuid": "u1", "title": "Buch", "authors": "Autor",
                    "formats": ["EPUB"], "last_modified": "1"}}

    def _rows(self, read_ids):
        return compare_inventory(
            self._metadata(), {},
            [{"id": "t1", "title": "Buch", "authors": "Autor"}],
            ["EPUB"], read_ids=read_ids)

    def test_failed_fetch_marks_the_status_unknown(self):
        row = self._rows(None)[0]
        self.assertFalse(row["read_known"])
        self.assertFalse(row["read_in_cloud"])
        self.assertFalse(row["read"])

    def test_unknown_is_not_the_same_as_nothing_read(self):
        row = self._rows(()) [0]
        self.assertTrue(row["read_known"])
        self.assertFalse(row["read_in_cloud"])

    def test_a_known_read_state_still_prechecks(self):
        row = self._rows({"t1"})[0]
        self.assertTrue(row["read_known"])
        self.assertTrue(row["read_in_cloud"])

    def test_cloud_only_rows_are_unknown_too(self):
        rows = compare_inventory(
            {}, {}, [{"id": "t9", "title": "Nur Cloud"}], ["EPUB"],
            read_ids=None)
        self.assertFalse(rows[0]["read_known"])

    def test_prepare_worker_reports_an_unknown_read_state(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal": worker_base_tests.CollectionReconcileWorkerTests
                     ._signal_descriptor()[0]})
        captured, payloads, logged = [], [], []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"

            def login(self):
                return "rot"

            def inventory(self):
                return []

            def read_ids(self):
                raise RuntimeError("Netzfehler mit tok-A")

        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok-A", "state": {},
                    "preferred_formats": ["EPUB"],
                    "sync_column_enabled": False}
        try:
            with patch("calibre_plugin.client.TolinoClient", FakeClient), \
                 patch("calibre_plugin.ui_workers.log_warning",
                       lambda text: logged.append(text)), \
                 patch("calibre_plugin.ui_workers.compare_inventory",
                       lambda *args, **kwargs: captured.append(kwargs) or []):
                worker = ui_module.workers.PrepareWorker(settings, {})
                worker.ready.connect(payloads.append)
                worker.failed.connect(lambda text: payloads.append(text))
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        self.assertFalse(payloads[0]["read_status_known"])
        self.assertIsNone(captured[0]["read_ids"])
        # Der technische Grund steht im Log, aber ohne den Token.
        self.assertTrue(any("Netzfehler" in line for line in logged))
        self.assertNotIn("tok-A", " ".join(logged))

    def test_the_dialog_keeps_unknown_read_states_untouched(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub=ui_base_tests.InventoryDialogBuildTests._qt_stub())
        try:
            dialog = ui_module.dialogs.InventoryDialog([
                {"title": "Buch", "authors": "A", "status": "identical",
                 "book_id": 1, "tolino_id": "t1", "read_known": False,
                 "read_in_cloud": False,
                 "explanation": "In beiden vorhanden."}], options={})
            self.assertEqual(1, len(dialog.read_checks))
            self.assertFalse(dialog.read_checks[0].enabled)
            self.assertFalse(dialog.read_checks[0].isChecked())
            self.assertIn("unbekannt", dialog.read_checks[0].tooltip)
            self.assertIn("Lesestatus unbekannt",
                          dialog.table.items[(0, 7)])
            self.assertIn("Lesestatus unbekannt", dialog.note.text())
            # Nichts wird als Änderung gezählt oder zurückgeschrieben.
            self.assertEqual([], dialog.selected_read_targets())
            self.assertEqual([], dialog.selected_read_unmarks())
            self.assertFalse(dialog._set_row_choice(0, "read", True))
        finally:
            ui_module._restore_stubs_for_tests()

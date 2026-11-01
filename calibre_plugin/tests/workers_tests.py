"""Hintergrundarbeit: Sync, Bestand und Keep-alive."""

from ..comparison import ReadState
import calibre_plugin.tolino as tolino_module
import calibre_plugin.token_vault as vault_module
import unittest
from . import ui_base_tests, worker_base_tests
from .. import config
from ..identifiers import display_title
from ..library import TOLINO_PROGRESS_COLUMN, TOLINO_READ_COLUMN
from ..plan import MetaUpdateRecord, plan_sync
from ..planning_values import SyncJob
from ..run_context import RunContext
from ..tolino import TolinoApiError, TolinoAuthError, TolinoClient
from ._support import (compare_inventory, parsed_inventory, start_cdp_stubs,
                       stop_cdp_stubs, ui_module_for)
from unittest.mock import patch


# Der Einzellauf richtet die CDP-Stubs selbst ein. Die Worker-Tests
# arbeiten mit Fake-Clients; der Vault findet dort einen gespeicherten
# Zugang, ohne dass jeder Test ein Konto anlegen muss.
_ACCOUNT_PATCH = None


def setUpModule():
    global _ACCOUNT_PATCH
    start_cdp_stubs()
    _ACCOUNT_PATCH = patch(
        "calibre_plugin.token_vault.account_by_name",
        lambda _name: {"refresh_token": "tok", "hardware_id": "hw"})
    _ACCOUNT_PATCH.start()


def tearDownModule():
    global _ACCOUNT_PATCH
    if _ACCOUNT_PATCH is not None:
        _ACCOUNT_PATCH.stop()
        _ACCOUNT_PATCH = None
    stop_cdp_stubs()
def _changes(read_marks=(), read_unmarks=()):
    """Lesestatus-Änderungen als (tolino-ID, Calibre-Nummer, gelesen?)."""

    return ([(tolino_id, None, True) for tolino_id in read_marks or ()]
            + [(tolino_id, None, False) for tolino_id in read_unmarks or ()])


def _read_ids(dialog, finished):
    """tolino-IDs der gewählten Lesestatus-Änderungen (True = gelesen)."""
    return [tolino_id for tolino_id, _book, state, _uuid
            in dialog.selected_read_changes() if state is finished]


class _ReadingDb:
    """Minimale Calibre-DB: UUID -> aktuelle Buchnummer plus Lesespalten."""

    def __init__(self, books, read=None, progress=None):
        self.books = dict(books)
        self.field_metadata = {
            TOLINO_READ_COLUMN: {"datatype": "bool"},
            TOLINO_PROGRESS_COLUMN: {"datatype": "int"},
        }
        self.values = {
            TOLINO_READ_COLUMN: dict(read or {}),
            TOLINO_PROGRESS_COLUMN: dict(progress or {}),
        }
        self.writes = []
        self.new_api = self

    def all_book_ids(self):
        return list(self.books)

    def get_metadata(self, book_id):
        return {"uuid": self.books[book_id]}

    def has_id(self, book_id):
        return book_id in self.books

    def field_for(self, column, book_id):
        return self.values.get(column, {}).get(book_id)

    def set_field(self, column, values):
        self.values.setdefault(column, {}).update(values)
        self.writes.append((column, dict(values)))


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
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                 patch("calibre_plugin.ui_workers.save_account",
                       lambda *args, **kwargs: None):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(settings), [], [("u-1", "t-1")],
                    [], {}, state=settings.get("state"))
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
        return SyncJob(book_id, uuid, "EPUB", None,
                       "/library/%s.epub" % uuid, cover, title)

    def _run(self, ui_module, settings, jobs, fake_client,
             removals=(), meta_updates=(), read_marks=(), read_unmarks=(),
             series_map=None):
        emitted = {"completed": None, "failed": None, "progress": []}
        saved, credentials = [], []
        with patch("calibre_plugin.token_vault.TolinoClient", fake_client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, **_kwargs: saved.append(
                       (name, values))), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args: credentials.append(args)):
            worker = ui_module.workers.SyncWorker(
                RunContext.from_values(settings), jobs, removals,
                meta_updates, series_map or {},
                _changes(read_marks, read_unmarks),
                state=settings.get("state"))
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

    def test_same_id_replacement_is_not_deleted(self):
        ui_module = self._ui()
        deletes = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def upload(self, path):
                return "same-id"

            def delete(self, tolino_id):
                deletes.append(tolino_id)

        state = {"u1": {"tolino_id": "same-id", "calibre_id": 1}}
        job = SyncJob(1, "u1", "EPUB", "same-id", "/book", None, "A")
        worker, emitted, _saved, _credentials = self._run(
            ui_module, self._settings(state), [job], FakeClient)
        ui_module._restore_stubs_for_tests()
        self.assertEqual([], deletes)
        self.assertEqual("same-id", worker.state["u1"]["tolino_id"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertIsNone(emitted["failed"])

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

    def test_successful_deletion_is_persisted_before_next_cloud_change(self):
        ui_module = self._ui()
        calls = []
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": "t2"}}

        class FakeClient:
            def delete(self, tolino_id):
                calls.append(("delete", tolino_id))

        worker = ui_module.workers.SyncWorker(
            RunContext.from_values(self._settings(state)), [],
            [("u1", "t1"), ("u2", "t2")], state=state)
        with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                   return_value=FakeClient()), \
             patch("calibre_plugin.ui_workers.save_account",
                   side_effect=lambda _name, values, **_kw: calls.append(
                       ("save", tuple(sorted(values["state"]))))):
            worker.run()
        self.assertEqual([("delete", "t1"), ("save", ("u2",)),
                          ("delete", "t2"), ("save", ())], calls)

    def test_a_broken_state_entry_does_not_lose_a_confirmed_upload(self):
        ui_module = self._ui()
        state = {"u1": "kaputt"}
        saved = []

        class FakeClient:
            def upload(self, path):
                return "new-1"

        worker = ui_module.workers.SyncWorker(
            RunContext.from_values(self._settings(state)),
            [SyncJob(1, "u1", "EPUB", None, "/book", None, "A")], [],
            state=state)
        with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                   return_value=FakeClient()), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda _name, values, **_kw: saved.append(values["state"])):
            worker.run()
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual({"u1": {"tolino_id": "new-1", "calibre_id": 1}},
                         saved[0])

    def test_persistence_failure_stops_after_confirmed_cloud_change(self):
        ui_module = self._ui()
        calls, emitted = [], {}

        class FakeClient:
            def upload(self, path):
                calls.append(("upload", path))
                return "new-1"

            def delete(self, tolino_id):
                calls.append(("delete", tolino_id))

        for mode in ("upload", "delete"):
            with self.subTest(mode=mode):
                calls.clear()
                emitted.clear()
                jobs = ([SyncJob(1, "u1", "EPUB", "old-1", "/book", None, "A"),
                         SyncJob(2, "u2", "EPUB", None, "/next", None, "B")]
                        if mode == "upload" else [])
                removals = [] if jobs else [("u1", "old-1"), ("u2", "old-2")]
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(self._settings({})), jobs, removals,
                    state={"u1": {"tolino_id": "old-1", "calibre_id": 1},
                           "u2": {"tolino_id": "old-2", "calibre_id": 2}})
                worker.failed.connect(lambda message: emitted.update(failed=message))
                worker.completed.connect(lambda *args: emitted.update(completed=args))
                with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                           return_value=FakeClient()), \
                     patch("calibre_plugin.ui_workers.save_account",
                           side_effect=OSError("Platte voll tok-A")):
                    worker.run()
                self.assertEqual(1, len(calls))
                self.assertNotIn("completed", emitted)
                self.assertIn("Cloud-Änderungen bleiben bestehen", emitted["failed"])
                self.assertNotIn("tok-A", emitted["failed"])
                self.assertEqual(1, sum(op["status"] == "succeeded"
                                        for op in worker.operations))
                self.assertEqual(1, sum(op["status"] == "not_started"
                                        for op in worker.operations))
                if mode == "upload":
                    self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
                    self.assertEqual([(1, "new-1")], worker.updates)
                else:
                    self.assertNotIn("u1", worker.state)
                    self.assertEqual([(1, "")], worker.updates)

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

            def upload_cover(self, tolino_id, data, suffix=".jpg"):
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

            def upload_cover(self, tolino_id, data, suffix=".jpg"):
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
                meta_updates=[MetaUpdateRecord(1, "u1", "t-1", {"title": "Neu"})])
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
        self.assertEqual(1, sum(operation["status"] == "not_started"
                                for operation in worker.operations))

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
        # Reiner Helfer aus identifiers -- keine Qt-Naht mehr nötig.
        self.assertEqual("Buch 7", display_title("", 7))
        self.assertEqual("Buch ohne Titel", display_title("", ""))
        self.assertEqual("Titel", display_title(" Titel "))
        worker = ui_module.workers.SyncWorker(
            RunContext.from_values(self._settings({})),
            [self._job(ui_module, 7, "u7")], [], state={})
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
        with patch("calibre_plugin.token_vault.TolinoClient", lambda *a, **k: fake), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, **_kwargs: saved.append(
                       (name, values))):
            worker = ui_module.workers.SyncWorker(
                RunContext.from_values(self._settings(state)),
                [self._job(ui_module, 1, "u1"), self._job(ui_module, 2, "u2")],
                [], state=state)
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
        self.assertFalse(any(operation["status"] == "not_started"
                             for operation in worker.operations))

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

        dashboard = type("Dashboard", (), {
            "thread": None, "worker": None, "prepare_thread": None,
            "prepare_worker": None, "login_thread": None,
            "login_worker": None})()
        dashboard.account_name = "B"
        dashboard.refresh = FakeField("tok-B")
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.finish_thread = lambda: None
        worker = type("Worker", (), {})()
        worker.account_name = "A"
        worker.context = type("Context", (), {
            "columns": {"id": True, "read": False}})()
        worker.outcome = {"uploaded": 1, "updated": 0, "skipped": 0, "failed": 0}
        worker.state = {"u1": {"tolino_id": "new-1"}}
        worker.updates = [(1, "new-1")]
        dashboard.worker = worker
        with patch("calibre_plugin.ui_dashboard.save_account",
                   lambda name, values, **_kwargs: saved.append((name, values))), \
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
        dashboard = type("Dashboard", (), {
            "thread": None, "worker": None, "prepare_thread": None,
            "prepare_worker": None, "login_thread": None,
            "login_worker": None})()
        dashboard.account_name = "A"
        dashboard.refresh = type("F", (), {"text": lambda self: "tok",
                                           "setText": lambda self, v: None})()
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.columns = {"id": False, "read": False}
        dashboard.finish_thread = lambda: None
        worker = type("Worker", (), {})()
        worker.account_name = "A"
        worker.cancelled = True
        worker.state = {"u1": {"tolino_id": "new-1"}}
        worker.updates = [(1, "new-1")]
        worker.outcome = {"uploaded": 1, "updated": 0, "skipped": 0, "failed": 0, "cancelled": True}
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

            def progress_by_id(self):
                return {"t-1": {"percent": 33, "current": "192",
                                 "last": "590"}}

        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-A", "refresh_token": "tok-A", "state": {},
                    "preferred_formats": ["EPUB"],
                    "columns": {"id": False}}
        payloads = []
        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
             patch("calibre_plugin.config.account_by_name", lambda _name: None), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args: credentials.append(args)), \
             patch("calibre_plugin.ui_workers.compare_inventory",
                   lambda *args, **kwargs: captured.append(
                       (args, kwargs)) or [{"status": "review"}]):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values(settings), {1: {"title": "A"}},
                state=settings.get("state"))
            worker.ready.connect(payloads.append)
            worker.failed.connect(lambda text: payloads.append(text))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        payload = payloads[0]
        self.assertEqual("A", payload["context"].account_name)
        self.assertEqual([{"status": "review"}], payload["comparison"])
        self.assertNotIn("refresh", payload)
        self.assertFalse(hasattr(payload["context"], "refresh_token"))
        # Der Vergleich bekommt die Einstellungen (Vergleichsfelder,
        # Tolino-ID-Spalte) mit.
        _args, kwargs = captured[0]
        # Der Vergleich bekommt den Metadaten-Snapshot unverändert mit ...
        self.assertEqual({1: {"title": "A"}}, _args[0])
        # ... und den Lesestatus sowie den Fortschritt als eigene Eingänge.
        self.assertIn("read_ids", kwargs)
        self.assertEqual({"percent": 33, "current": "192", "last": "590"},
                         kwargs["progress_by_id"]["t-1"])
        self.assertTrue(payload["progress_status_known"])

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
        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values({
                    "account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "preferred_formats": ["EPUB"]}), {}, state={})
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
            def login(self):
                return "rot"

            def inventory(self):
                raise ui_module.workers.TolinoApiError(
                    "Die Cloud-Antwort enthält kein PublicationInventory.",
                    code="N02")

        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values({
                    "account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "preferred_formats": ["EPUB"]}), {}, state={})
            worker.failed.connect(messages.append)
            worker.ready.connect(lambda payload: messages.append("ready"))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(messages))
        self.assertIn("enthält kein PublicationInventory", messages[0])
        self.assertIn("N02", messages[0])
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_complete_inventory_clears_stale_cloud_id_in_worker_state(self):
        class FakeClient:
            refresh = "rot"
            hardware = "hw"

            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return self.refresh

            def inventory(self):
                return inventory

            def read_ids(self):
                return set()

        metadata = {1: {"uuid": "u-stale", "title": "A",
                        "formats": ["EPUB"]}}
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "state": {"u-stale": {"tolino_id": "gone"}},
                    "preferred_formats": ["EPUB"]}
        for inventory in ([], parsed_inventory([{}])):
            with self.subTest(inventory=inventory):
                ui_module = self._ui()
                payloads = []
                try:
                    with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
                        worker = ui_module.workers.PrepareWorker(
                            RunContext.from_values(settings), metadata,
                            state=settings.get("state"))
                        worker.ready.connect(payloads.append)
                        worker.run()
                    self.assertEqual(1, len(payloads))
                    self.assertNotIn("tolino_id", payloads[0]["state"]["u-stale"])
                finally:
                    ui_module._restore_stubs_for_tests()

    def test_a_partly_readable_inventory_still_compares(self):
        """Einzelne nicht zuordenbare Cloud-Datensätze verhindern den Vergleich nicht."""
        ui_module = self._ui()
        payloads, compared = [], []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"

            def login(self):
                return "rot"

            def inventory(self):
                return parsed_inventory([{"deliverableId": "d1"}])

            def read_ids(self):
                return set()

        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                patch("calibre_plugin.ui_workers.compare_inventory",
                      lambda *args, **kwargs: compared.append(args[2]) or []):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values({
                    "account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "preferred_formats": ["EPUB"]}), {}, state={})
            worker.failed.connect(payloads.append)
            worker.ready.connect(payloads.append)
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        self.assertIsInstance(payloads[0], dict)
        self.assertEqual([[{"tolino_id": "d1",
                            "title": "", "authors": "", "isbn": ""}]],
                         compared)
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
        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values({
                    "account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "preferred_formats": ["EPUB"]}), {}, state={})
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
                RunContext.from_values({
                    "account_name": "A", "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "preferred_formats": ["EPUB"]}), {}, state={},
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
        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                patch("calibre_plugin.token_vault.account_by_name",
                      lambda name: {"name": "A", "refresh_token": "neu-token",
                                    "hardware_id": "hw-neu"}):
            worker = ui_module.workers.PrepareWorker(
                RunContext.from_values(settings), {},
                state=settings.get("state"))
            worker.run()
        ui_module._restore_stubs_for_tests()
        self.assertEqual([("neu-token", "hw-neu")], seen)
        self.assertEqual(0, int(vault_module.TOKEN_VAULT._lock.locked()))

    def test_client_factory_persists_the_rotated_token(self):
        ui_module = self._ui()
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-A", "refresh_token": "tok-A"}
        saved = []

        class FakeClient:
            def __init__(self, partner_id, hardware, refresh,
                         token_callback=None, hardware_callback=None):
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
            context = RunContext.from_values(settings)
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                 patch("calibre_plugin.token_vault.account_by_name",
                       lambda _name: {"refresh_token": "tok-A",
                                      "hardware_id": "hw-A"}), \
                 patch("calibre_plugin.token_vault.save_refresh_token",
                       lambda name, token: saved.append((name, token))):
                dashboard._worker_client(context)
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("A", "rot-A")], saved)

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
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(settings), [], [],
                    state=settings.get("state"))
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
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(settings), [], [],
                    meta_updates=[MetaUpdateRecord(1, "u1", "t1", {"title": "A"})],
                    state=settings.get("state"))
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
        accounts = [{"name": "A", "partner_id": 4, "hardware_id": "hw",
                     "refresh_token": "tok"}]
        with patch("calibre_plugin.token_vault.TolinoClient") as client_mock, \
                patch("calibre_plugin.token_vault.settings",
                      lambda: {"accounts": accounts}), \
                patch("calibre_plugin.token_vault.account_by_name",
                      lambda _name: accounts[0]):
            with vault_module.TOKEN_VAULT._lock:
                result = vault_module.TOKEN_VAULT.keepalive_refresh()
            self.assertEqual(0, result)
            client_mock.assert_not_called()
            self.assertEqual(1, vault_module.TOKEN_VAULT.keepalive_refresh())
        client_mock.assert_called_once()

    def test_token_vault_serializes_refreshes_and_releases_on_failure(self):
        from ..token_vault import TOKEN_VAULT
        with TOKEN_VAULT._lock:
            with self.assertRaises(TolinoAuthError) as caught:
                vault_module.TOKEN_VAULT.login("A", 4, lock_timeout=0)
        self.assertEqual("S91", caught.exception.code)

        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                raise RuntimeError("kaputt")

        with patch("calibre_plugin.token_vault.TolinoClient", FailingClient), \
                patch("calibre_plugin.token_vault.account_by_name",
                      lambda _name: {"refresh_token": "tok",
                                     "hardware_id": "hw"}):
            with self.assertRaises(RuntimeError):
                vault_module.TOKEN_VAULT.login("A", 4)
        self.assertFalse(TOKEN_VAULT._lock.locked())

    def test_a_successful_rotation_is_logged(self):
        """Ohne Erfolgszeile ist im Log nicht zu sehen, ob er laeuft."""
        logged = []

        class FakeClient(object):
            def __init__(self, *args, **kwargs):
                self.refresh = "neu"

            def login(self):
                return self.refresh

        accounts = [{"name": "A", "partner_id": 4, "hardware_id": "hw",
                     "refresh_token": "alt"}]
        with patch("calibre_plugin.token_vault.log_info", logged.append), \
                patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                patch("calibre_plugin.token_vault.settings",
                      lambda: {"accounts": accounts}), \
                patch("calibre_plugin.token_vault.account_by_name",
                      lambda _name: accounts[0]):
            result = vault_module.TOKEN_VAULT.keepalive_refresh()
        self.assertEqual(1, result)
        self.assertTrue(any("Keep-alive" in line for line in logged))

    def test_keepalive_failure_is_logged_without_the_token(self):
        logged = []

        class BoomClient(object):
            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                raise RuntimeError("kaputt mit refresh_token=sk-geheim-token")

        accounts = [{"name": "A", "partner_id": 4, "hardware_id": "hw",
                     "refresh_token": "sk-geheim-token"}]
        with patch("calibre_plugin.token_vault.log_warning", logged.append), \
                patch("calibre_plugin.token_vault.TolinoClient", BoomClient), \
                patch("calibre_plugin.token_vault.settings",
                      lambda: {"accounts": accounts}), \
                patch("calibre_plugin.token_vault.account_by_name",
                      lambda _name: accounts[0]):
            result = vault_module.TOKEN_VAULT.keepalive_refresh()
        self.assertEqual(0, result)
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
        """A01 vor jedem Netzaufruf -- wie die Dialogpruefung."""
        from ..token_vault import TOKEN_VAULT
        self._ui()
        with patch("calibre_plugin.token_vault.account_by_name",
                   lambda *_args, **_kwargs: {}):
            with self.assertRaises(tolino_module.TolinoAuthError) as caught:
                TOKEN_VAULT.login("konto", 4)
        self.assertEqual("A01", caught.exception.code)

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
        with patch("calibre_plugin.token_vault.TolinoClient",
                   lambda *args, **kwargs: client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda name, values, **_kwargs: saved.append(values)), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args, **kwargs: None):
            worker = ui_module.workers.SyncWorker(
                RunContext.from_values(settings), jobs, [("u1", "t1")],
                state=settings.get("state"))
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
        job = SyncJob(1, "u1", "EPUB", "t-alt",
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
        job = SyncJob(1, "u1", "EPUB", "t-alt",
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
        job = SyncJob(1, "u1", "EPUB", "t-alt",
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

    def test_an_unreadable_envelope_never_looks_like_an_empty_library(self):
        for data in ({}, {"unexpected": "shape"},
                     {"PublicationInventory": {}},
                     {"PublicationInventory": "leer"},
                     {"PublicationInventory": None},
                     {"PublicationInventory": 17}):
            with self.subTest(data=data):
                client = self._client(data)
                with self.assertRaises(TolinoApiError):
                    client.inventory()
        # Der Grund nennt die gesehenen Felder -- nie deren Werte.
        client = self._client({"unexpected": "shape"})
        with self.assertRaises(TolinoApiError):
            client.inventory()

    def test_invalid_entries_without_a_record_do_not_look_empty(self):
        for data in ({"PublicationInventory": {"edata": [None, "x"]}},
                     {"PublicationInventory": {"ebook": [17]}}):
            with self.subTest(data=data):
                with self.assertRaises(TolinoApiError):
                    self._client(data).inventory()

    def test_a_record_without_any_id_blocks_only_the_deletion(self):
        client = self._client({"PublicationInventory": {"edata": [
            {"deliverableId": "d1"},
            {"etwas": "anderes"}]}})
        self.assertEqual(2, len(client.inventory()))
        self.assertEqual(1, sum(not record["tolino_id"]
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
        self.assertEqual("d7", records[0]["tolino_id"])

    def test_the_client_returns_normalised_records_only(self):
        """EINE Form fuer Vergleich und sichere Loeschlogik."""
        client = self._client({"PublicationInventory": {"ebook": [
            {"deliverableId": "d1",
             "epubMetaData": {"title": "T"}},
            {"epubMetaData": {"identifier": "978-3-16-148410-0",
                              "title": "K"}}]}})
        records = client.inventory()
        self.assertEqual(
            ["authors", "isbn", "title", "tolino_id"], sorted(records[0]))
        self.assertEqual("d1", records[0]["tolino_id"])
        self.assertEqual("", records[1]["tolino_id"])
        self.assertEqual("9783161484100", records[1]["isbn"])

    def test_identifier_embedded_isbn_is_normalized_for_matching(self):
        client = self._client({"PublicationInventory": {"ebook": [
            {"epubMetaData": {
                "identifier": "DT0400.9783832170141_A38978050",
                "title": "Gekauft"}}]}})
        records = client.inventory()
        self.assertEqual("", records[0]["tolino_id"])
        self.assertEqual("9783832170141", records[0]["isbn"])

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

class ReadStatusUnknownTests(unittest.TestCase):
    """P2: «unbekannt» ist weder gelesen noch ungelesen."""

    @staticmethod
    def _metadata():
        return {1: {"uuid": "u1", "title": "Buch", "authors": "Autor",
                    "formats": ["EPUB"], "last_modified": "1"}}

    def _rows(self, read_ids):
        return compare_inventory(
            self._metadata(), {},
            [{"deliverableId": "t1", "title": "Buch", "authors": "Autor"}],
            ["EPUB"], read_ids=read_ids)

    def test_failed_fetch_marks_the_status_unknown(self):
        row = self._rows(None)[0]
        self.assertFalse(row["read"].known)
        self.assertFalse(row["read"].cloud)
        self.assertFalse(row["read"])

    def test_unknown_is_not_the_same_as_nothing_read(self):
        row = self._rows(()) [0]
        self.assertTrue(row["read"].known)
        self.assertFalse(row["read"].cloud)

    def test_a_known_read_state_still_prechecks(self):
        row = self._rows({"t1"})[0]
        self.assertTrue(row["read"].known)
        self.assertTrue(row["read"].cloud)

    def test_cloud_only_rows_are_unknown_too(self):
        rows = compare_inventory(
            {}, {}, [{"deliverableId": "t9", "title": "Nur Cloud"}], ["EPUB"],
            read_ids=None)
        self.assertFalse(rows[0]["read"].known)

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
                    "columns": {"id": False}}
        try:
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
                 patch("calibre_plugin.run_context.account_secrets",
                       lambda _name: ("tok-A",)), \
                 patch("calibre_plugin.ui_workers.log_warning",
                       lambda text: logged.append(text)), \
                 patch("calibre_plugin.ui_workers.compare_inventory",
                       lambda *args, **kwargs: captured.append(kwargs) or []):
                worker = ui_module.workers.PrepareWorker(
                RunContext.from_values(settings), {},
                state=settings.get("state"))
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
                 "book_id": 1, "tolino_id": "t1",
                 "read": ReadState(known=False),
                 "explanation": "In beiden vorhanden."}], options={})
            self.assertFalse(dialog.table.item(0, 2).flags() &
                             ui_module.dialogs.Qt.ItemFlag.ItemIsEnabled)
            self.assertFalse(dialog.table.item(0, 2).isChecked())
            self.assertIn("unbekannt", dialog.table.item(0, 2).toolTip())
            self.assertIn("Lesestatus unbekannt",
                          dialog.table.items[(0, 7)])
            self.assertIn("Lesestatus unbekannt", dialog.note.text())
            # Nichts wird als Änderung gezählt oder zurückgeschrieben.
            self.assertEqual([], _read_ids(dialog, True))
            self.assertEqual([], _read_ids(dialog, False))
            self.assertFalse(dialog._set_row_choice(0, "read", True))
        finally:
            ui_module._restore_stubs_for_tests()


class KeepaliveStopTests(unittest.TestCase):
    """Der Keep-alive-Thread endet im Plugin-Shutdown, nicht erst im atexit."""

    def test_stop_ends_a_running_keepalive_thread(self):
        from .. import token_vault
        token_vault.stop_token_keepalive()
        thread = token_vault.start_token_keepalive(interval=3600)
        try:
            self.assertTrue(thread.is_alive())
            self.assertIsNone(token_vault.start_token_keepalive(interval=3600))
        finally:
            token_vault.stop_token_keepalive()
        self.assertFalse(thread.is_alive())

    def test_a_stuck_thread_keeps_its_slot_and_reference(self):
        """Nach dem Join-Timeout darf kein zweiter Keep-alive starten."""
        import threading
        from .. import token_vault
        token_vault.stop_token_keepalive()

        class StuckThread:
            def __init__(self):
                self.joins = []

            @staticmethod
            def is_alive():
                return True

            def join(self, timeout=None):
                self.joins.append(timeout)

        stuck = StuckThread()
        token_vault._KEEPALIVE_THREAD = stuck
        token_vault._KEEPALIVE_STOP = threading.Event()
        try:
            self.assertFalse(token_vault.stop_token_keepalive(timeout=0))
            self.assertIs(stuck, token_vault._KEEPALIVE_THREAD)
            self.assertIsNone(token_vault.start_token_keepalive(interval=3600))
        finally:
            token_vault._KEEPALIVE_THREAD = None
            token_vault._KEEPALIVE_STOP = None


class MultiAccountReadingStateTests(unittest.TestCase):
    """Lesewerte folgen dem Konto, nicht der gemeinsam genutzten Spalte."""

    UUID = "11111111-1111-1111-1111-111111111111"
    OTHER_UUID = "22222222-2222-2222-2222-222222222222"

    @staticmethod
    def _ui():
        return ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal": worker_base_tests
                     .CollectionReconcileWorkerTests
                     ._signal_descriptor()[0]})

    @classmethod
    def _metadata(cls):
        return {1: {"uuid": cls.UUID, "title": "Buch", "authors": "Autor",
                    "formats": ["EPUB"], "last_modified": "1"}}

    @staticmethod
    def _inventory():
        return parsed_inventory([{"deliverableId": "t1", "title": "Buch",
                                  "authors": "Autor"}])

    @staticmethod
    def _reading(read, percent, current="192", last="590"):
        return {"read": read, "progress": {
            "percent": percent, "current": current, "last": last,
            "position": "Ops/017.html#point(/1/4/74:0)"}}

    def _accounts(self, states):
        original = config.PREFERENCES
        self.addCleanup(setattr, config, "PREFERENCES", original)
        config.PREFERENCES = config.JSONConfig("test")
        for name, state in states.items():
            config.save_account(name, {"state": state}, active=name)

    def _prepare(self, account, state, read_ids, progress,
                 fail_read=False):
        ui_module = self._ui()
        payloads, failures = [], []
        inventory = self._inventory()
        metadata = self._metadata()

        class FakeClient:
            hardware = "hw"
            refresh = "rot"

            def __init__(self, *args, **kwargs):
                pass

            def login(self):
                return "rot"

            def inventory(self):
                return inventory

            def read_ids(self):
                if fail_read:
                    raise RuntimeError("Netz weg")
                return set(read_ids or ())

            def progress_by_id(self):
                return dict(progress or {})

        settings = {"account_name": account, "partner_id": 4,
                    "hardware_id": "hw", "refresh_token": "tok",
                    "state": state, "preferred_formats": ["EPUB"]}
        try:
            with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                       lambda *args, **kwargs: FakeClient()), \
                 patch("calibre_plugin.config.save_credentials",
                       lambda *args, **kwargs: None):
                worker = ui_module.workers.PrepareWorker(
                    RunContext.from_values(
                        settings, {"read": True, "progress": True}),
                    metadata, state=state)
                worker.ready.connect(payloads.append)
                worker.failed.connect(failures.append)
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([], failures)
        self.assertEqual(1, len(payloads))
        return payloads[0]

    def test_two_accounts_keep_read_and_progress_independent(self):
        a_state = {self.UUID: {"tolino_id": "t1", "calibre_id": 1}}
        b_state = {self.UUID: {"tolino_id": "t1", "calibre_id": 1}}
        self._accounts({"A": a_state, "B": b_state})
        self._prepare("A", a_state, {"t1"}, {
            "t1": {"percent": 100, "current": "350", "last": "350",
                   "position": "Ops/A.html"}})
        self._prepare("B", b_state, set(), {
            "t1": {"percent": 37, "current": "130", "last": "350",
                   "position": "Ops/B.html"}})
        a_saved = config.account_by_name("A")["state"][self.UUID]
        b_saved = config.account_by_name("B")["state"][self.UUID]
        self.assertIs(True, a_saved["reading"]["read"])
        self.assertEqual(100, a_saved["reading"]["progress"]["percent"])
        self.assertIs(False, b_saved["reading"]["read"])
        self.assertEqual(37, b_saved["reading"]["progress"]["percent"])
        self.assertEqual("t1", a_saved["tolino_id"])
        self.assertEqual("t1", b_saved["tolino_id"])
        # Das Speichern des Vergleichs schaltet das Konto nicht um.
        self.assertEqual("B", config.settings()["active_account"])

    def test_comparison_result_stays_with_its_account_after_a_switch(self):
        a_state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": self._reading(False, 37)}}
        b_state = {self.UUID: {
            "tolino_id": "t1-b", "calibre_id": 1,
            "reading": self._reading(False, 37)}}
        self._accounts({"A": a_state, "B": b_state})
        # B ist aktiv, während der Vergleich für A fertig wird.
        self._prepare("A", a_state, {"t1"}, {
            "t1": {"percent": 100, "current": "350", "last": "350",
                   "position": "Ops/A.html"}})
        self.assertIs(True, config.account_by_name("A")["state"]
                      [self.UUID]["reading"]["read"])
        self.assertIs(False, config.account_by_name("B")["state"]
                      [self.UUID]["reading"]["read"])
        self.assertEqual("B", config.settings()["active_account"])

    def test_old_state_keeps_ids_fingerprints_and_collections(self):
        old = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "content_fp": "content-fingerprint",
            "meta_fp": "metadata-fingerprint",
            "collections": ["Serie"],
        }}
        self._accounts({"A": old})
        self._prepare("A", old, {"t1"}, {
            "t1": {"percent": 100, "current": "350", "last": "350",
                   "position": "Ops/A.html"}})
        entry = config.account_by_name("A")["state"][self.UUID]
        self.assertEqual("content-fingerprint", entry["content_fp"])
        self.assertEqual("metadata-fingerprint", entry["meta_fp"])
        self.assertEqual(["Serie"], entry["collections"])
        self.assertEqual("t1", entry["tolino_id"])
        self.assertEqual(1, entry["calibre_id"])
        self.assertIs(True, entry["reading"]["read"])
        self.assertEqual(100, entry["reading"]["progress"]["percent"])

    def test_failed_cloud_fetch_keeps_saved_reading(self):
        import copy
        state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": self._reading(True, 100)}}
        self._accounts({"A": state})
        before = copy.deepcopy(config.account_by_name("A")["state"])
        payload = self._prepare("A", state, None, None, fail_read=True)
        self.assertFalse(payload["read_status_known"])
        self.assertFalse(payload["progress_status_known"])
        self.assertEqual(before, config.account_by_name("A")["state"])

    def test_successful_fetch_without_bookmark_stores_none_and_clears_column(
            self):
        state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": self._reading(True, 100)}}
        self._accounts({"A": state})
        self._prepare("A", state, set(), {})
        entry = config.account_by_name("A")["state"][self.UUID]
        self.assertIs(False, entry["reading"]["read"])
        self.assertIsNone(entry["reading"]["progress"]["percent"])

        from ..ui_dashboard import _mirror_account_reading
        db = _ReadingDb({1: self.UUID}, read={1: True}, progress={1: 100})
        dashboard = type("Dashboard", (), {
            "gui": type("Gui", (), {"current_db": db})()})()
        _mirror_account_reading(dashboard, "A")
        self.assertIsNone(db.values[TOLINO_PROGRESS_COLUMN][1])
        self.assertIs(False, db.values[TOLINO_READ_COLUMN][1])

    def test_unknown_stored_values_are_not_written_as_false_or_zero(self):
        from ..ui_dashboard import _mirror_account_reading
        state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": {"read": None, "progress": {"current": "192"}}}}
        self._accounts({"A": state})
        db = _ReadingDb({1: self.UUID}, read={1: True}, progress={1: 100})
        dashboard = type("Dashboard", (), {
            "gui": type("Gui", (), {"current_db": db})()})()
        _mirror_account_reading(dashboard, "A")
        self.assertEqual([], db.writes)
        self.assertIs(True, db.values[TOLINO_READ_COLUMN][1])
        self.assertEqual(100, db.values[TOLINO_PROGRESS_COLUMN][1])

    def test_changed_calibre_id_uses_uuid_not_the_stored_number(self):
        from ..ui_dashboard import _mirror_account_reading
        state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 7,
            "reading": self._reading(True, 100)}}
        self._accounts({"A": state})
        db = _ReadingDb({42: self.UUID, 7: self.OTHER_UUID})
        dashboard = type("Dashboard", (), {
            "gui": type("Gui", (), {"current_db": db})()})()
        _mirror_account_reading(dashboard, "A")
        self.assertIs(True, db.values[TOLINO_READ_COLUMN][42])
        self.assertEqual(100, db.values[TOLINO_PROGRESS_COLUMN][42])
        self.assertNotIn(7, db.values[TOLINO_READ_COLUMN])
        self.assertNotIn(7, db.values[TOLINO_PROGRESS_COLUMN])

    def _manual(self, state, fail=False):
        ui_module = self._ui()
        emitted, calls = {}, []

        class FakeClient:
            def mark_read_many(self, book_ids, finished=True):
                calls.append((list(book_ids), finished))
                if fail:
                    raise RuntimeError("Netz weg")
                return len(book_ids)

        try:
            with patch("calibre_plugin.ui_workers.TOKEN_VAULT.login",
                       lambda *args, **kwargs: FakeClient()):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values({"account_name": "A",
                                            "partner_id": 4}),
                    [], [], [], {},
                    [("t1", 1, True, self.UUID)], state=state)
                worker.completed.connect(
                    lambda *args: emitted.update(completed=args))
                worker.failed.connect(
                    lambda *args: emitted.update(failed=args))
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        return worker, calls

    def test_confirmed_manual_read_change_updates_only_its_account(self):
        a_state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": self._reading(False, 37)}}
        b_state = {self.UUID: {
            "tolino_id": "t1-b", "calibre_id": 1,
            "reading": self._reading(False, 37)}}
        self._accounts({"A": a_state, "B": b_state})
        worker, calls = self._manual(config.account_by_name("A")["state"])
        self.assertEqual([(["t1"], True)], calls)
        self.assertEqual([(1, True)], worker.read_updates)
        self.assertIs(True, config.account_by_name("A")["state"]
                      [self.UUID]["reading"]["read"])
        self.assertIs(False, config.account_by_name("B")["state"]
                      [self.UUID]["reading"]["read"])
        self.assertEqual("B", config.settings()["active_account"])

    def test_failed_manual_read_change_is_not_stored(self):
        a_state = {self.UUID: {
            "tolino_id": "t1", "calibre_id": 1,
            "reading": self._reading(False, 37)}}
        self._accounts({"A": a_state})
        worker, calls = self._manual(a_state, fail=True)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, worker.outcome["failed"])
        self.assertIs(False, config.account_by_name("A")["state"]
                      [self.UUID]["reading"]["read"])
        self.assertEqual(37, config.account_by_name("A")["state"]
                         [self.UUID]["reading"]["progress"]["percent"])

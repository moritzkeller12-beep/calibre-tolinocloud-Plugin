"""Zustandsjournal: kleine Sofortschreibvorgaenge, ein Vollschreiben je Phase.

Der Schutz gegen Doppel-Uploads haengt daran, dass die Tolino-ID eines
fertigen Uploads sofort auf der Platte steht. Diese Tests pruefen, dass das
Journal genau das leistet -- und dass der volle Konto-Schnappschuss nicht
mehr je Aktion geschrieben wird.
"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from .. import config
from ..run_context import RunContext
from . import worker_base_tests
from ._support import (compare_inventory, start_cdp_stubs, stop_cdp_stubs,
                       ui_module_for)


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


class StateJournalTests(unittest.TestCase):
    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="tolino-journal-")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.addCleanup(setattr, config, "STATE_JOURNAL_DIR",
                        config.STATE_JOURNAL_DIR)
        config.STATE_JOURNAL_DIR = self.directory

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    @staticmethod
    def _settings(state):
        return {"account_name": "A", "partner_id": 4, "hardware_id": "hw",
                "refresh_token": "tok", "state": state}

    @staticmethod
    def _job(ui_module, book_id, uuid, title):
        return ui_module.common.SyncJob(
            book_id, uuid, "EPUB", None, "/library/%s.epub" % uuid, None,
            title)

    def test_two_uploads_share_one_full_save_and_journal_each_id(self):
        ui_module = self._ui()
        state = {}
        journals, saved = [], []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.calls = 0

            def login(self):
                return self.refresh

            def upload(self, path):
                self.calls += 1
                if self.calls == 2:
                    journals.append(config.read_state_journal("A"))
                return "new-%d" % self.calls

        jobs = [self._job(ui_module, 1, "u1", "A"),
                self._job(ui_module, 2, "u2", "B")]
        try:
            with patch("calibre_plugin.token_vault.TolinoClient",
                       lambda *args, **kwargs: FakeClient()), \
                 patch("calibre_plugin.ui_workers.save_account",
                       lambda name, values, active=None: saved.append(
                           (name, values))):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(self._settings(state)), jobs, [],
                    state=state)
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        # Vor dem zweiten Upload stand die Tolino-ID des ersten im Journal.
        self.assertEqual("new-1", journals[0]["u1"]["tolino_id"])
        # Vollschreiben: einmal am Ende der Upload-Phase, nicht je Buch.
        self.assertEqual(1, len(saved))
        self.assertEqual({"u1", "u2"}, set(saved[0][1]["state"]))
        # Nach dem Vollschreiben ist das Journal leer.
        self.assertEqual({}, config.read_state_journal("A"))

    def test_a_leftover_journal_prevents_a_duplicate_upload(self):
        ui_module = self._ui()
        config.journal_state("A", {"u1": {"tolino_id": "t-1",
                                          "calibre_id": 1}})
        metadata = {1: {"uuid": "u1", "title": "A", "authors": "",
                        "formats": ["EPUB"], "last_modified": "1"}}
        try:
            worker = ui_module.workers.SyncWorker(
                RunContext.from_values({"account_name": "A", "state": {}}),
                [], [], state={})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual("t-1", worker.state["u1"]["tolino_id"])
        # Der Vergleich ordnet die Cloud-Kopie zu, statt einen Upload
        # vorzuschlagen -- genau das verhindert das doppelte Hochladen.
        rows = compare_inventory(
            metadata, worker.state,
            [{"deliverableId": "t-1", "title": "A", "authors": ""}],
            ["EPUB"])
        self.assertEqual("t-1", rows[0]["tolino_id"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_a_truncated_journal_line_does_not_discard_the_rest(self):
        path = config.state_journal_path("A")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"key": "u1", "entry": {"tolino_id": "t-1"}}\n')
            handle.write('{"key": "u2", "entry": {"tol')
        self.assertEqual({"u1": {"tolino_id": "t-1"}},
                         config.read_state_journal("A"))

    def test_a_removed_entry_is_replayed_as_a_tombstone(self):
        state = {"u1": {"tolino_id": "t-1"}, "u2": {"tolino_id": "t-2"}}
        config.journal_state("A", {"u1": None})
        self.assertEqual(1, config.merge_state_journal(state, "A"))
        self.assertNotIn("u1", state)
        self.assertEqual("t-2", state["u2"]["tolino_id"])

    def test_the_final_gui_save_clears_the_journal(self):
        ui_module = self._ui()
        config.journal_state("A", {"u1": {"tolino_id": "t-1"}})
        dialog = type("Dialog", (), {})()
        dialog.account_name = "A"
        dialog.worker = type("Worker", (), {"account_name": "A"})()
        saved = []
        try:
            with patch("calibre_plugin.ui_dashboard.save_account",
                       lambda name, values, active=None: saved.append(
                           (name, values))):
                ui_module.dashboard._persist_run(
                    dialog, {"u1": {"tolino_id": "t-1"}})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(saved))
        self.assertEqual({}, config.read_state_journal("A"))

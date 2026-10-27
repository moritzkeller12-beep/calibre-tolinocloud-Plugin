"""Lesestatus-Abgleich zwischen Calibre (#tolino_read) und tolino."""
import importlib
import unittest
from unittest.mock import patch

from . import worker_base_tests
from ._support import compare_inventory, ui_module_for
from .dialogs_tests import _QtDialogTest
from ..comparison import merge_read_status
from ..library import (TOLINO_READ_COLUMN, metadata_read_value,
                       read_column_available, update_read_column)


class MergeReadStatusTests(unittest.TestCase):
    """Drei Stände: Calibre, tolino, letzter Abgleich."""

    def test_equal_sides_need_nothing(self):
        self.assertEqual((True, "", False), merge_read_status(True, True, None))
        self.assertEqual((False, "", False),
                         merge_read_status(False, False, True))

    def test_an_empty_column_takes_the_cloud(self):
        self.assertEqual((True, "tolino", False),
                         merge_read_status(None, True, None))

    def test_only_calibre_changed(self):
        self.assertEqual((True, "calibre", False),
                         merge_read_status(True, False, False))

    def test_only_tolino_changed(self):
        self.assertEqual((True, "tolino", False),
                         merge_read_status(False, True, False))

    def test_differences_without_history_are_a_conflict(self):
        target, _source, conflict = merge_read_status(False, True, None)
        self.assertTrue(conflict)
        self.assertTrue(target)  # Cloud-Stand bleibt bis zur Entscheidung


class ReadColumnLibraryTests(unittest.TestCase):
    @staticmethod
    def _db(fields, book_ids=(1, 2)):
        writes = []

        class Api:
            def has_id(self, book_id):
                return book_id in book_ids

            def set_field(self, field, values):
                writes.append((field, values))

        class Db:
            field_metadata = fields
            new_api = Api()

        return Db(), writes

    def test_only_a_yes_no_column_is_used(self):
        self.assertTrue(read_column_available(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "bool"}})[0]))
        self.assertFalse(read_column_available(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "text"}})[0]))
        self.assertFalse(read_column_available(self._db({})[0]))

    def test_values_are_read_as_true_false_or_empty(self):
        self.assertTrue(metadata_read_value({TOLINO_READ_COLUMN: True}))
        self.assertFalse(metadata_read_value({TOLINO_READ_COLUMN: False}))
        self.assertIsNone(metadata_read_value({TOLINO_READ_COLUMN: None}))
        self.assertIsNone(metadata_read_value({}))

    def test_writes_go_through_set_field_for_known_books(self):
        db, writes = self._db({TOLINO_READ_COLUMN: {"datatype": "bool"}})
        self.assertEqual(1, update_read_column(db, [(1, True), (9, False)]))
        self.assertEqual([(TOLINO_READ_COLUMN, {1: True})], writes)

    def test_no_column_no_write(self):
        db, writes = self._db({})
        self.assertEqual(0, update_read_column(db, [(1, True)]))
        self.assertEqual([], writes)


class ReadColumnComparisonTests(unittest.TestCase):
    INVENTORY = {"PublicationInventory": {"edata": [
        {"deliverableId": "t-1", "title": "Eins", "author": "A"},
        {"deliverableId": "t-2", "title": "Zwei", "author": "B"},
        {"deliverableId": "t-3", "title": "Drei", "author": "C"},
    ]}}

    @staticmethod
    def _metadata(local_reads):
        uuids = ("11111111-1111-1111-1111-111111111111",
                 "22222222-2222-2222-2222-222222222222",
                 "33333333-3333-3333-3333-333333333333")
        titles = ("Eins", "Zwei", "Drei")
        authors = ("A", "B", "C")
        return {index + 1: {"uuid": uuids[index], "title": titles[index],
                            "authors": authors[index], "formats": ["EPUB"],
                            "calibre_read": local_reads[index]}
                for index in range(3)}

    def _rows(self, local_reads, read_ids, synced=None, read_column=True):
        metadata = self._metadata(local_reads)
        state = {}
        for (book_id, item), value in zip(sorted(metadata.items()),
                                          synced or (None,) * 3):
            state[item["uuid"]] = {"tolino_id": "t-%d" % book_id}
            if value is not None:
                state[item["uuid"]]["read_synced"] = value
        rows = compare_inventory(metadata, state, self.INVENTORY, ["EPUB"],
                                 read_ids=read_ids, read_column=read_column)
        return {row["title"]: row for row in rows}

    def test_calibre_change_is_proposed_for_tolino(self):
        rows = self._rows((True, False, None), set(), synced=(False, False, None))
        self.assertTrue(rows["Eins"]["read"])
        self.assertEqual("calibre", rows["Eins"]["read_source"])
        self.assertTrue(rows["Eins"]["read_sync"])
        self.assertEqual("action", rows["Eins"]["group"])
        # Gleich auf beiden Seiten: nichts zu tun, Gruppe bleibt.
        self.assertFalse(rows["Zwei"]["read"])
        self.assertEqual("", rows["Zwei"]["read_source"])

    def test_cloud_read_fills_an_empty_column(self):
        rows = self._rows((None, None, None), {"t-3"})
        self.assertTrue(rows["Drei"]["read"])
        self.assertEqual("tolino", rows["Drei"]["read_source"])

    def test_a_conflict_waits_for_a_decision(self):
        rows = self._rows((False, None, None), {"t-1"})
        self.assertTrue(rows["Eins"]["read_conflict"])
        self.assertFalse(rows["Eins"]["read_sync"])
        self.assertEqual("review", rows["Eins"]["group"])

    def test_without_the_column_nothing_changes(self):
        rows = self._rows((True, None, None), set(), read_column=False)
        self.assertFalse(rows["Eins"]["read"])
        self.assertNotIn("read_column", rows["Eins"])


class ReadSyncPlanTests(unittest.TestCase):
    class Job:
        def __init__(self, book_id, book_uuid):
            self.book_id = book_id
            self.book_uuid = book_uuid

    def _plan(self):
        """dashboard_plan braucht Qt; erst mit den Stubs laden."""
        ui_module_for(self)
        return importlib.import_module("calibre_plugin.dashboard_plan")

    def test_new_uploads_carry_their_calibre_read_state(self):
        plan = self._plan()
        metadata = {1: {"calibre_read": True}, 2: {"calibre_read": None},
                    3: {}}
        items = plan.read_sync_items(
            metadata, [self.Job(1, "u1"), self.Job(2, "u2"), self.Job(3, "u3")])
        self.assertEqual([{"book_id": 1, "book_uuid": "u1", "tolino_id": "",
                           "value": True, "write_calibre": False}], items)
        self.assertEqual([], plan.calibre_read_updates({"read_sync": items}))

    def test_a_selected_row_wins_over_the_upload_default(self):
        plan = self._plan()
        selected = [{"book_id": 1, "book_uuid": "u1", "tolino_id": "t-1",
                     "value": False, "write_calibre": True}]
        items = plan.read_sync_items({1: {"calibre_read": True}},
                                [self.Job(1, "u1")], selected)
        self.assertEqual(selected, items)


class ReadColumnDialogTests(_QtDialogTest):
    @staticmethod
    def _column_rows():
        base = {"status": "identical", "read_column": True,
                "read_known": True}
        return [
            dict(base, title="Aus Calibre", authors="A", book_id=1,
                 uuid="u1", tolino_id="t-1", read_in_cloud=False,
                 read_local=True, read=True, read_sync=True,
                 read_source="calibre"),
            dict(base, title="Aus tolino", authors="B", book_id=2,
                 uuid="u2", tolino_id="t-2", read_in_cloud=True,
                 read_local=None, read=True, read_sync=True,
                 read_source="tolino"),
            dict(base, title="Konflikt", authors="C", book_id=3,
                 uuid="u3", tolino_id="t-3", read_in_cloud=True,
                 read_local=False, read=True, read_sync=False,
                 read_conflict=True, group="review"),
        ]

    @staticmethod
    def _cell(dialog, title):
        cells = {(row, column): widget
                 for row, column, widget in dialog.table.cell_widgets}
        index = next(i for i, row in enumerate(dialog.rows)
                     if row["title"] == title)
        return index, cells[(index, 2)]

    def test_defaults_sync_both_sides_and_leave_conflicts(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._column_rows())
        self.assertEqual(["t-1"], dialog.selected_read_targets())
        self.assertEqual([], dialog.selected_read_unmarks())
        updates = dialog.selected_calibre_read_updates()
        self.assertEqual([(2, True)],
                         [(item["book_id"], item["value"]) for item in updates])
        self.assertNotIn(3, [item["book_id"]
                             for item in dialog.selected_read_sync()])
        dialog._update_summary()
        self.assertIn("1 Lesestatus in Calibre", dialog.summary.text())
        self.assertEqual(2, dialog.selected_work_count())

    def test_a_click_decides_a_conflict(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._column_rows())
        index, cell = self._cell(dialog, "Konflikt")
        cell.setChecked(False)
        dialog._read_clicked(index)
        self.assertIn("t-3", dialog.selected_read_unmarks())
        items = {item["book_id"]: item for item in dialog.selected_read_sync()}
        self.assertFalse(items[3]["value"])
        self.assertFalse(items[3]["write_calibre"])  # Calibre war schon «nein»

    def test_the_row_action_also_decides_a_conflict_to_the_cloud_value(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._column_rows())
        index, _cell = self._cell(dialog, "Konflikt")
        self.assertTrue(dialog._set_row_choice(index, "read", True))
        items = {item["book_id"]: item for item in dialog.selected_read_sync()}
        self.assertTrue(items[3]["value"])
        self.assertTrue(items[3]["write_calibre"])

    def test_reset_changes_nothing_and_recommendation_restores(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._column_rows())
        dialog.clear_all_selection()
        self.assertEqual([], dialog.selected_read_sync())
        self.assertEqual([], dialog.selected_read_targets())
        dialog.apply_recommended()
        self.assertEqual(["t-1"], dialog.selected_read_targets())
        self.assertEqual(2, len(dialog.selected_read_sync()))

    def test_the_hint_names_both_sides_of_a_conflict(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._column_rows())
        note = dialog._read_note(self._column_rows()[2])
        self.assertIn("Calibre: ungelesen", note)
        self.assertIn("tolino: gelesen", note)


class ReadSyncWorkerTests(unittest.TestCase):
    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    @staticmethod
    def _settings(state):
        return {"account_name": "A", "partner_id": 4, "hardware_id": "hw-A",
                "refresh_token": "tok-A", "state": state}

    def _run(self, ui_module, state, client, jobs=(), read_marks=(),
             read_unmarks=(), read_sync=()):
        with patch("calibre_plugin.client.TolinoClient", client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args: None):
            worker = ui_module.workers.SyncWorker(
                self._settings(state), list(jobs), [], [], {},
                list(read_marks), list(read_unmarks), read_sync=read_sync)
            worker.run()
        return worker

    @staticmethod
    def _client(marked, fail=False):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"

            def login(self):
                return self.refresh

            def upload(self, path):
                return "new-1"

            def mark_read_many(self, book_ids, finished=True):
                if fail:
                    raise RuntimeError("sync-data kaputt")
                marked.append((list(book_ids), finished))

        return FakeClient

    def test_success_records_the_state_and_the_calibre_write(self):
        ui_module = self._ui()
        marked = []
        state = {"u1": {"tolino_id": "t-1"}, "u2": {"tolino_id": "t-2"}}
        worker = self._run(ui_module, state, self._client(marked),
                           read_marks=["t-1"], read_sync=[
            {"book_id": 1, "book_uuid": "u1", "tolino_id": "t-1",
             "value": True, "write_calibre": False},
            {"book_id": 2, "book_uuid": "u2", "tolino_id": "t-2",
             "value": False, "write_calibre": True}])
        self.assertEqual([(["t-1"], True)], marked)
        self.assertTrue(worker.state["u1"]["read_synced"])
        self.assertFalse(worker.state["u2"]["read_synced"])
        self.assertEqual([(2, False)], worker.read_updates)
        self.assertEqual(1, worker.outcome["calibre_read"])

    def test_a_failed_cloud_step_keeps_calibre_unchanged(self):
        ui_module = self._ui()
        state = {"u1": {"tolino_id": "t-1"}}
        worker = self._run(ui_module, state, self._client([], fail=True),
                           read_marks=["t-1"], read_sync=[
            {"book_id": 1, "book_uuid": "u1", "tolino_id": "t-1",
             "value": True, "write_calibre": True}])
        self.assertEqual([], worker.read_updates)
        self.assertNotIn("read_synced", worker.state["u1"])

    def test_a_read_book_is_marked_after_its_first_upload(self):
        ui_module = self._ui()
        marked = []
        job = ui_module.common.SyncJob(1, "u1", "EPUB", None,
                                       "/library/u1.epub", None, "Eins")
        worker = self._run(ui_module, {}, self._client(marked), jobs=[job],
                           read_sync=[{"book_id": 1, "book_uuid": "u1",
                                       "tolino_id": "", "value": True,
                                       "write_calibre": False}])
        self.assertEqual([(["new-1"], True)], marked)
        self.assertTrue(worker.state["u1"]["read_synced"])

    def test_a_replaced_copy_moves_the_read_mark_to_the_new_copy(self):
        ui_module = self._ui()
        marked = []
        job = ui_module.common.SyncJob(1, "u1", "EPUB", "t-old",
                                       "/library/u1.epub", None, "Eins")
        client = self._client(marked)
        client.delete = lambda self, deliverable_id: None
        self._run(
            ui_module, {"u1": {"tolino_id": "t-old"}}, client, jobs=[job],
            read_marks=["t-old"],
            read_sync=[{"book_id": 1, "book_uuid": "u1", "tolino_id": "t-old",
                        "value": True, "write_calibre": False}])
        self.assertEqual([(["new-1"], True)], marked)

    def test_an_upload_that_fails_skips_the_planned_mark(self):
        ui_module = self._ui()
        marked = []
        client = self._client(marked)

        def broken_upload(self, path):
            raise RuntimeError("Upload kaputt")

        client.upload = broken_upload
        job = ui_module.common.SyncJob(1, "u1", "EPUB", None,
                                       "/library/u1.epub", None, "Eins")
        worker = self._run(ui_module, {}, client, jobs=[job],
                           read_sync=[{"book_id": 1, "book_uuid": "u1",
                                       "tolino_id": "", "value": True,
                                       "write_calibre": False}])
        self.assertEqual([], marked)
        self.assertEqual(1, worker.outcome["failed"])


class ReadColumnPersistTests(unittest.TestCase):
    def test_the_dashboard_writes_the_worker_result_into_the_column(self):
        ui_module = ui_module_for(self)
        writes = []
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "A"
        dashboard.gui = type("Gui", (), {"current_db": object()})()
        dashboard.sync_column_enabled = False
        dashboard.read_column_enabled = True
        dashboard.worker = type("Worker", (), {
            "read_updates": [(1, True)], "account_name": "A"})()
        with patch("calibre_plugin.ui_dashboard._persist_run",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.ui_dashboard.update_read_column",
                   lambda db, updates: writes.append(list(updates))):
            ui_module.dashboard._persist_sync_result(
                dashboard, {}, "rot", [])
        self.assertEqual([[(1, True)]], writes)
        dashboard.read_column_enabled = False
        with patch("calibre_plugin.ui_dashboard._persist_run",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.ui_dashboard.update_read_column",
                   lambda db, updates: writes.append(list(updates))):
            ui_module.dashboard._persist_sync_result(
                dashboard, {}, "rot", [])
        self.assertEqual(1, len(writes))

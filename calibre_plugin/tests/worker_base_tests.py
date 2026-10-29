"""Gemeinsame Worker-Basis: Signal-Descriptor und SyncWorker."""

from . import ui_base_tests
import unittest
from unittest.mock import patch
from ._support import start_cdp_stubs, stop_cdp_stubs


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class CollectionReconcileWorkerTests(unittest.TestCase):
    """Serien → Sammlungen im SyncWorker.

    Feldbefund des Nutzers: «ich habe das Gefühl, es synchronisiert die
    Ordner automatisch». Genau das passiert hier – aber nur, wenn die
    Sammlungen eingeschaltet sind, und vorhandene Zuordnungen werden
    nicht erneut geschrieben.
    """

    @staticmethod
    def _signal_descriptor():
        class Signal:
            def __init__(self):
                self.slots = []

            def connect(self, slot):
                self.slots.append(slot)

            def emit(self, *args):
                for slot in self.slots:
                    slot(*args)

        class Descriptor:
            def __init__(self, *_types):
                self.name = ""

            def __set_name__(self, owner, name):
                self.name = name

            def __get__(self, obj, objtype=None):
                if obj is None:
                    return self
                return obj.__dict__.setdefault(self.name, Signal())

        return Descriptor, Signal

    def _run_worker(self, series_map, state, known_pairs, read_changes=None):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal": self._signal_descriptor()[0]})
        calls = {"pairs_reads": 0, "added": [], "progress": [],
                 "completed": None, "failed": None, "read_marks": [],
                 "worker": None}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rotated-refresh"

            def login(self):
                return "rotated-refresh"

            def collection_pairs(self):
                calls["pairs_reads"] += 1
                return set(known_pairs)

            def add_to_collection(self, tolino_id, series, revision=None):
                calls["added"].append((tolino_id, series))
                return 1

            def mark_read_many(self, book_ids, finished=True):
                calls["read_marks"].append((list(book_ids), finished))
                return 2

        settings = {"partner_id": 4, "hardware_id": "hw",
                    "refresh_token": "tok",
                    "state": state}
        try:
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient):
                worker = ui_module.workers.SyncWorker(
                    settings, [], [], [], series_map, read_changes)
                calls["worker"] = worker
                worker.progress.connect(
                    lambda *args: calls["progress"].append(args))
                worker.completed.connect(
                    lambda *args: calls.update(completed=args))
                worker.failed.connect(
                    lambda *args: calls.update(failed=args))
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        return calls

    def test_switched_on_creates_only_missing_collections(self):
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": "t2"}}
        calls = self._run_worker(
            {"u1": "Adamsberg", "u2": "Neu"}, state,
            {("t1", "Adamsberg")})
        # t1/Adamsberg ist schon da, nur t2/Neu fehlt.
        self.assertEqual([("t2", "Neu")], calls["added"])
        self.assertEqual(1, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])
        self.assertIsNone(calls["failed"])
        self.assertTrue(any("Serie in der Cloud: Neu" in text
                            for _done, _total, text in calls["progress"]))
        # Beide Zuordnungen sind jetzt gemerkt -- auch die schon vorhandene.
        # Diesen Zustand bekommt das Dashboard und speichert ihn.
        self.assertEqual(["Adamsberg"], state["u1"]["collections"])
        self.assertEqual(["Neu"], state["u2"]["collections"])
        emitted = calls["completed"][0]
        self.assertEqual(["Adamsberg"], emitted["u1"]["collections"])
        self.assertEqual(["Neu"], emitted["u2"]["collections"])
        # Nur die neu geschriebene Sammlung zählt im Abschlussbericht;
        # die bestehende Zuordnung wird als übersprungen ausgewiesen.
        self.assertEqual(1, calls["worker"].outcome["collections"])
        self.assertEqual(1, calls["worker"].outcome["skipped"])

    def test_remembered_collections_skip_the_cloud_entirely(self):
        """Zweiter Sync mit denselben Serien: nichts zu tun, also auch
        keine Cloud-Anfrage und keine Zuordnung mehr."""
        state = {"u1": {"tolino_id": "t1", "collections": ["Adamsberg"]}}
        calls = self._run_worker({"u1": "Adamsberg"}, state, set())
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])

    def test_switched_off_leaves_the_cloud_untouched(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}},
            {("t1", "Adamsberg")})
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])
        self.assertIsNotNone(calls["completed"])

    def test_books_without_cloud_copy_get_no_collection(self):
        calls = self._run_worker(
            {"u2": "Neu"}, {"u1": {"tolino_id": "t1"}}, set())
        self.assertEqual([], calls["added"])
        self.assertEqual(0, calls["pairs_reads"])

    def test_read_marks_are_one_bulk_call(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_changes=[("t1", None, True), ("t2", None, True)])
        self.assertEqual([(["t1", "t2"], True)], calls["read_marks"])
        self.assertIsNotNone(calls["completed"])
        self.assertTrue(any("Als gelesen markieren" in text
                            for _done, _total, text in calls["progress"]))

    def test_read_unmarks_are_one_bulk_call_with_finished_false(self):
        """Häkchen weggeklickt: EIN Aufruf, der die Marke entfernt."""
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_changes=[("t1", None, False), ("t2", None, False)])
        self.assertEqual([(["t1", "t2"], False)], calls["read_marks"])
        self.assertTrue(any("Als ungelesen markieren" in text
                            for _done, _total, text in calls["progress"]))
        self.assertIsNotNone(calls["completed"])
        self.assertIsNone(calls["failed"])

    def test_confirmed_unmark_reaches_the_calibre_column(self):
        """Gelesen -> ungelesen muss die Spalte auf Nein setzen."""
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1", "calibre_id": 7}}, set(),
            read_changes=[("t1", 7, False)])
        self.assertEqual([(7, False)], calls["worker"].read_updates)

    def test_confirmed_mark_reaches_the_calibre_column(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1", "calibre_id": 7}}, set(),
            read_changes=[("t1", 7, True)])
        self.assertEqual([(7, True)], calls["worker"].read_updates)

    def test_marking_and_unmarking_are_two_separate_calls(self):
        calls = self._run_worker(
            {}, {"u1": {"tolino_id": "t1"}}, set(),
            read_changes=[("t1", None, True), ("t2", None, False)])
        self.assertEqual([(["t1"], True), (["t2"], False)], calls["read_marks"])
        # Beide Schritte zählen im Fortschritt mit.
        self.assertEqual(2, max(total for _done, total, _text
                                in calls["progress"]))

    def test_without_read_marks_the_cloud_status_is_untouched(self):
        calls = self._run_worker({}, {"u1": {"tolino_id": "t1"}}, set())
        self.assertEqual([], calls["read_marks"])


class SyncWorkerStateTests(unittest.TestCase):
    """Der Lauf arbeitet auf dem bestätigten Zustand, nicht auf einer Kopie."""

    def test_worker_takes_the_state_of_the_run(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     CollectionReconcileWorkerTests._signal_descriptor()[0]})
        settings = {"partner_id": 4, "state": {"alt": {"tolino_id": "t-alt"}}}
        try:
            worker = ui_module.workers.SyncWorker(
                settings, [], [], state={"neu": {"tolino_id": "t-neu"}})
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual({"neu": {"tolino_id": "t-neu"}}, worker.state)
        self.assertIsNot(worker.state, settings["state"])

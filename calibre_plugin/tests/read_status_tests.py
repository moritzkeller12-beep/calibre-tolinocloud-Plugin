"""Tolino-Lesestatus im Vergleichsdialog; #tolino_read bleibt informativ."""

from ..comparison import (ProgressState, ReadState, comparison_progress_updates,
                          comparison_read_updates)
import unittest
from ._support import _read_ids, compare_inventory
from .dialogs_tests import _QtDialogTest
from ..library import (TOLINO_PROGRESS_COLUMN, TOLINO_READ_COLUMN,
                       bool_column_value, column_state,
                       update_progress_status, update_read_status)
from ..sync_data import reading_progress


class ReadColumnLibraryTests(unittest.TestCase):
    @staticmethod
    def _db(fields):
        class Db:
            field_metadata = fields
        return Db()

    def test_read_column_is_boolean_and_its_values_stay_true_false_or_empty(self):
        self.assertTrue(column_state(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "bool"}}))["read"])
        self.assertFalse(column_state(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "text"}}))["read"])
        self.assertFalse(column_state(self._db({}))["read"])
        for value, expected in ((True, True), (False, False), (None, None)):
            with self.subTest(value=value):
                self.assertIs(expected, bool_column_value(
                    {TOLINO_READ_COLUMN: value}, TOLINO_READ_COLUMN))
        self.assertIsNone(bool_column_value({}, TOLINO_READ_COLUMN))


class ReadColumnWriteBackTests(unittest.TestCase):
    """Nach einem Lauf spiegelt sich der bestätigte Status in #tolino_read."""

    @staticmethod
    def _db(known, fields=None):
        if fields is None:
            fields = {TOLINO_READ_COLUMN: {"datatype": "bool"}}

        class Api:
            def __init__(self):
                self.label = None
                self.values = {}

            @staticmethod
            def has_id(book_id):
                return book_id in known

            def set_field(self, label, mapping):
                self.label = label
                self.values = dict(mapping)

        api = Api()

        class Db:
            field_metadata = fields
            new_api = api

        return Db()

    def test_confirmed_states_are_written_to_the_column(self):
        db = self._db({1: None, 2: None})
        self.assertEqual(2, update_read_status(db, [(1, False), (2, True)]))
        self.assertEqual(TOLINO_READ_COLUMN, db.new_api.label)
        self.assertEqual({1: False, 2: True}, db.new_api.values)

    def test_only_confirmed_known_books_are_written_when_the_column_exists(self):
        db = self._db({1: None})
        self.assertEqual(1, update_read_status(db, [(1, True), (2, False)]))
        self.assertEqual({1: True}, db.new_api.values)
        db = self._db({1: None}, fields={})
        self.assertEqual(0, update_read_status(db, [(1, False)]))
        self.assertEqual({}, db.new_api.values)

    def test_none_clears_the_column_instead_of_becoming_false(self):
        db = self._db({1: None})
        self.assertEqual(1, update_read_status(db, [(1, None)]))
        self.assertEqual({1: None}, db.new_api.values)


class ReadColumnComparisonTests(unittest.TestCase):
    INVENTORY = {"PublicationInventory": {"edata": [
        {"deliverableId": "t-1", "title": "Eins", "author": "A"},
        {"deliverableId": "t-2", "title": "Zwei", "author": "B"},
    ]}}

    @staticmethod
    def _metadata(local_reads):
        uuids = ("11111111-1111-1111-1111-111111111111",
                 "22222222-2222-2222-2222-222222222222")
        return {index + 1: {"uuid": uuids[index], "title": title,
                            "authors": author, "formats": ["EPUB"],
                            "calibre_read": local_reads[index]}
                for index, (title, author) in enumerate(
                    (("Eins", "A"), ("Zwei", "B")))}

    def test_calibre_value_is_display_only_and_absent_without_the_column(self):
        metadata = self._metadata((True, None))
        state = {item["uuid"]: {"tolino_id": "t-%d" % book_id}
                 for book_id, item in metadata.items()}
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids=set(), read_column=True)
        by_title = {row["title"]: row for row in rows}
        self.assertTrue(by_title["Eins"]["read"].local)
        self.assertFalse(by_title["Eins"]["read"])
        self.assertEqual("unchanged", by_title["Eins"]["group"])
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids=set(), read_column=False)
        self.assertFalse(rows[0]["read"])
        self.assertNotIn("read_local", rows[0])

    def test_existing_cloud_read_state_becomes_a_column_update(self):
        metadata = self._metadata((False, None))
        state = {"22222222-2222-2222-2222-222222222222": {
            "tolino_id": "t-2"}}
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids={"t-2"}, read_column=True)
        self.assertEqual([(1, False), (2, True)],
                         comparison_read_updates(rows))

        unknown = compare_inventory(metadata, state, self.INVENTORY,
                                    ["EPUB"], read_ids=None,
                                    read_column=True)
        self.assertEqual([], comparison_read_updates(unknown))

    def test_existing_cloud_progress_becomes_a_column_update(self):
        metadata = self._metadata((False, None))
        state = {"22222222-2222-2222-2222-222222222222": {
            "tolino_id": "t-2"}}
        rows = compare_inventory(
            metadata, state, self.INVENTORY, ["EPUB"], read_ids=set(),
            read_column=True, progress_by_id={
                "t-2": {"percent": 37, "current": "130", "last": "350"}})
        self.assertEqual([(1, None), (2, 37)],
                         comparison_progress_updates(rows))

        unknown = compare_inventory(metadata, state, self.INVENTORY,
                                    ["EPUB"], read_ids=None,
                                    read_column=True, progress_by_id=None)
        self.assertEqual([], comparison_progress_updates(unknown))

    def test_cloud_status_is_still_used_for_dialog_defaults(self):
        metadata = self._metadata((False, None))
        state = {item["uuid"]: {"tolino_id": "t-%d" % book_id}
                 for book_id, item in metadata.items()}
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids={"t-2"}, read_column=True)
        by_title = {row["title"]: row for row in rows}
        self.assertFalse(by_title["Eins"]["read"])
        self.assertTrue(by_title["Zwei"]["read"])
        self.assertIsNone(by_title["Zwei"]["read"].local)



class ProgressReducerTests(unittest.TestCase):
    """sync-data-Bookmarks: Position und Prozent, ohne Schreibweg."""

    @staticmethod
    def _patch(op, value, book="b1", tag="12"):
        return {"op": op,
                "path": "/publications/%s/bookmark/%s" % (book, tag),
                "value": value}

    def test_bookmark_fields_and_position_fallback_are_read(self):
        result = reading_progress([self._patch("replace", {
            "currentPosition": "192", "lastPosition": "590",
            "progress": 0.32881355,
            "position": "Ops/017.html#point(/1/4/74:0)",
            "modified": 100,
        })])
        self.assertEqual(33, result["b1"]["percent"])
        self.assertEqual("192", result["b1"]["current"])
        self.assertEqual("590", result["b1"]["last"])
        self.assertIn("Ops/017.html", result["b1"]["position"])
        fallback = reading_progress([self._patch("replace", {
            "currentPosition": 1, "lastPosition": 4
        })])
        self.assertEqual(25, fallback["b1"]["percent"])

    def test_patch_order_and_remove_control_the_result(self):
        patches = [
            self._patch("replace", {"progress": 0.2, "modified": 200}),
            self._patch("replace", {"progress": 0.8, "modified": 100}),
        ]
        self.assertEqual(20, reading_progress(patches)["b1"]["percent"])
        patches = [self._patch("add", {"progress": 0.4}),
                   self._patch("remove", {"progress": 0.4})]
        self.assertEqual({}, reading_progress(patches))

    def test_removing_one_bookmark_keeps_the_other_progress(self):
        patches = [
            self._patch("replace", {"progress": 0.25, "modified": 100},
                        tag="1"),
            self._patch("replace", {"progress": 0.75, "modified": 200},
                        tag="2"),
        ]
        self.assertEqual(75, reading_progress(patches)["b1"]["percent"])
        self.assertEqual(25, reading_progress(
            patches + [self._patch("remove", {}, tag="2")])["b1"]["percent"])
        self.assertEqual({}, reading_progress(
            patches + [self._patch("remove", {}, tag="2"),
                       self._patch("remove", {}, tag="1")]))

    def test_foreign_and_malformed_paths_are_ignored(self):
        patches = [
            {"op": "replace", "path": "/publications/b1/tags/1",
             "value": {"progress": 0.5}},
            {"op": "replace", "path": "/publications//bookmark/1",
             "value": {"progress": 0.5}},
            "kein dict",
        ]
        self.assertEqual({}, reading_progress(patches))


class ProgressColumnTests(unittest.TestCase):
    """#tolino_progress ist eine informative Zahlenspalte (0-100)."""

    @staticmethod
    def _db(known, fields=None):
        if fields is None:
            fields = {TOLINO_PROGRESS_COLUMN: {"datatype": "int"}}

        class Api:
            def __init__(self):
                self.label = None
                self.values = {}

            @staticmethod
            def has_id(book_id):
                return book_id in known

            def set_field(self, label, mapping):
                self.label = label
                self.values = dict(mapping)

        api = Api()

        class Db:
            field_metadata = fields
            new_api = api

        return Db()

    def test_progress_is_written_as_rounded_percent_and_can_be_cleared(self):
        db = self._db({1, 2, 3})
        self.assertEqual(3, update_progress_status(
            db, [(1, 33.4), (2, 85), (3, None)]))
        self.assertEqual(TOLINO_PROGRESS_COLUMN, db.new_api.label)
        self.assertEqual({1: 33, 2: 85, 3: None}, db.new_api.values)

    def test_unknown_books_and_invalid_or_non_finite_values_are_skipped(self):
        db = self._db({1})
        self.assertEqual(1, update_progress_status(
            db, [(1, 42), (2, 90), (1, "keine Zahl"), (1, float("inf"))]))
        self.assertEqual({1: 42}, db.new_api.values)
        self.assertIsNone(ProgressState(percent=float("inf")).percent)

    def test_only_an_existing_numeric_column_is_written(self):
        db = self._db({1}, fields={
            TOLINO_PROGRESS_COLUMN: {"datatype": "bool"}})
        self.assertFalse(column_state(db)["progress"])
        self.assertEqual(0, update_progress_status(db, [(1, 42)]))
        db = self._db({1}, fields={})
        self.assertEqual(0, update_progress_status(db, [(1, 42)]))
        self.assertEqual({}, db.new_api.values)

    def test_unchanged_progress_is_not_written_again(self):
        db = self._db({1})
        db.new_api.field_for = lambda _column, _book_id: 33
        self.assertEqual(0, update_progress_status(db, [(1, 33)]))
        self.assertEqual({}, db.new_api.values)


class ProgressComparisonTests(unittest.TestCase):
    """Fortschritt kommt an die Zeile, bleibt aber reine Anzeige."""

    INVENTORY = {"PublicationInventory": {"edata": [
        {"deliverableId": "t-1", "title": "Eins", "author": "A"},
    ]}}

    def _rows(self, progress_by_id):
        metadata = {1: {"uuid": "11111111-1111-4111-8111-111111111111", "title": "Eins", "authors": "A",
                        "formats": ["EPUB"]}}
        state = {"11111111-1111-4111-8111-111111111111": {"tolino_id": "t-1"}}
        return compare_inventory(
            metadata, state, self.INVENTORY, ["EPUB"], read_ids=set(),
            progress_by_id=progress_by_id)

    def test_progress_text_distinguishes_known_unknown_and_empty(self):
        row = self._rows({"t-1": {
            "percent": 33, "current": "192", "last": "590",
            "position": "Ops/017.html#point(/1/4/74:0)"}})[0]
        self.assertTrue(row["progress"].known)
        self.assertEqual(33, row["progress"].percent)
        self.assertEqual("33 % · 192/590", row["progress"].text)
        row = self._rows(None)[0]
        self.assertFalse(row["progress"].known)
        self.assertEqual("unbekannt", row["progress"].text)
        row = self._rows({})[0]
        self.assertTrue(row["progress"].known)
        self.assertIsNone(row["progress"].percent)
        self.assertEqual("", row["progress"].text)


class ReadColumnDialogTests(_QtDialogTest):
    @staticmethod
    def _rows():
        base = {"status": "identical", "read_column": True}
        return [
            dict(base, title="Calibre only", authors="A", book_id=1,
                 uuid="11111111-1111-4111-8111-111111111111", tolino_id="t-1",
                 read=ReadState(cloud=False, local=True)),
            dict(base, title="Cloud read", authors="B", book_id=2,
                 uuid="22222222-2222-4222-8222-222222222222", tolino_id="t-2",
                 read=ReadState(cloud=True, local=None)),
        ]

    @staticmethod
    def _cell(dialog, title):
        index = next(i for i, row in enumerate(dialog.rows)
                     if row["title"] == title)
        return index, dialog.table.item(index, 2)

    def test_progress_is_visible_but_not_an_action(self):
        ui_module = self._ui()
        self._messages(ui_module)
        rows = self._rows()
        rows[0] = dict(rows[0], progress=ProgressState(
            percent=33, current="192", last="590"))
        rows[1] = dict(rows[1], progress=ProgressState(known=False))
        dialog = ui_module.dialogs.InventoryDialog(rows)
        index = next(i for i, row in enumerate(dialog.rows)
                     if row["title"] == "Calibre only")
        other = next(i for i, row in enumerate(dialog.rows)
                     if row["title"] == "Cloud read")
        self.assertEqual("33 % · 192/590",
                         dialog.table.items[(index, 8)])
        self.assertEqual("unbekannt", dialog.table.items[(other, 8)])
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))

    def test_clicking_read_creates_only_the_explicit_cloud_selection(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._rows())
        index, cell = self._cell(dialog, "Calibre only")
        cell.setChecked(True)
        self.assertEqual(["t-1"], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        self.assertEqual(1, dialog._selection_counts()["work"])

    def test_recommendations_do_not_change_read_selection_from_calibre(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._rows())
        dialog.apply_recommended()
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))

    def test_unknown_cloud_status_cannot_be_changed(self):
        ui_module = self._ui()
        self._messages(ui_module)
        row = dict(self._rows()[0],
                   read=ReadState(known=False, cloud=True, local=True))
        dialog = ui_module.dialogs.InventoryDialog([row])
        self.assertFalse(dialog.table.item(0, 2).flags() &
                         ui_module.dialogs.Qt.ItemFlag.ItemIsEnabled)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))



if __name__ == "__main__":
    unittest.main()

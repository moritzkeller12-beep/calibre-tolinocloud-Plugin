"""Tolino-Lesestatus im Vergleichsdialog; #tolino_read bleibt informativ."""
import unittest
from ._support import compare_inventory
from .dialogs_tests import _QtDialogTest
from ..library import (TOLINO_READ_COLUMN, bool_column_value,
                       read_column_available, update_read_status)


def _read_ids(dialog, finished):
    """tolino-IDs der gewählten Lesestatus-Änderungen (True = gelesen)."""
    return [tolino_id for tolino_id, _book, state
            in dialog.selected_read_changes() if state is finished]


class ReadColumnLibraryTests(unittest.TestCase):
    @staticmethod
    def _db(fields):
        class Db:
            field_metadata = fields
        return Db()

    def test_only_a_yes_no_column_is_recognized(self):
        self.assertTrue(read_column_available(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "bool"}})))
        self.assertFalse(read_column_available(self._db(
            {TOLINO_READ_COLUMN: {"datatype": "text"}})))
        self.assertFalse(read_column_available(self._db({})))

    def test_values_are_read_only_as_true_false_or_empty(self):
        self.assertTrue(bool_column_value({TOLINO_READ_COLUMN: True},
                          TOLINO_READ_COLUMN))
        self.assertFalse(bool_column_value({TOLINO_READ_COLUMN: False},
                          TOLINO_READ_COLUMN))
        self.assertIsNone(bool_column_value({TOLINO_READ_COLUMN: None},
                          TOLINO_READ_COLUMN))
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

    def test_unknown_books_are_skipped(self):
        db = self._db({1: None})
        self.assertEqual(1, update_read_status(db, [(1, True), (2, False)]))
        self.assertEqual({1: True}, db.new_api.values)

    def test_without_the_column_nothing_is_written(self):
        db = self._db({1: None}, fields={})
        self.assertEqual(0, update_read_status(db, [(1, False)]))
        self.assertEqual({}, db.new_api.values)


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

    def test_calibre_value_does_not_propose_a_cloud_change(self):
        metadata = self._metadata((True, False))
        state = {item["uuid"]: {"tolino_id": "t-%d" % book_id}
                 for book_id, item in metadata.items()}
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids=set(), read_column=True)
        by_title = {row["title"]: row for row in rows}
        self.assertTrue(by_title["Eins"]["read"].local)
        self.assertFalse(by_title["Eins"]["read"])
        self.assertEqual("unchanged", by_title["Eins"]["group"])
        self.assertFalse(by_title["Zwei"]["read"])

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

    def test_without_the_column_no_calibre_display_value_is_added(self):
        metadata = self._metadata((True, None))
        state = {item["uuid"]: {"tolino_id": "t-%d" % book_id}
                 for book_id, item in metadata.items()}
        rows = compare_inventory(metadata, state, self.INVENTORY,
                                 ["EPUB"], read_ids=set(), read_column=False)
        by_title = {row["title"]: row for row in rows}
        self.assertFalse(by_title["Eins"]["read"])
        self.assertNotIn("read_local", by_title["Eins"])


class ReadColumnDialogTests(_QtDialogTest):
    @staticmethod
    def _rows():
        base = {"status": "identical", "read_column": True,
                "read_known": True}
        return [
            dict(base, title="Calibre only", authors="A", book_id=1,
                 uuid="u1", tolino_id="t-1", read_in_cloud=False,
                 read_local=True, read=False),
            dict(base, title="Cloud read", authors="B", book_id=2,
                 uuid="u2", tolino_id="t-2", read_in_cloud=True,
                 read_local=None, read=True),
        ]

    @staticmethod
    def _cell(dialog, title):
        index = next(i for i, row in enumerate(dialog.rows)
                     if row["title"] == title)
        return index, dialog.read_checks[index]

    def test_calibre_value_is_a_note_not_a_default_or_action(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._rows())
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        note = dialog._read_note(self._rows()[0])
        self.assertEqual("Calibre-Lesestatus: gelesen.", note)
        self.assertEqual(0, dialog.selected_work_count())

    def test_clicking_read_creates_only_the_explicit_cloud_selection(self):
        ui_module = self._ui()
        self._messages(ui_module)
        dialog = ui_module.dialogs.InventoryDialog(self._rows())
        index, cell = self._cell(dialog, "Calibre only")
        cell.setChecked(True)
        self.assertEqual(["t-1"], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))
        self.assertEqual(1, dialog.selected_work_count())

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
        row = dict(self._rows()[0], read_known=False, read_local=True,
                   read=True, read_in_cloud=True)
        dialog = ui_module.dialogs.InventoryDialog([row])
        self.assertFalse(dialog.read_checks[0].enabled)
        self.assertEqual([], _read_ids(dialog, True))
        self.assertEqual([], _read_ids(dialog, False))



if __name__ == "__main__":
    unittest.main()

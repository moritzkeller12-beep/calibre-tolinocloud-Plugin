"""Calibre-Spalten, ihre Stellvertreter und die Bibliothekszuordnung.

Geprueft wird das Verhalten, das der Nutzer sieht: welche Spalten angelegt
werden, dass ein zweiter Klick nichts doppelt anlegt, dass eine neue Spalte
erst nach dem Neustart beschrieben wird und dass Lesestatus, Fortschritt und
tolino-ID nur in vorhandene Spalten geschrieben werden.
"""
import json
import unittest

from ..library import (CUSTOM_COLUMNS, DEFAULT_LIBRARY_SCOPE, TOLINO_COLUMN,
                       TOLINO_IGNORE_COLUMN, TOLINO_PROGRESS_COLUMN,
                       TOLINO_READ_COLUMN, column_state, create_custom_columns,
                       library_scope, metadata_ignored, update_progress_status,
                       update_read_status, update_tolino_ids)
from ._support import CalibreDatabase

ALL_LABELS = [spec[0] for spec in CUSTOM_COLUMNS]


class _RecordingDatabase(CalibreDatabase):
    """Merkt sich Label und Calibre-Typ jedes Anlege-Aufrufs."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.specs = []

    def create_custom_column(self, label, name, datatype, is_multiple,
                             editable=True, display=None):
        self.specs.append((label, datatype))
        return super().create_custom_column(label, name, datatype,
                                            is_multiple, editable, display)


class ColumnCreationTests(unittest.TestCase):
    """Spalten entstehen ueber Calibres oeffentliche API -- einmal je Bibliothek."""

    def test_missing_columns_are_created_with_their_types(self):
        database = _RecordingDatabase(dbpath=self.id())
        created = create_custom_columns(database)
        self.assertEqual(ALL_LABELS, created)
        self.assertEqual([("tolino_id", "text"), ("tolino_read", "bool"),
                          ("tolino_progress", "int"), ("tolino_ignore", "bool")],
                         database.specs)

    def test_a_second_click_creates_no_duplicates(self):
        database = _RecordingDatabase(dbpath=self.id())
        create_custom_columns(database)
        self.assertEqual([], create_custom_columns(database))
        self.assertEqual(ALL_LABELS, database.created)

    def test_another_library_gets_its_own_columns(self):
        first = CalibreDatabase(dbpath=self.id() + "-a")
        second = CalibreDatabase(dbpath=self.id() + "-b")
        create_custom_columns(first)
        self.assertEqual(ALL_LABELS, create_custom_columns(second))

    def test_existing_columns_are_not_touched(self):
        database = _RecordingDatabase(
            {TOLINO_COLUMN: {"datatype": "text"}}, dbpath=self.id())
        self.assertEqual(["tolino_read", "tolino_progress", "tolino_ignore"],
                         create_custom_columns(database))
        complete = _RecordingDatabase(
            {column: {"datatype": "text"} for column in
             (TOLINO_COLUMN, TOLINO_READ_COLUMN, TOLINO_PROGRESS_COLUMN,
              TOLINO_IGNORE_COLUMN)}, dbpath=self.id() + "-b")
        self.assertEqual([], create_custom_columns(complete))
        self.assertEqual([], complete.created)

    def test_a_single_label_creates_only_that_column(self):
        database = _RecordingDatabase(dbpath=self.id())
        self.assertEqual(["tolino_progress"],
                         create_custom_columns(database, ["tolino_progress"]))
        self.assertEqual([("tolino_progress", "int")], database.specs)


class ColumnStateTests(unittest.TestCase):
    """Was Calibre kennt, entscheidet ueber Schreiben -- nicht der Wunsch."""

    def test_state_reports_only_columns_calibre_knows(self):
        database = CalibreDatabase(
            {TOLINO_COLUMN: {"datatype": "text"},
             TOLINO_READ_COLUMN: {"datatype": "bool"}}, dbpath=self.id())
        self.assertEqual({"id": True, "read": True, "progress": False},
                         column_state(database))

    def test_a_fresh_column_is_not_used_before_the_restart(self):
        database = CalibreDatabase(dbpath=self.id())
        create_custom_columns(database)
        self.assertEqual({"id": False, "read": False, "progress": False},
                         column_state(database))
        self.assertEqual(0, update_tolino_ids(database, [(1, "t1")], "A"))
        self.assertEqual(0, update_read_status(database, [(1, True)]))
        self.assertEqual(0, update_progress_status(database, [(1, 42)]))
        self.assertEqual([], database.api.writes)

    def test_a_text_column_is_not_a_read_column(self):
        database = CalibreDatabase(
            {TOLINO_READ_COLUMN: {"datatype": "text"}}, dbpath=self.id())
        self.assertFalse(column_state(database)["read"])
        self.assertEqual(0, update_read_status(database, [(1, True)]))


class ColumnWriteTests(unittest.TestCase):
    """Informative Spalten: nur bestaetigte Werte, nie doppelt."""

    def test_read_and_progress_are_written_independently(self):
        database = CalibreDatabase(
            {TOLINO_READ_COLUMN: {"datatype": "bool"},
             TOLINO_PROGRESS_COLUMN: {"datatype": "int"}},
            fail={TOLINO_READ_COLUMN}, dbpath=self.id())
        with self.assertRaises(OSError):
            update_read_status(database, [(1, True)])
        self.assertEqual(1, update_progress_status(database, [(2, 42)]))
        self.assertEqual([(TOLINO_PROGRESS_COLUMN, {2: 42})],
                         database.api.writes)

    def test_unchanged_values_and_unknown_progress_are_not_written(self):
        database = CalibreDatabase(
            {TOLINO_READ_COLUMN: {"datatype": "bool"},
             TOLINO_PROGRESS_COLUMN: {"datatype": "int"}}, dbpath=self.id())
        self.assertEqual(1, update_read_status(database, [(1, True)]))
        self.assertEqual(0, update_read_status(database, [(1, True)]))
        self.assertEqual(0, update_progress_status(
            database, [(1, 150), (2, "keine Zahl")]))
        self.assertEqual(1, update_progress_status(database, [(3, 42.6)]))
        self.assertEqual([(TOLINO_READ_COLUMN, {1: True}),
                          (TOLINO_PROGRESS_COLUMN, {3: 43})],
                         database.api.writes)

    def test_tolino_ids_are_kept_per_account(self):
        database = CalibreDatabase(
            {TOLINO_COLUMN: {"datatype": "text"}}, dbpath=self.id())
        update_tolino_ids(database, [(1, "t1")], "A")
        update_tolino_ids(database, [(1, "t2")], "B")
        self.assertEqual({"A": "t1", "B": "t2"},
                         json.loads(database.api.values[(TOLINO_COLUMN, 1)]))
        update_tolino_ids(database, [(1, "")], "A")
        self.assertEqual({"B": "t2"},
                         json.loads(database.api.values[(TOLINO_COLUMN, 1)]))

    def test_tolino_ids_need_a_column_and_an_account(self):
        database = CalibreDatabase(dbpath=self.id())
        self.assertEqual(0, update_tolino_ids(database, [(1, "t1")], "A"))
        database = CalibreDatabase(
            {TOLINO_COLUMN: {"datatype": "text"}}, dbpath=self.id())
        self.assertEqual(0, update_tolino_ids(database, [(1, "t1")], ""))
        self.assertEqual([], database.api.writes)


class IgnoreAndScopeTests(unittest.TestCase):
    """Ausschluss nur bei klarem Ja; Bibliotheken bleiben unterscheidbar."""

    def test_only_a_clear_yes_ignores_a_book(self):
        for value, expected in ((True, True), ("ja", True), ("1", True),
                                (False, False), ("nein", False), ("", False),
                                (None, False), ("vielleicht", False)):
            with self.subTest(value=value):
                self.assertIs(expected,
                              metadata_ignored({TOLINO_IGNORE_COLUMN: value}))

    def test_library_scope_prefers_id_then_dbpath(self):
        named = type("Db", (), {"library_id": "kennung", "dbpath": "/a"})()
        self.assertEqual(library_scope(named), library_scope(
            type("Db", (), {"library_id": "kennung", "dbpath": "/b"})()))
        without = type("Db", (), {"dbpath": "/a"})()
        self.assertNotEqual(library_scope(without), library_scope(
            type("Db", (), {"dbpath": "/b"})()))
        self.assertEqual(DEFAULT_LIBRARY_SCOPE,
                         library_scope(type("Db", (), {})()))


if __name__ == "__main__":
    unittest.main()

"""Echte Calibre-Laeufe: Spalten in einer bestehenden Bibliothek.

Diese Tests bauen keine Attrappe nach: sie starten das echte Calibre aus dem
Plugin-Quellbaum (``calibre_plugin/``) auf einer echten Bibliothek und lesen
die Ergebnisse direkt aus ``metadata.db``. Ohne Calibre werden sie
uebersprungen -- ein Ueberspringen ist ehrlicher als ein Nachbau.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest


def _calibre_tools():
    """Pfad zu ``calibre-debug`` und ``calibredb`` oder ``(None, None)``."""
    candidates = [os.environ.get("CALIBRE_DEBUG"),
                  shutil.which("calibre-debug")]
    for candidate in candidates:
        if candidate and os.access(candidate, os.X_OK):
            calibredb = os.path.join(os.path.dirname(candidate), "calibredb")
            if os.access(calibredb, os.X_OK):
                return candidate, calibredb
    return None, None


CALIBRE_DEBUG, CALIBREDB = _calibre_tools()
if not CALIBRE_DEBUG:
    raise unittest.SkipTest("calibre-debug fehlt -- echte Laeufe brauchen Calibre")

_BASE = '''
import importlib.util
import json
import sys

LIB, SRC = sys.argv[1], sys.argv[2]

# Das Modul direkt aus dem Quellbaum laden: calibre-debug wuerde sonst das
# installierte Plugin aus dem Calibre-Konfigurationsordner importieren.
spec = importlib.util.spec_from_file_location(
    "tolino_library_under_test", SRC + "/library.py")
library = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = library
spec.loader.exec_module(library)

from calibre.db.legacy import LibraryDatabase

database = LibraryDatabase(LIB)
api = database.new_api


def facts(payload):
    payload["module"] = library.__file__
    print("FACTS" + json.dumps(payload, default=str, ensure_ascii=False))


'''

_PREPARE_COLUMNS = _BASE + '''
created = []
for label, name, datatype in (("meintext", "Mein Text", "text"),
                              ("meinebewertung", "Meine Bewertung", "rating"),
                              ("meinezahl", "Meine Zahl", "int")):
    if "#" + label in database.field_metadata:
        continue
    api.create_custom_column(label, name, datatype, False,
                             editable=True, display={})
    created.append(label)
facts({"created": created} if created else {"already": True})
'''

_FILL_COLUMNS = _BASE + '''
api.set_field("#meintext", {1: "Erster Text", 2: "Zweiter Text"})
api.set_field("#meinebewertung", {1: 4, 2: 2})
api.set_field("#meinezahl", {1: 7, 2: 42})
facts({key: [api.field_for(key, book) for book in (1, 2)]
       for key in ("#meintext", "#meinebewertung", "#meinezahl")})
'''

_CREATE_TOLINO = _BASE + '''
facts({"created": library.create_custom_columns(database),
       "columns": library.column_state(database)})
'''

_AUDIT = _BASE + '''
import sqlite3

CUSTOM = ("#meintext", "#meinebewertung", "#meinezahl")


def snapshot():
    out = {}
    for key in database.field_metadata:
        if str(key).startswith("#"):
            entry = database.field_metadata[key]
            out[key] = {"colnum": entry.get("colnum"),
                        "rec_index": entry.get("rec_index"),
                        "datatype": entry.get("datatype")}
    return out


result = {"before": snapshot(),
          "existing_before": {key: [api.field_for(key, book) for book in (1, 2)]
                              for key in CUSTOM if key in database.field_metadata},
          "tolino_before": library.column_state(database)}
result["created"] = library.create_custom_columns(database)
result["columns"] = library.column_state(database)
result["writes"] = {
    "id": library.update_tolino_ids(database, [(1, "TL-1")], "konto"),
    "read": library.update_read_status(database, [(1, True)]),
    "progress": library.update_progress_status(database, [(1, 42)]),
}
result["progress_out_of_range"] = library.update_progress_status(
    database, [(2, 150)])
result["id_without_account"] = library.update_tolino_ids(
    database, [(2, "TL-2")], "")
result["existing_after"] = {key: [api.field_for(key, book) for book in (1, 2)]
                            for key in result["existing_before"]}
api.set_field("#meintext", {2: "Neu"})
result["existing_write"] = api.field_for("#meintext", 2)

con = sqlite3.connect(LIB + "/metadata.db")
numbers = {label: num for num, label in con.execute(
    "SELECT id, label FROM custom_columns")}


def one(sql):
    row = con.execute(sql).fetchone()
    return row[0] if row else None


result["db"] = {
    "id": one("SELECT v.value FROM books_custom_column_%d_link l "
              "JOIN custom_column_%d v ON v.id = l.value WHERE l.book = 1"
              % (numbers["tolino_id"], numbers["tolino_id"])),
    "read": one("SELECT value FROM custom_column_%d WHERE book = 1"
                % numbers["tolino_read"]),
    "progress": one("SELECT value FROM custom_column_%d WHERE book = 1"
                    % numbers["tolino_progress"]),
    "progress_book2": one("SELECT value FROM custom_column_%d WHERE book = 2"
                          % numbers["tolino_progress"]),
    "text_book2": one("SELECT v.value FROM books_custom_column_%d_link l "
                      "JOIN custom_column_%d v ON v.id = l.value "
                      "WHERE l.book = 2" % (numbers["meintext"],
                                            numbers["meintext"])),
}
con.close()

tables = sorted(api.backend.tables)
fields = sorted(api.fields)
result["second_call"] = library.create_custom_columns(database)
result["same_tables"] = sorted(api.backend.tables) == tables
result["same_fields"] = sorted(api.fields) == fields
result["columns_after_second"] = library.column_state(database)
result["write_after_second"] = library.update_read_status(
    database, [(2, True)])
result["after"] = snapshot()
facts(result)
'''

_TOLINO_LABELS = ["tolino_id", "tolino_read", "tolino_progress", "tolino_ignore"]


class RealCalibreColumnTests(unittest.TestCase):
    """Kein Nachbau: echte Bibliothek, echter Calibre-Prozess, echte DB."""

    def setUp(self):
        workdir = tempfile.mkdtemp(prefix="tolino-real-")
        self.addCleanup(shutil.rmtree, workdir, True)
        self.workdir = workdir
        self.library = os.path.join(workdir, "lib")
        os.makedirs(self.library)
        self.source = self._source()
        for _count in range(2):
            self._calibredb("add", "--empty")

    def _source(self):
        """Verzeichnis des Plugin-Quellbaums, gegen den getestet wird."""
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _calibredb(self, *args):
        proc = subprocess.run([CALIBREDB, "--with-library", self.library,
                               *args], capture_output=True, text=True,
                              timeout=180)
        self.assertEqual(proc.returncode, 0,
                         "calibredb %s: %s" % (args, proc.stderr[-400:]))
        return proc.stdout

    def _calibre(self, source):
        script = os.path.join(self.workdir, "step.py")
        with open(script, "w") as handle:
            handle.write(source)
        env = dict(os.environ)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")
        proc = subprocess.run(
            [CALIBRE_DEBUG, "-e", script, "--", self.library, self.source],
            capture_output=True, text=True, env=env, timeout=300)
        line = next((text for text in proc.stdout.splitlines()
                     if text.startswith("FACTS")), None)
        self.assertIsNotNone(
            line, "kein Ergebnis (rc=%s): %s%s" % (proc.returncode,
                                                   proc.stdout[-800:],
                                                   proc.stderr[-800:]))
        result = json.loads(line[len("FACTS"):])
        self.assertEqual(os.path.join(self.source, "library.py"),
                         result.get("module"),
                         "Lauf ging nicht gegen den Quellbaum")
        return result

    def _prepare_populated_columns(self):
        self._calibre(_PREPARE_COLUMNS)
        filled = self._calibre(_FILL_COLUMNS)
        self.assertEqual(
            {"#meintext": ["Erster Text", "Zweiter Text"],
             "#meinebewertung": [4, 2], "#meinezahl": [7, 42]},
            {key: filled[key] for key in ("#meintext", "#meinebewertung",
                                          "#meinezahl")})

    def _assert_indices_match(self, snapshot):
        rows = dict((label, num) for num, label
                    in self._db_columns())
        for key, entry in snapshot.items():
            self.assertEqual(rows[key[1:]], entry["colnum"],
                             "Spaltennummer passt nicht fuer %s" % key)
            self.assertIsNotNone(entry["rec_index"],
                                 "Feldindex fehlt fuer %s" % key)
        indices = [entry["rec_index"] for entry in snapshot.values()]
        self.assertEqual(len(indices), len(set(indices)),
                         "Feldindizes sind nicht eindeutig")

    def _db_columns(self):
        import sqlite3
        con = sqlite3.connect(os.path.join(self.library, "metadata.db"))
        try:
            return list(con.execute("SELECT id, label FROM custom_columns"))
        finally:
            con.close()

    def test_columns_work_in_one_session_with_populated_library(self):
        """Anlegen, Schreiben, DB-Ruecklesung -- ohne Neustart, ohne Attrappe."""
        self._prepare_populated_columns()
        result = self._calibre(_AUDIT)

        self.assertEqual(_TOLINO_LABELS, result["created"])
        self.assertEqual({"id": True, "read": True, "progress": True},
                         result["columns"])
        self.assertEqual({"id": 1, "read": 1, "progress": 1},
                         result["writes"])
        self.assertEqual({"id": '{"konto":"TL-1"}', "read": 1, "progress": 42},
                         {key: result["db"][key]
                          for key in ("id", "read", "progress")})

        # Grenzen: 150 % wird nicht geschrieben, ohne Kontonamen keine ID.
        self.assertEqual(0, result["progress_out_of_range"])
        self.assertIsNone(result["db"]["progress_book2"])
        self.assertEqual(0, result["id_without_account"])

        # Vorhandene Spalten bleiben unveraendert und schreibbar.
        self.assertEqual({"#meintext": ["Erster Text", "Zweiter Text"],
                          "#meinebewertung": [4, 2], "#meinezahl": [7, 42]},
                         result["existing_before"])
        self.assertEqual(result["existing_before"], result["existing_after"])
        self.assertEqual("Neu", result["existing_write"])
        self.assertEqual("Neu", result["db"]["text_book2"])

        # Indizes: Spaltennummer == custom_columns.id, Feldindex eindeutig.
        self._assert_indices_match(result["after"])

        # Zweiter Aufruf bleibt idempotent.
        self.assertEqual([], result["second_call"])
        self.assertTrue(result["same_tables"])
        self.assertTrue(result["same_fields"])
        self.assertEqual({"id": True, "read": True, "progress": True},
                         result["columns_after_second"])
        self.assertEqual(1, result["write_after_second"])

    def test_existing_tolino_columns_are_a_no_op(self):
        """Bestehende Installation: Spalten sind schon da, nichts wird doppelt."""
        self._prepare_populated_columns()
        self.assertEqual(_TOLINO_LABELS,
                         self._calibre(_CREATE_TOLINO)["created"])
        result = self._calibre(_AUDIT)

        self.assertEqual([], result["created"])
        self.assertEqual({"id": True, "read": True, "progress": True},
                         result["columns"])
        self.assertEqual({"id": 1, "read": 1, "progress": 1},
                         result["writes"])
        self.assertEqual({"id": '{"konto":"TL-1"}', "read": 1, "progress": 42},
                         {key: result["db"][key]
                          for key in ("id", "read", "progress")})
        self.assertEqual([], result["second_call"])
        self.assertTrue(result["same_tables"])
        self.assertTrue(result["same_fields"])
        self._assert_indices_match(result["after"])

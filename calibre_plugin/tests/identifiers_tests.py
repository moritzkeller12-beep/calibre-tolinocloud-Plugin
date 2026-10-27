"""Buchkennungen und duenne Metadaten-Fixtures."""

import json
import unittest
from unittest.mock import patch
from . import orchestration_tests
from ..plan import plan_metadata_updates, plan_sync
from ._support import (compare_inventory, start_cdp_stubs, stop_cdp_stubs)


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class CalibreLikeMetadata:
    """Minimales Calibre-`Metadata`: fehlende Felder existieren nicht.

    Calibre legt die Felder in einem internen Dict ab; `getattr()` auf ein
    nicht gesetztes Feld wirft `AttributeError`, und `get()` liefert dann
    den Standardwert. Genau daran scheiterte das Einlesen der Kennung.
    """

    def __init__(self, **fields):
        self.__dict__.update(fields)

    def __getattr__(self, name):
        raise AttributeError(name)

    def get(self, name, default=None):
        return self.__dict__.get(name, default)

class BookIdentifierSourceTests(unittest.TestCase):
    """Die Kennung kommt aus mehreren Quellen, nie stillschweigend leer.

    Feldbefund: «5 Buchdateien und 496 Metadatensätze: ohne gültige
    Calibre-Kennung». `Metadata.uuid` war dabei leer – weder `uuid.UUID`
    noch Text. `plan_sync` und `compare_inventory` verwerfen ein Buch ohne
    Kennung kommentarlos, deshalb endete der Lauf ohne eine einzige Aktion.
    """

    UUID = "8f3c5f0e-1a2b-4c3d-9e8f-001122334455"

    def test_normalize_reads_every_representation(self):
        from ..identifiers import normalize_uuid_text
        import uuid
        self.assertEqual(self.UUID, normalize_uuid_text(uuid.UUID(self.UUID.upper())))
        self.assertEqual(self.UUID, normalize_uuid_text(self.UUID.upper()))
        self.assertEqual(self.UUID, normalize_uuid_text("UUID('%s')" % self.UUID))
        self.assertEqual(self.UUID, normalize_uuid_text("b'%s'" % self.UUID))
        self.assertEqual(self.UUID, normalize_uuid_text(
            self.UUID.encode("utf-8")))
        self.assertEqual("", normalize_uuid_text(None))

    def test_the_database_placeholder_is_not_a_key(self):
        from ..identifiers import valid_book_uuid
        self.assertEqual("", valid_book_uuid("dummy"))

    def test_metadata_uuid_is_the_supported_identifier_source(self):
        from ..identifiers import book_uuid
        self.assertEqual(self.UUID, book_uuid(
            CalibreLikeMetadata(uuid=self.UUID)))
        self.assertEqual("", book_uuid(CalibreLikeMetadata(title="Der Schwarm")))

    def test_is_real_uuid_accepts_every_real_representation(self):
        from ..identifiers import is_real_uuid
        import uuid
        for value in (self.UUID, self.UUID.upper(), uuid.UUID(self.UUID),
                      "UUID('%s')" % self.UUID):
            with self.subTest(value=value):
                self.assertTrue(is_real_uuid(value))
        for value in ("Dune", "Der Schwarm", "calibre-7", "dummy", "", None):
            with self.subTest(value=value):
                self.assertFalse(is_real_uuid(value))

    def test_a_title_is_never_accepted_as_an_identifier(self):
        """Feldbefund: In Moritz' Bibliothek steht im `uuid`-Feld
        der Titel, teils mit, teils ohne Leerzeichen. Ein Titel ist keine
        Kennung -- er ist nicht eindeutig und aendert sich mit dem Titel.
        `valid_book_uuid` bleibt tolerant, die QUELLEN aber nicht.
        """
        from ..identifiers import book_uuid
        for title in ("Der Schwarm", "Dune",
                      "[Rivers of London 04] • Broken Homes"):
            with self.subTest(title=title):
                self.assertEqual("", book_uuid(CalibreLikeMetadata(uuid=title)))

    def test_a_tolerant_state_key_stays_valid(self):
        """`calibre-7` und alte Schluessel muessen weiter gelten; nur die
        Quellen sind streng."""
        from ..identifiers import valid_book_uuid
        self.assertEqual("calibre-7", valid_book_uuid("calibre-7"))
        self.assertEqual("alter-schluessel", valid_book_uuid("alter-schluessel"))

    def test_the_comparison_remembers_the_calibre_id(self):
        from ..identifiers import state_key_for
        metadata = {7: {"uuid": self.UUID, "title": "A", "authors": "B",
                        "formats": ["EPUB"], "last_modified": "1",
                        "format_signatures": {}}}
        state = {}
        compare_inventory(metadata, state, [{
            "deliverableId": "t1", "title": "A", "authors": "B"}],
            ["EPUB"], ("authors", "title"), use_metadata_ids=False)
        self.assertEqual(7, state[self.UUID]["calibre_id"])
        self.assertEqual(self.UUID, state_key_for(state, 7))

    def test_a_known_state_entry_is_found_by_calibre_id(self):
        from ..identifiers import state_key_for
        state = {"alter-schluessel": {"calibre_id": 7, "tolino_id": "t1"},
                 "anderer": {"calibre_id": "8", "tolino_id": "t2"}}
        self.assertEqual("alter-schluessel", state_key_for(state, 7))
        self.assertEqual("anderer", state_key_for(state, "8"))
        self.assertEqual("", state_key_for(state, 9))
        self.assertEqual("", state_key_for(None, 7))

class SilentIdentifierDeadEndTests(unittest.TestCase):
    """Ohne lesbare Kennung muss der Lauf trotzdem etwas tun."""

    def setUp(self):
        self.ui_module = orchestration_tests.StartSyncOrchestrationTests._ui_module()
        self.addCleanup(self.ui_module._restore_stubs_for_tests)
        self.dashboard_module = self.ui_module.dashboard
        self.dashboard_plan = self.dashboard_module._dashboard_plan
        self.original = (self.dashboard_plan.iter_book_ids,
                         self.dashboard_plan.metadata_by_id)

    def tearDown(self):
        (self.dashboard_plan.iter_book_ids,
         self.dashboard_plan.metadata_by_id) = self.original

    def _dashboard(self, books, settings=None):
        settings = settings or {"state": "", "preferred_formats": ["EPUB"]}
        dashboard = self.dashboard_module.SyncDashboard.__new__(
            self.dashboard_module.SyncDashboard)
        dashboard.gui = type("Gui", (), {"current_db": books["library"]})()
        dashboard.values = lambda: settings
        dashboard.sync_column_enabled = False
        self.dashboard_plan.iter_book_ids = lambda _db: list(books["metadata"])
        self.dashboard_plan.metadata_by_id = (
            lambda _db, book_id: books["metadata"][book_id])
        return dashboard

    def _books(self, library=None):
        if library is None:
            library = type("Library", (), {"new_api": type("Api", (), {
                "has_id": staticmethod(lambda _book_id: True),
                "format_abspath": staticmethod(lambda _book_id, _fmt: __file__),
            })()})()
        return {
            "library": library,
            "metadata": {7: CalibreLikeMetadata(
                title="Der Schwarm", authors=["Frank Schatzing"],
                formats=["EPUB"], last_modified="2026-01-01")},
        }

    def test_the_uuid_comes_from_the_database_instead_of_being_dropped(self):
        books = self._books()
        books["metadata"][7] = CalibreLikeMetadata(
            uuid=BookIdentifierSourceTests.UUID, title="Der Schwarm",
            authors=["Frank Schatzing"], formats=["EPUB"],
            last_modified="2026-01-01")
        dashboard = self._dashboard(books)
        metadata = self.dashboard_plan.local_metadata(dashboard)
        self.assertEqual(BookIdentifierSourceTests.UUID, metadata[7]["uuid"])
        uploads = plan_sync(metadata, {}, ["EPUB"],
                            upload_book_ids={7})
        self.assertEqual(1, len(uploads))

    def test_signatures_are_only_read_for_preferred_formats(self):
        class Database:
            def __init__(self):
                self.stat_formats = []
                self.new_api = self

            def all_book_ids(self):
                return (7,)

            def get_metadata(self, _book_id):
                return metadata_row

            def has_id(self, _book_id):
                return True

            def format_abspath(self, book_id, fmt):
                self.stat_formats.append(fmt)
                return "/tmp/%s.%s" % (book_id, fmt.lower())

        database = Database()
        metadata_row = CalibreLikeMetadata(
            title="Der Schwarm", authors=["Frank Schatzing"],
            formats=["EPUB", "PDF"], last_modified="2026-01-01")
        books = {"library": database, "metadata": {7: metadata_row}}
        dashboard = self._dashboard(
            books, settings={"state": "", "preferred_formats": ["EPUB"]})
        with patch("calibre_plugin.ui_common.os.stat",
                   return_value=type("Stat", (), {
                       "st_size": 1, "st_mtime": 1.0})()):
            metadata = self.dashboard_plan.local_metadata(dashboard)
        self.assertEqual(["EPUB"], database.stat_formats)
        self.assertEqual({"EPUB": "1:1000"}, metadata[7]["format_signatures"])

    def test_a_blank_uuid_still_gets_a_stable_key_and_the_upload_runs(self):
        dashboard = self._dashboard(self._books())
        metadata = self.dashboard_plan.local_metadata(dashboard)
        self.assertEqual("calibre-7", metadata[7]["uuid"])
        # Der Vergleich merkt sich die Cloud-Kopie unter der Ersatzkennung.
        state = {}
        compare_inventory(metadata, state, [{
            "deliverableId": "t1", "title": "Der Schwarm",
            "authors": "Frank Schatzing"}], ["EPUB"],
            ("authors", "title"), use_metadata_ids=False)
        self.assertEqual("t1", state.get("calibre-7", {}).get("tolino_id"))
        uploads = plan_sync(metadata, state, ["EPUB"],
                            upload_book_ids={7})
        self.assertEqual(1, len(uploads))
        self.assertEqual("calibre-7", uploads[0][1])
        meta_updates = plan_metadata_updates(
            metadata, state, [], only_book_ids={7})
        self.assertEqual(1, len(meta_updates))
        self.assertEqual("t1", meta_updates[0][2])

    def test_a_stored_entry_keeps_its_old_key(self):
        state = {"frueher": {"calibre_id": 7, "tolino_id": "t1",
                             "fingerprint": "1:1"}}
        dashboard = self._dashboard(self._books(), {
            "state": json.dumps(state), "preferred_formats": ["EPUB"]})
        metadata = self.dashboard_plan.local_metadata(dashboard)
        self.assertEqual("frueher", metadata[7]["uuid"])

    def test_a_title_as_uuid_keeps_the_stored_state_key(self):
        """Ein EINWORT-Titel rutschte bisher als Kennung durch (Titel mit
        Leerzeichen lehnt `valid_book_uuid` schon ab). Der vorhandene
        Zustandsschluessel bleibt: kein neuer Eintrag, keine Migration,
        kein Doppel-Upload."""
        title = "Dune"
        state = {title: {"calibre_id": 7, "tolino_id": "t1"}}
        books = self._books()
        books["metadata"][7] = CalibreLikeMetadata(
            uuid=title, title=title, authors=["Frank Herbert"],
            formats=["EPUB"], last_modified="2026-01-01")
        dashboard = self._dashboard(books, {
            "state": json.dumps(state), "preferred_formats": ["EPUB"]})
        metadata = self.dashboard_plan.local_metadata(dashboard)
        self.assertEqual(title, metadata[7]["uuid"])
        # A legacy fingerprint isn't part of the authoritative state anymore;
        # without file and metadata signatures a sync must be offered.
        uploads = plan_sync(metadata, state, ["EPUB"], upload_book_ids={7})
        self.assertEqual(1, len(uploads))
        self.assertEqual("t1", state[title]["tolino_id"])

class UuidValueTests(unittest.TestCase):
    """Calibres Buchkennung ist nicht immer ein `str`.

    ``_local_metadata`` liest sie über `getattr(mi, "uuid")`; Calibre liefert
    dort je nach Version und Datenbankweg ein `uuid.UUID`. `valid_book_uuid()`
 verlangte dagegen ausdrücklich einen `str` und gab für alles
    andere "" zurück – damit übersprang `plan_sync` **jedes** Buch, der
    Zustand merkte keine Tolino-ID, und alle ausgewählten Uploads und
    Metadatensätze fielen aus dem Plan. 0.15 hatte den Wert immer mit `str()`
    gelesen und war davon nicht betroffen.
    """

    UUID = "11111111-2222-3333-4444-555555555555"

    def test_a_uuid_object_is_a_valid_book_uuid(self):
        from ..identifiers import valid_book_uuid
        import uuid
        self.assertEqual(self.UUID, valid_book_uuid(uuid.UUID(self.UUID)))

    def test_junk_is_still_rejected(self):
        from ..identifiers import valid_book_uuid
        self.assertEqual("", valid_book_uuid(None))
        self.assertEqual("", valid_book_uuid(""))
        self.assertEqual("", valid_book_uuid("   "))
        self.assertEqual("", valid_book_uuid("none"))
        self.assertEqual("", valid_book_uuid("x" * 200))
        self.assertEqual("", valid_book_uuid("mit leerzeichen"))

    @staticmethod
    def _metadata(kind):
        import uuid
        value = uuid.UUID(UuidValueTests.UUID) if kind == "obj" else UuidValueTests.UUID
        return {1: {"uuid": value, "title": "Der Schwarm",
                    "authors": "Frank Schatzing", "isbn": "9783161484100",
                    "formats": ["EPUB"], "last_modified": "1",
                    "format_signatures": {}}}

    @staticmethod
    def _inventory():
        return [{"deliverableId": "t1", "title": "Der Schwarm",
                 "authors": "Frank Schatzing", "isbn": "9783161484100"}]

    def test_the_upload_is_planned_for_a_uuid_object(self):
        uploads = plan_sync(self._metadata("obj"), {}, ["EPUB"],
                            upload_book_ids={1})
        self.assertEqual(1, len(uploads))

    def test_metadata_update_is_planned_for_a_uuid_object(self):
        state = {}
        compare_inventory(self._metadata("obj"), state, self._inventory(),
                          ["EPUB"], ("authors", "title", "isbn"),
                          use_metadata_ids=False)
        # Der Vergleich merkt sich die gefundene Tolino-ID.
        self.assertEqual("t1", state.get(self.UUID, {}).get("tolino_id"))
        uploads = []
        meta_updates = plan_metadata_updates(
            self._metadata("obj"), state, uploads, only_book_ids={1})
        self.assertEqual(1, len(meta_updates))
        self.assertEqual("t1", meta_updates[0][2])

    def test_a_string_uuid_behaves_the_same(self):
        string_state = {}
        compare_inventory(self._metadata("str"), string_state,
                          self._inventory(), ["EPUB"],
                          ("authors", "title", "isbn"), use_metadata_ids=False)
        object_state = {}
        compare_inventory(self._metadata("obj"), object_state,
                          self._inventory(), ["EPUB"],
                          ("authors", "title", "isbn"), use_metadata_ids=False)
        self.assertEqual(string_state, object_state)

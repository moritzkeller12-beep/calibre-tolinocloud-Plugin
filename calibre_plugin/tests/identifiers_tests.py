"""Buchkennungen und duenne Metadaten-Fixtures."""

import unittest
from unittest.mock import patch
from . import orchestration_tests
from ..plan import plan_metadata_updates, plan_sync
from ..identifiers import calibre_key
from ..library import DEFAULT_LIBRARY_SCOPE, library_scope
from ..run_context import RunContext
from ._support import (compare_inventory, dashboard_stub, start_cdp_stubs,
                       stop_cdp_stubs)


# Der Einzellauf richtet die CDP-Stubs selbst ein.
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

    def test_normalize_reads_text_and_uuid_objects_only(self):
        from ..identifiers import normalize_uuid_text
        import uuid
        self.assertEqual(self.UUID, normalize_uuid_text(uuid.UUID(self.UUID.upper())))
        self.assertEqual(self.UUID, normalize_uuid_text(self.UUID.upper()))
        self.assertEqual("uuid('%s')" % self.UUID,
                         normalize_uuid_text("UUID('%s')" % self.UUID))
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
        for value in (self.UUID, self.UUID.upper(), uuid.UUID(self.UUID)):
            with self.subTest(value=value):
                self.assertTrue(is_real_uuid(value))
        for value in ("Dune", "Der Schwarm", "calibre-7", "dummy", "", None,
                      "UUID('%s')" % self.UUID, self.UUID.encode("utf-8")):
            with self.subTest(value=value):
                self.assertFalse(is_real_uuid(value))

    def test_a_title_is_never_accepted_as_an_identifier(self):
        """Feldbefund: In einer Bibliothek steht im `uuid`-Feld
        der Titel, teils mit, teils ohne Leerzeichen. Ein Titel ist keine
        Kennung -- er ist nicht eindeutig und aendert sich mit dem Titel.
        `valid_book_uuid` bleibt tolerant, die QUELLEN aber nicht.
        """
        from ..identifiers import book_uuid
        for title in ("Der Schwarm", "Dune",
                      "[Rivers of London 04] • Broken Homes"):
            with self.subTest(title=title):
                self.assertEqual("", book_uuid(CalibreLikeMetadata(uuid=title)))

    def test_only_library_scoped_calibre_keys_are_valid(self):
        """The unscoped fallback belongs to migration, not to new state."""
        from ..identifiers import valid_book_uuid
        self.assertEqual("", valid_book_uuid("calibre-7"))
        self.assertEqual("calibre-abcdef123456-7",
                         valid_book_uuid("calibre-abcdef123456-7"))

    def test_the_comparison_remembers_the_calibre_id(self):
        metadata = {7: {"uuid": self.UUID, "title": "A", "authors": "B",
                        "formats": ["EPUB"], "last_modified": "1",
                        "format_signatures": {}}}
        state = {}
        compare_inventory(metadata, state, [{
            "deliverableId": "t1", "title": "A", "authors": "B"}],
            ["EPUB"])
        self.assertEqual(7, state[self.UUID]["calibre_id"])

class SilentIdentifierDeadEndTests(unittest.TestCase):
    """Ohne lesbare Kennung muss der Lauf trotzdem etwas tun."""

    def setUp(self):
        self.ui_module = orchestration_tests.StartSyncOrchestrationTests._ui_module()
        self.addCleanup(self.ui_module._restore_stubs_for_tests)
        self.dashboard_module = self.ui_module.dashboard
        self.dashboard_plan = self.dashboard_module._dashboard_plan

    def _dashboard(self, books, settings=None):
        settings = settings or {"state": "", "preferred_formats": ["EPUB"]}
        dashboard = dashboard_stub()
        api = books["library"].new_api
        metadata = books["metadata"]
        attrs = {name: staticmethod(getattr(api, name)) for name in
                 ("has_id", "format_abspath", "cover") if hasattr(api, name)}
        attrs["all_book_ids"] = staticmethod(lambda: list(metadata))
        attrs["get_metadata"] = staticmethod(lambda book_id: metadata[book_id])
        library = type("Library", (), {"new_api": type("Api", (), attrs)()})()
        dashboard.gui = type("Gui", (), {"current_db": library})()
        dashboard.values = lambda: settings
        dashboard.columns = {"id": False}
        return dashboard

    @staticmethod
    def _context(dashboard):
        return RunContext.from_values(
            dashboard.values(), dashboard.columns,
            library_scope=library_scope(dashboard.gui.current_db))

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
        metadata = self.dashboard_plan.local_metadata(
            dashboard.gui.current_db, self._context(dashboard))
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
        with patch("calibre_plugin.planning_values.os.stat",
                   return_value=type("Stat", (), {
                       "st_size": 1, "st_mtime": 1.0})()):
            metadata = self.dashboard_plan.local_metadata(
                dashboard.gui.current_db, self._context(dashboard))
        self.assertEqual(["EPUB"], database.stat_formats)
        self.assertEqual({"EPUB": "1:1000"}, metadata[7]["format_signatures"])

    def test_a_blank_uuid_still_gets_a_stable_key_and_the_upload_runs(self):
        dashboard = self._dashboard(self._books())
        metadata = self.dashboard_plan.local_metadata(
            dashboard.gui.current_db, self._context(dashboard))
        key = calibre_key(7, library_scope(dashboard.gui.current_db))
        self.assertEqual(key, metadata[7]["uuid"])
        # Der Vergleich merkt sich die Cloud-Kopie unter der Ersatzkennung.
        state = {}
        compare_inventory(metadata, state, [{
            "deliverableId": "t1", "title": "Der Schwarm",
            "authors": "Frank Schatzing"}], ["EPUB"])
        self.assertEqual("t1", state.get(key, {}).get("tolino_id"))
        uploads = plan_sync(metadata, state, ["EPUB"],
                            upload_book_ids={7})
        self.assertEqual(1, len(uploads))
        self.assertEqual(key, uploads[0][1])
        meta_updates = plan_metadata_updates(
            metadata, state, [], {7})
        self.assertEqual(1, len(meta_updates))
        self.assertEqual("t1", meta_updates[0][2])

    def test_a_stored_entry_under_an_old_key_does_not_override_the_calibre_key(self):
        state = {"frueher": {"calibre_id": 7, "tolino_id": "t1"}}
        dashboard = self._dashboard(self._books(), {
            "state": state, "preferred_formats": ["EPUB"]})
        metadata = self.dashboard_plan.local_metadata(
            dashboard.gui.current_db, self._context(dashboard))
        self.assertEqual(
            calibre_key(7, library_scope(dashboard.gui.current_db)),
            metadata[7]["uuid"])
        self.assertIn("frueher", state)

    def test_a_title_as_uuid_uses_the_stable_calibre_key(self):
        """A one-word title is not a source UUID; `calibre-<id>` wins."""
        title = "Dune"
        state = {title: {"calibre_id": 7, "tolino_id": "t1"}}
        books = self._books()
        books["metadata"][7] = CalibreLikeMetadata(
            uuid=title, title=title, authors=["Frank Herbert"],
            formats=["EPUB"], last_modified="2026-01-01")
        dashboard = self._dashboard(books, {
            "state": state, "preferred_formats": ["EPUB"]})
        metadata = self.dashboard_plan.local_metadata(
            dashboard.gui.current_db, self._context(dashboard))
        self.assertEqual(
            calibre_key(7, library_scope(dashboard.gui.current_db)),
            metadata[7]["uuid"])
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
    Metadatensätze fielen aus dem Plan. Eine frühere Version hatte den Wert
    immer mit `str()`
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
        self.assertEqual("", valid_book_uuid("u1"))
        self.assertEqual("", valid_book_uuid("book"))
        self.assertEqual("", valid_book_uuid("calibre-7"))
        self.assertEqual("calibre-abcdef123456-7",
                         valid_book_uuid("calibre-abcdef123456-7"))


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
                          ["EPUB"])
        # Der Vergleich merkt sich die gefundene Tolino-ID.
        self.assertEqual("t1", state.get(self.UUID, {}).get("tolino_id"))
        uploads = []
        meta_updates = plan_metadata_updates(
            self._metadata("obj"), state, uploads, {1})
        self.assertEqual(1, len(meta_updates))
        self.assertEqual("t1", meta_updates[0][2])

    def test_a_string_uuid_behaves_the_same(self):
        string_state = {}
        compare_inventory(self._metadata("str"), string_state,
                          self._inventory(), ["EPUB"])
        object_state = {}
        compare_inventory(self._metadata("obj"), object_state,
                          self._inventory(), ["EPUB"])
        self.assertEqual(string_state, object_state)


class LibraryScopedKeyTests(unittest.TestCase):
    """Fallback keys name their library; old keys are adopted only once."""

    class Api:
        def all_book_ids(self):
            return (7,)

        def get_metadata(self, _book_id):
            return CalibreLikeMetadata(title="Der Schwarm",
                                       authors=["Frank Schatzing"],
                                       formats=[])

    class Database:
        def __init__(self, library_id):
            self.library_id = library_id
            self.new_api = LibraryScopedKeyTests.Api()

    def test_scope_is_stable_and_distinct_between_libraries(self):
        first = library_scope(type("Db", (), {"library_id": "a"})())
        second = library_scope(type("Db", (), {"library_id": "b"})())
        self.assertEqual(first, library_scope(
            type("Db", (), {"library_id": "a"})()))
        self.assertNotEqual(first, second)
        self.assertEqual(first, library_scope(
            type("Db", (), {"dbpath": "a"})()))
        self.assertEqual(DEFAULT_LIBRARY_SCOPE, library_scope(object()))

    def test_a_key_requires_a_scope(self):
        self.assertEqual("", calibre_key(7))
        self.assertEqual("calibre-abcdef123456-7", calibre_key(7, "ABCDEF123456"))

    def test_fallback_key_uses_the_library_scope(self):
        from ..dashboard_plan import local_metadata
        from ..identifiers import calibre_key
        first = self.Database("library-a")
        second = self.Database("library-b")
        first_metadata = local_metadata(
            first, RunContext(preferred_formats=("EPUB",),
                              library_scope=library_scope(first)))
        second_metadata = local_metadata(
            second, RunContext(preferred_formats=("EPUB",),
                               library_scope=library_scope(second)))
        self.assertEqual(calibre_key(7, library_scope(first)),
                         first_metadata[7]["uuid"])
        self.assertNotEqual(first_metadata[7]["uuid"],
                            second_metadata[7]["uuid"])

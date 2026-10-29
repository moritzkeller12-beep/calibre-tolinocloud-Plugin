"""Planung, Fingerabdruecke und Zustandsverhalten."""

from . import ui_base_tests
from . import worker_base_tests
import ast
import json
import re
import runpy
import time
import calibre_plugin.token_vault as vault_module
import unittest
import uuid
from .. import config
from ..library import (TOLINO_COLUMN, TOLINO_IGNORE_COLUMN, CUSTOM_COLUMNS,
                       cover_bytes, create_custom_columns,
                       custom_column_available, ignore_column_available,
                       metadata_ignored, metadata_tolino_id,
                       normalize_formats, safe_format_path,
                       selected_table_rows, update_tolino_ids)
from ..comparison import (content_fingerprint, metadata_fingerprint,
                          normalize_title)
from ..plan import (metadata_update_fields, plan_collection_updates,
                    plan_metadata_updates, plan_sync, recorded_collections,
                    remember_collection, series_map_from_metadata)
from ..plan import UploadRecord
from ..inventory import normalize_inventory_record
from ..comparison import compare_inventory as comparison_compare_inventory
from ..plan import plan_sync as plan_module_plan_sync
from ..plan import plan_collection_updates as plan_module_collection_plan
from ..plan import plan_metadata_updates as plan_module_metadata_plan
from ..library import (cover_bytes as library_cover_bytes,
                       normalize_formats as library_normalize_formats,
                       safe_format_path as library_safe_format_path,
                       update_tolino_ids as library_update_tolino_ids)
from ..matching import (author_tokens, authors_match, normalize_isbn,
                        normalize_match_text,
                        normalized_authors)
from ..config import load_state
from ..partners import (PARTNERS, WEBREADER_CLIENT_TYPE,
                        WEBREADER_CLIENT_VERSION, WEBREADER_TOKEN_HEADERS,
                        WEBREADER_URL)
from ..tolino import (
    TolinoAuthError, TolinoClient, hardware_id, normalize_hardware_id,
    normalize_refresh_token, sanitize_error)
from pathlib import Path
from unittest.mock import patch
from ._support import (compare_inventory, parsed_inventory, start_cdp_stubs,
                       stop_cdp_stubs, ui_module_for)


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class SyncPlanTests(unittest.TestCase):
    def setUp(self):
        self.book = {"uuid": "u1", "title": "A", "formats": ["EPUB"], "last_modified": "1"}

    def test_new_book_is_uploaded(self):
        uploads = plan_sync({1: self.book}, {}, ["EPUB"], upload_book_ids={1})
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)

    def test_database_reads_use_only_the_supported_new_api(self):
        class DatabaseAPI:
            def __init__(self):
                self.calls = []

            def all_book_ids(self):
                self.calls.append(("ids",))
                return (41, 99)

            def get_metadata(self, book_id):
                self.calls.append(("metadata", book_id))
                return {"uuid": "u%d" % book_id}

            def has_id(self, book_id):
                return book_id in {41, 99}

            def format_abspath(self, book_id, fmt):
                self.calls.append(("format", book_id, fmt))
                return "/library/%d.%s" % (book_id, fmt.lower())

            def cover(self, book_id, as_file=False):
                self.calls.append(("cover", book_id, as_file))
                return b"cover-data"

        class Database:
            new_api = DatabaseAPI()

        database = Database()
        self.assertEqual([41, 99], list(database.new_api.all_book_ids()))
        self.assertEqual({"uuid": "u41"},
                         database.new_api.get_metadata(41))
        self.assertEqual("/library/41.epub", safe_format_path(database, 41, "EPUB"))
        self.assertEqual(b"cover-data", cover_bytes(database, 41))
        self.assertEqual([
            ("ids",), ("metadata", 41), ("format", 41, "EPUB"),
            ("cover", 41, False)], database.new_api.calls)

    def test_new_api_rejects_stale_ids_for_file_and_cover_access(self):
        class DatabaseAPI:
            def has_id(self, _book_id):
                return False

        class Database:
            new_api = DatabaseAPI()

        for resolve in (lambda: safe_format_path(Database(), 99, "EPUB"),
                        lambda: cover_bytes(Database(), 99)):
            with self.assertRaisesRegex(ValueError, "no book with id 99"):
                resolve()

    def test_normalize_formats_handles_all_calibre_shapes(self):
        self.assertEqual(["EPUB", "PDF"], normalize_formats(("EPUB", "PDF")))
        self.assertEqual(["EPUB", "PDF"],
                         library_normalize_formats(("EPUB", "PDF")))
        self.assertEqual(["EPUB", "PDF"], normalize_formats("EPUB, PDF"))
        class FormatsList:
            def __iter__(self):
                return iter((".epub", "PDF", "epub"))

        self.assertEqual(["EPUB", "PDF"], normalize_formats(FormatsList()))
        self.assertEqual([], normalize_formats(None))
        self.assertEqual([], normalize_formats(("", None)))

    def test_plan_sync_accepts_calibre_formats_list_and_normalizes_case(self):
        class FormatsList:
            def __iter__(self):
                return iter((".epub",))

        metadata = {"uuid": "u1", "title": "A", "formats": FormatsList()}
        self.assertEqual(
            [(1, "u1", "EPUB", None)],
            plan_sync({1: metadata}, {}, (".EPUB",), upload_book_ids={1}),
        )

    def test_selected_book_without_preferred_format_is_skipped(self):
        uploads = plan_sync(
            {1: {"uuid": "u1", "title": "A", "formats": ("MOBI",)}},
            {}, ("EPUB",), upload_book_ids={1},
        )
        self.assertEqual([], uploads)

    def test_upload_planner_filters_formats_without_rechecking_them(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            plan_module = ui_module.dashboard._dashboard_plan
            metadata = {
                1: {"uuid": "u1", "title": "EPUB verfügbar", "formats": ["EPUB"]},
                2: {"uuid": "u2", "title": "Nur PDF", "formats": ["PDF"]},
            }
            dialog = type("Dialog", (), {})()
            with patch.object(plan_module, "normalize_formats",
                              wraps=normalize_formats) as normalize:
                with patch.object(plan_module, "upload_jobs",
                                  return_value=([], [])):
                    preparation = plan_module.plan_actions(
                        dialog, {"preferred_formats": ["EPUB"]}, metadata, {},
                        {"uploads": {1, 2}, "metadata": set(), "read": [],
                         "unread": [], "cloud_deletions": []})
            self.assertEqual(1, normalize.call_count)
            self.assertEqual(1, preparation["selected_without_action"]["uploads"])
            self.assertEqual(1, len(preparation["skipped_uploads"]))
            self.assertIn("Nur PDF", preparation["skipped_uploads"][0])
            self.assertIn("EPUB", preparation["skipped_uploads"][0])
        finally:
            ui_module._restore_stubs_for_tests()

    def test_selected_rows_handles_qmodelindex_and_short_values(self):
        class Index:
            def __init__(self, value):
                self.value = value

            def row(self):
                return self.value

        class Selection:
            def selectedRows(self):
                return (Index(2), Index(-1), object(), Index(0))

        class Table:
            def selectionModel(self):
                return Selection()

        self.assertEqual([2, 0], selected_table_rows(Table()))

    def test_complete_preparation_path_handles_empty_single_and_short_data(self):
        cases = (
            ({}, [], []),
            ({1: dict(self.book)}, [], [(1, "u1", "EPUB", None)]),
            ({2: {"uuid": "u2", "formats": ("EPUB",)}}, [], [(2, "u2", "EPUB", None)]),
        )
        for metadata, inventory, expected_uploads in cases:
            compare_inventory(metadata, {}, inventory, ("EPUB",))
            uploads = plan_sync(
                metadata, {}, ("EPUB",), upload_book_ids=set(metadata))
            self.assertEqual(expected_uploads, uploads)
            for record in uploads:
                self.assertEqual(expected_uploads[0][0] if expected_uploads else None,
                                 record.book_id)

    def test_safe_format_path_accepts_calibre_path_or_missing_format(self):
        class DatabaseAPI:
            def __init__(self, path):
                self.path = path
                self.args = None

            def has_id(self, book_id):
                return book_id == 1

            def format_abspath(self, book_id, format_name):
                self.args = (book_id, format_name)
                return self.path

        class Database:
            def __init__(self, path):
                self.new_api = DatabaseAPI(path)

        database = Database("/library/book.epub")
        self.assertEqual("/library/book.epub", safe_format_path(database, 1, "EPUB"))
        self.assertEqual("/library/book.epub",
                         library_safe_format_path(database, 1, "EPUB"))
        self.assertEqual((1, "EPUB"), database.new_api.args)
        with self.assertRaises(ValueError):
            safe_format_path(Database(None), 1, "EPUB")

    def test_safe_format_path_rejects_invalid_ids_and_formats(self):
        class DatabaseAPI:
            def has_id(self, _book_id):
                return True

            def format_abspath(self, _book_id, _format_name):
                return "/library/book.epub"

        class Database:
            new_api = DatabaseAPI()

        for invalid_id in (None, 0, -1, True, "42"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                safe_format_path(Database(), invalid_id, "EPUB")
        with self.assertRaises(ValueError):
            safe_format_path(Database(), 2, "../EPUB")

    def test_local_metadata_reads_isbn_from_calibre_identifiers(self):
        from ._support import load_ui_module
        ui_module = load_ui_module(lambda *args, **kwargs: None)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        local_metadata = ui_module.dashboard._dashboard_plan.local_metadata

        class Db:
            field_metadata = {}

        class Dialog:
            gui = type("Gui", (), {"current_db": Db()})()
            sync_column_enabled = False

            @staticmethod
            def values():
                return {"state": {}, "preferred_formats": []}

        metadata = type("Metadata", (), {
            "uuid": "00000000-0000-4000-8000-000000000001",
            "title": "Buch", "authors": ["Autor"],
            "identifiers": {"isbn": "9783161484100", "amazon": "B00X123"},
            "formats": [], "last_modified": "now", "series": "",
        })()
        class Api:
            def all_book_ids(self):
                return [1]

            def get_metadata(self, _book_id):
                return metadata

        Dialog.gui.current_db.new_api = Api()
        result = local_metadata(Dialog())
        self.assertEqual("9783161484100", result[1]["isbn"])

    def test_upload_job_skips_cover_when_cover_read_raises_os_error(self):
        from ._support import load_ui_module
        ui_module = load_ui_module(lambda *args, **kwargs: None)
        self.addCleanup(ui_module._restore_stubs_for_tests)
        dashboard_plan = ui_module.dashboard._dashboard_plan
        class DatabaseAPI:
            @staticmethod
            def has_id(_book_id):
                return True

            @staticmethod
            def format_abspath(_book_id, _format_name):
                return __file__

        class Database:
            new_api = DatabaseAPI()

        dialog = type("Dialog", (), {
            "gui": type("Gui", (), {"current_db": Database()})(),
        })()
        with patch.object(dashboard_plan, "cover_bytes",
                          side_effect=OSError("cover unavailable")):
            with patch.object(dashboard_plan, "log_warning") as warning:
                jobs, skipped = dashboard_plan.upload_jobs(
                    dialog,
                    {1: {"title": "Buch"}},
                    [UploadRecord(1, "u1", "EPUB", None)])
        self.assertEqual(1, len(jobs))
        self.assertIsNone(jobs[0].cover)
        self.assertEqual([], skipped)
        warning.assert_called_once()
        self.assertIn("cover unavailable", warning.call_args.args[0])

    def test_cover_bytes_uses_calibre_bytes_or_none_contract(self):
        class DatabaseAPI:
            def __init__(self, value):
                self.value = value

            def has_id(self, _book_id):
                return True

            def cover(self, _book_id, as_file=False):
                self.as_file = as_file
                return self.value

        class Database:
            def __init__(self, value):
                self.new_api = DatabaseAPI(value)

        for value in (b"cover", None):
            database = Database(value)
            self.assertIs(value, cover_bytes(database, 1))
            self.assertIs(value, library_cover_bytes(database, 1))
            self.assertFalse(database.new_api.as_file)

    def test_cover_bytes_rejects_invalid_ids(self):
        class DatabaseAPI:
            def has_id(self, _book_id):
                return True

            def cover(self, _book_id, as_file=False):
                return b"cover"

        class Database:
            new_api = DatabaseAPI()

        for invalid_id in (None, 0, -1, True, "42"):
            with self.subTest(invalid_id=invalid_id), self.assertRaises(ValueError):
                cover_bytes(Database(), invalid_id)

    def test_stale_stored_cloud_id_is_not_shown_as_a_cloud_match(self):
        metadata = {1: {"uuid": "u1", "title": "A", "authors": "Author",
                        "formats": ["EPUB"]}}
        state = {"u1": {"tolino_id": "deleted-cloud-id"}}
        rows = compare_inventory(metadata, state, [], ["EPUB"])
        self.assertEqual("new_in_calibre", rows[0]["status"])
        self.assertEqual("", rows[0]["tolino_id"])
        self.assertEqual("deleted-cloud-id", rows[0]["stale_cloud_id"])

    def test_inventory_comparison_matches_uuid_and_marks_changed(self):
        metadata = {
            1: {"uuid": "u1", "title": "A", "authors": "Author",
                "isbn": "123", "formats": ["EPUB"], "last_modified": "2"},
            2: {"uuid": "u2", "title": "B", "authors": "Writer",
                "isbn": "456", "formats": ["EPUB"], "last_modified": "1"},
        }
        state = {
            "u1": {"tolino_id": "t1", "meta_fp": metadata_fingerprint(metadata[1])},
            "u2": {"tolino_id": "t2", "meta_fp": metadata_fingerprint(metadata[2])},
        }
        rows = compare_inventory(
            metadata, state,
            [{"deliverableId": "t2", "uuid": "u2", "title": "B", "authors": "Writer"},
             {"deliverableId": "t1", "uuid": "u1", "title": "A", "authors": "Author"}],
            ["EPUB"],
        )
        self.assertEqual(["identical", "identical"], [row["status"] for row in rows])
        self.assertTrue(all(row["status"] == "identical" for row in rows))

    def test_custom_column_value_is_a_matching_fallback(self):
        metadata = {1: {"uuid": "u1", "title": "Changed title",
                        "formats": ["EPUB"], "#tolino_id": "bosh_8_saved"}}
        rows = compare_inventory(
            metadata, {}, [{"deliverableId": "bosh_8_saved", "title": "Remote title"}],
            ["EPUB"],
        )
        self.assertEqual("changed", rows[0]["status"])
        self.assertEqual("bosh_8_saved", rows[0]["tolino_id"])
        self.assertEqual("stored_id_or_uuid", rows[0]["match_reason"])

    def test_missing_columns_are_created_once_with_calibre_types(self):
        """Der Knopf legt nur fehlende Spalten an -- mit passendem Typ."""
        from .. import library as library_module
        library_module.CREATED_COLUMNS.clear()
        self.addCleanup(library_module.CREATED_COLUMNS.clear)

        class Database:
            field_metadata = {"#tolino_id": {"datatype": "text"}}

            def __init__(self):
                self.calls = []

            def create_custom_column(self, label, name, datatype,
                                     is_multiple, editable=True, display=None):
                self.calls.append((label, name, datatype, is_multiple,
                                   editable, display))
                self.field_metadata["#" + label] = {"datatype": datatype}
                return 1

        database = Database()
        self.assertEqual(["tolino_read", "tolino_ignore"],
                         create_custom_columns(database))
        self.assertEqual(
            [("tolino_read", "Gelesen (tolino)", "bool", False, True, {}),
             ("tolino_ignore", "Ignoriert (tolino)", "bool", False, True, {})],
            database.calls)
        # Zweiter Klick: nichts doppelt anlegen, auch wenn Calibre die neue
        # Spalte erst nach einem Neustart fuehrt.
        self.assertEqual([], create_custom_columns(database))

    def test_a_single_column_can_be_created_on_its_own(self):
        """Je Spalte ein Knopf: nur die fehlende Spalte wird angelegt."""
        from .. import library as library_module
        library_module.CREATED_COLUMNS.clear()
        self.addCleanup(library_module.CREATED_COLUMNS.clear)

        class Database:
            field_metadata = {"#tolino_id": {"datatype": "text"}}

            def __init__(self):
                self.calls = []

            def create_custom_column(self, label, name, datatype,
                                     is_multiple, editable=True, display=None):
                self.calls.append(label)
                self.field_metadata["#" + label] = {"datatype": datatype}
                return 1

        database = Database()
        self.assertEqual(["tolino_ignore"],
                         create_custom_columns(database, ("tolino_ignore",)))
        self.assertEqual(["tolino_ignore"], database.calls)
        # Die andere Spalte bleibt unangetastet, auch beim zweiten Klick.
        self.assertEqual([], create_custom_columns(database, ("tolino_ignore",)))
        self.assertEqual(["tolino_ignore"], database.calls)

    def test_a_present_column_needs_no_calibre_api(self):
        """Ist die Spalte schon da, gibt es keinen Fehler -- nur «schon da»."""
        from .. import library as library_module
        library_module.CREATED_COLUMNS.clear()
        self.addCleanup(library_module.CREATED_COLUMNS.clear)

        class WithoutColumns:
            field_metadata = {"#tolino_id": {"datatype": "text"}}

        self.assertEqual([], create_custom_columns(WithoutColumns(),
                                                   ("tolino_id",)))

    def test_the_column_types_match_what_the_plugin_reads(self):
        """Text fuer die ID, Ja/Nein fuer Lesestatus und Ignorieren."""
        self.assertEqual([("tolino_id", "tolino-ID", "text"),
                          ("tolino_read", "Gelesen (tolino)", "bool"),
                          ("tolino_ignore", "Ignoriert (tolino)", "bool")],
                         list(CUSTOM_COLUMNS))

    def test_without_the_calibre_api_the_error_names_the_columns(self):
        """Eine alte Calibre-Fassung bekommt eine umsetzbare Meldung."""
        class WithoutColumns:
            field_metadata = {}

        with self.assertRaises(RuntimeError) as caught:
            create_custom_columns(WithoutColumns())
        for label in ("#tolino_id", "#tolino_read", "#tolino_ignore"):
            self.assertIn(label, str(caught.exception))

    def test_custom_column_availability_accepts_calibre_field_metadata(self):
        class FieldMetadata:
            def __contains__(self, key):
                return key == TOLINO_COLUMN

        class Database:
            field_metadata = FieldMetadata()

        self.assertNotIsInstance(Database.field_metadata, dict)
        self.assertTrue(custom_column_available(Database()))

    def test_custom_column_writer_uses_the_calibre_new_api(self):
        class DatabaseAPI:
            def __init__(self):
                self.writes = []
                self.current = {7: ""}

            @staticmethod
            def has_id(book_id):
                return book_id == 7

            def get_metadata(self, book_id):
                return {TOLINO_COLUMN: self.current[book_id]}

            def set_field(self, field, values):
                self.writes.append((field, values))
                self.current.update(values)

        class LegacyDatabase:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def __init__(self):
                self.new_api = DatabaseAPI()

        database = LegacyDatabase()
        self.assertEqual(1, update_tolino_ids(
            database, [(7, "bosh_8_new")], account_name="Konto A"))
        self.assertEqual([(TOLINO_COLUMN,
                           {7: '{"Konto A":"bosh_8_new"}'})],
                         database.new_api.writes)
        self.assertEqual("bosh_8_new", metadata_tolino_id(
            {TOLINO_COLUMN: '{"Konto A":"bosh_8_new"}'}, "Konto A"))
        self.assertEqual("", metadata_tolino_id(
            {TOLINO_COLUMN: '{"Konto A":"bosh_8_new"}'}, "Konto B"))

    def test_custom_column_writes_only_when_available_and_enabled(self):
        """Geschrieben wird ueber set_field() -- nur bei vorhandener und
        eingeschalteter Spalte; ohne Spalte gibt es keinen SQL-Ersatzweg."""
        class DatabaseAPI:
            def __init__(self):
                self.writes = []

            def has_id(self, _book_id):
                return True

            def set_field(self, field, values):
                self.writes.append((field, values))

        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def __init__(self):
                self.new_api = DatabaseAPI()

        database = Database()
        self.assertTrue(custom_column_available(database))
        self.assertEqual(1, update_tolino_ids(database, [(7, "bosh_8_new")]))
        self.assertEqual(1, library_update_tolino_ids(
            database, [(7, "bosh_8_new")]))
        self.assertEqual([(TOLINO_COLUMN, {7: "bosh_8_new"})] * 2,
                         database.new_api.writes)
        # Nach dem Löschen einer Cloud-Kopie wird die Spalte geleert.
        self.assertEqual(1, update_tolino_ids(database, [(7, "")]))
        self.assertEqual((TOLINO_COLUMN, {7: ""}), database.new_api.writes[-1])

        class Gone(Database):
            def __init__(self):
                super().__init__()
                self.new_api.has_id = lambda _book_id: False
                self.new_api.set_field = lambda *_args: (_ for _ in ()).throw(
                    AssertionError("deleted books must not be written"))

        self.assertEqual(0, update_tolino_ids(Gone(), [(7, "")]))

        class Disabled(Database):
            def __init__(self):
                super().__init__()
                self.new_api.set_field = lambda *_args: (_ for _ in ()).throw(
                    AssertionError("disabled column must not be written"))

        self.assertEqual(0, update_tolino_ids(
            Disabled(), [(7, "bosh_8_new")], enabled=False))

        class Missing:
            field_metadata = {}

        self.assertFalse(custom_column_available(Missing()))
        self.assertEqual(0, update_tolino_ids(Missing(), [(7, "bosh_8_new")]))

    def test_account_column_update_preserves_and_clears_only_selected_account(self):
        class DatabaseAPI:
            def __init__(self):
                self.value = '{"Konto A":"id-a","Konto B":"id-b"}'
                self.writes = []

            @staticmethod
            def has_id(book_id):
                return book_id == 7

            def get_metadata(self, book_id):
                return {TOLINO_COLUMN: self.value}

            def set_field(self, field, values):
                self.writes.append((field, values))
                self.value = values[7]

        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def __init__(self):
                self.new_api = DatabaseAPI()

        database = Database()
        update_tolino_ids(database, [(7, "id-a-new")], account_name="Konto A")
        self.assertEqual("id-a-new", metadata_tolino_id(
            {TOLINO_COLUMN: database.new_api.value}, "Konto A"))
        self.assertEqual("id-b", metadata_tolino_id(
            {TOLINO_COLUMN: database.new_api.value}, "Konto B"))
        update_tolino_ids(database, [(7, "")], account_name="Konto A")
        self.assertEqual("", metadata_tolino_id(
            {TOLINO_COLUMN: database.new_api.value}, "Konto A"))
        self.assertEqual("id-b", metadata_tolino_id(
            {TOLINO_COLUMN: database.new_api.value}, "Konto B"))

    def test_legacy_column_id_is_adopted_by_active_account(self):
        class DatabaseAPI:
            value = "legacy-id"

            @staticmethod
            def has_id(book_id):
                return book_id == 7

            def get_metadata(self, _book_id):
                return {TOLINO_COLUMN: self.value}

            def set_field(self, _field, values):
                self.value = values[7]

        class Database:
            field_metadata = {TOLINO_COLUMN: {"datatype": "text"}}

            def __init__(self):
                self.new_api = DatabaseAPI()

        database = Database()
        self.assertEqual("legacy-id", metadata_tolino_id(
            {TOLINO_COLUMN: "legacy-id"}, "Konto A"))
        update_tolino_ids(database, [(7, "new-id")], account_name="Konto A")
        self.assertEqual('{"Konto A":"new-id"}', database.new_api.value)

    def test_metadata_tolino_id_accepts_calibre_style_mapping(self):
        self.assertEqual("bosh_8_id", metadata_tolino_id({"#tolino_id": " bosh_8_id "}))

    def test_inventory_comparison_uses_isbn_and_reports_remote_only(self):
        metadata = {
            1: {"uuid": "u1", "title": "Local", "authors": "Author",
                "isbn": "978-3-1614-8410-0", "formats": ["EPUB"]},
        }
        rows = compare_inventory(
            metadata, {},
            [{"id": "t1", "title": "Remote title", "authors": "Author",
              "isbn": "9783161484100"}, {"id": "t2", "title": "Cloud only"}],
            ["EPUB"],
        )
        self.assertEqual("changed", rows[0]["status"])
        self.assertEqual("t1", rows[0]["tolino_id"])
        self.assertEqual("only_tolino", rows[1]["status"])
        self.assertNotEqual("new_in_calibre", rows[1]["status"])

    def test_inventory_envelope_metadata_is_not_treated_as_a_cloud_record(self):
        inventory = {
            "PublicationInventory": {
                "edata": [{
                    "deliverableId": "cloud-book-1",
                    "epubMetaData": {
                        "title": "Gemeinsamer Titel",
                        "author": [{"name": "Eine Autorin"}],
                        "isbn": "9783161484100",
                    },
                }],
                "ebook": [],
                "count": 1,
            }
        }
        local = {
            1: {"uuid": "u1", "title": "Gemeinsamer Titel",
                "authors": "Eine Autorin", "isbn": "9783161484100",
                "formats": ["EPUB"]}
        }
        rows = compare_inventory(local, {}, inventory, ["EPUB"])
        self.assertEqual(1, len(rows))
        self.assertEqual("cloud-book-1", rows[0]["tolino_id"])
        self.assertEqual("identical", rows[0]["status"])

    def test_inventory_shape_uses_nested_metadata_and_keeps_id_technical(self):
        item = {
            "deliverableId": "bosh_8_543897186788677497438396920",
            "epubMetaData": {
                "title": "Der echte Tolino-Titel",
                "author": [{"name": "Eine Autorin"}],
                "isbn": "978-3-1234-5678-9",
            },
            "publicationId": "pub-1",
            "resellerId": "8",
        }
        record = normalize_inventory_record(item)
        self.assertEqual("Der echte Tolino-Titel", record["title"])
        self.assertEqual("Eine Autorin", record["authors"])
        self.assertEqual("978-3-1234-5678-9", record["isbn"])
        self.assertEqual("bosh_8_543897186788677497438396920",
                         record["tolino_id"])
        self.assertTrue(record["has_deliverable_id"])
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Der echte Tolino-Titel",
                 "authors": "Eine Autorin", "isbn": "9783123456789",
                 "formats": ["EPUB"]}},
            {}, {"PublicationInventory": {"edata": [item]}}, ["EPUB"],
        )
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("Der echte Tolino-Titel", rows[0]["title"])
        self.assertEqual("bosh_8_543897186788677497438396920", rows[0]["tolino_id"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_inventory_without_title_is_not_matchable_and_does_not_show_id_as_title(self):
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Local title", "formats": ["EPUB"]}},
            {}, [{"deliverableId": "bosh_8_543897186788677497438396920",
                  "epubMetaData": {"author": [{"name": "Author"}]}}],
            ["EPUB"],
        )
        self.assertEqual("new_in_calibre", rows[0]["status"])
        remote = rows[1]
        self.assertEqual("not_matchable", remote["status"])
        self.assertEqual("", remote["title"])
        self.assertEqual("bosh_8_543897186788677497438396920", remote["tolino_id"])
        self.assertIn("ohne Titel", remote["explanation"])

    def test_inventory_normalization_accepts_ebook_and_deliverable_shapes(self):
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Nested title",
                 "authors": "Nested author", "isbn": "978-1-2",
                 "formats": ["EPUB"]}},
            {}, {"PublicationInventory": {"ebook": [{"deliverable": {
                "deliverableId": "bosh_8_2",
                "title": "Nested title",
                "author": {"name": "Nested author"},
                "isbn13": "978-1-2",
            }}]}}, ["EPUB"],
        )
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("Nested author", normalize_inventory_record({
            "deliverable": {"author": {"name": "Nested author"},
                            "isbn13": "978-1-2"}
        })["authors"])
        self.assertEqual("978-1-2", normalize_inventory_record({
            "deliverable": {"author": {"name": "Nested author"},
                            "isbn13": "978-1-2"}
        })["isbn"])

    def test_a_cloud_copy_without_deliverable_id_is_review_not_an_upload(self):
        """Ein Kauf-Ebook traegt oft nur ``identifier``.

        Frueher fiel so ein Datensatz aus dem Vergleich; das lokale Buch
        galt dann als «noch nicht in tolino» -- mit vorausgewaehltem Upload
        und Duplikat als Folge. Jetzt ist es eine Prueffzeile ohne Auswahl.
        """
        state = {}
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Gekauftes Buch",
                 "authors": "Autorin", "isbn": "978-3-16-148410-0",
                 "formats": ["EPUB"]}},
            state,
            {"PublicationInventory": {"ebook": [{"resellerId": 3,
                        "deliverableId": "", "epubMetaData": {
                            "identifier": "978-3-16-148410-0",
                            "title": "Gekauftes Buch",
                            "author": [{"name": "Autorin"}]}}]}},
            ["EPUB"],
        )
        local = rows[0]
        self.assertEqual("review", local["status"])
        self.assertEqual("cloud_id_missing", local["match_reason"])
        self.assertNotEqual("new_in_calibre", local["status"])
        self.assertEqual("", local["tolino_id"])
        self.assertEqual({}, state)
        self.assertIn("nicht ansprechbar", local["explanation"])

    def test_a_cloud_only_copy_without_deliverable_id_is_visible(self):
        """Der Datensatz verschwindet nicht mehr aus der Anzeige."""
        rows = compare_inventory(
            {}, {},
            {"PublicationInventory": {"ebook": [{"deliverableId": "",
                "epubMetaData": {"identifier": "978-3-16-148410-0",
                                  "title": "Nur in tolino"}}]}},
            ["EPUB"],
        )
        self.assertEqual(1, len(rows))
        self.assertEqual("review", rows[0]["status"])
        self.assertTrue(rows[0]["cloud_id_missing"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])
        self.assertEqual("", rows[0]["tolino_id"])
        self.assertIn("nicht ansprechbar", rows[0]["explanation"])

    def test_a_deliverable_id_inside_a_list_is_usable_for_matching(self):
        """``epubMetaData.deliverable`` ist eine Liste -- kein Verlust."""
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Titel", "authors": "Autorin",
                 "isbn": "978-3-16-148410-0", "formats": ["EPUB"]}},
            {},
            {"PublicationInventory": {"ebook": [{"epubMetaData": {
                "title": "Titel", "author": [{"name": "Autorin"}],
                "deliverable": [{"deliverableId": "d5"}]}}]}},
            ["EPUB"],
        )
        self.assertEqual("d5", rows[0]["tolino_id"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_a_record_with_its_own_ebook_field_keeps_its_fields(self):
        """Ein Datensatz mit ``ebook`` ist kein Container."""
        item = {"ebook": {"hinweis": "x"}, "deliverableId": "d7",
                "epubMetaData": {"title": "Titel"}}
        normalized = normalize_inventory_record(item)
        self.assertEqual("d7", normalized["tolino_id"])
        self.assertTrue(normalized["has_deliverable_id"])
        self.assertEqual("Titel", normalized["title"])
        rows = compare_inventory(
            {}, {}, {"PublicationInventory": {"edata": [item]}}, ["EPUB"])
        self.assertEqual(1, len(rows))
        self.assertEqual("d7", rows[0]["tolino_id"])
        self.assertEqual("Titel", rows[0]["title"])

    def test_normalized_inventory_record_remains_comparable(self):
        """A second normalization yields the same canonical record."""
        record = normalize_inventory_record({
            "deliverableId": "d9", "title": "Titel",
            "author": [{"name": "Autorin"}],
            "epubMetaData": {"isbn": "978-3-16-148410-0"}})
        self.assertTrue(record["has_deliverable_id"])
        self.assertEqual(record, normalize_inventory_record(record))
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Titel", "authors": "Autorin",
                 "isbn": "978-3-16-148410-0", "formats": ["EPUB"]}},
            {}, [record], ["EPUB"])
        self.assertEqual("d9", rows[0]["tolino_id"])
        self.assertEqual("Titel", rows[0]["title"])
        self.assertEqual("Autorin", rows[0]["authors"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_a_partially_normalised_record_is_read_instead_of_crashing(self):
        """Nur ein VOLLSTAENDIG normalisiertes Dict darf unveraendert gelten.

        Ein einzelnes Feld wie ``has_deliverable_id`` reichte frueher als
        Erkennungsmerkmal; danach fehlten ``uuid``/``authors`` und der
        Vergleich brach mit KeyError ab.
        """
        rows = compare_inventory(
            {1: {"uuid": "u1", "title": "Titel", "authors": "Autorin",
                 "formats": ["EPUB"]}},
            {},
            [{"has_deliverable_id": True, "title": "Titel"}],
            ["EPUB"],
        )
        self.assertEqual(2, len(rows))
        remote = [row for row in rows if row["book_id"] is None][0]
        self.assertEqual("", remote["tolino_id"])
        self.assertEqual("review", remote["status"])
        self.assertTrue(remote["cloud_id_missing"])

    def test_normalizers_accept_unhashable_values_without_retaining_them(self):
        """Planner text inputs may be Calibre lists; normalization stays bounded."""
        self.assertEqual("abc 123", normalize_match_text(["ABC", 123]))
        self.assertEqual("123x", normalize_isbn(["12", "3X"]))
        self.assertEqual(("author", "name"), author_tokens(["Name", "Author"]))
        self.assertTrue(authors_match(["J. K. Rowling"], "Joanne K Rowling"))
        self.assertEqual("author name", normalized_authors(["Name", "Author"]))

    def test_title_alone_is_only_a_review_candidate(self):
        """Titel allein darf weder matchen noch «identical» ergeben."""
        self.assertEqual(normalize_title("Der «Überblick» – Band 2.epub"),
                         normalize_title("DER Überblick - Band 2"))
        metadata = {1: {"uuid": "u1", "title": "Der «Überblick» – Band 2",
                        "authors": "Andere", "formats": ["EPUB"]}}
        state = {}
        rows = compare_inventory(
            metadata, state, [{"id": "t1", "title": "DER Überblick - Band 2.pdf"}],
            ["EPUB"],
        )
        self.assertEqual("review", rows[0]["status"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])
        self.assertEqual("title_only", rows[0]["match_reason"])
        self.assertEqual("", rows[0]["tolino_id"])
        self.assertEqual({}, state)
        # Der Cloud-Datensatz bleibt als möglicher Treffer markiert.
        self.assertEqual("review", rows[1]["status"])

    def test_duplicate_titles_are_ambiguous_not_identical(self):
        metadata = {
            1: {"uuid": "u1", "title": "The Book", "authors": "A",
                "formats": ["EPUB"]},
            2: {"uuid": "u2", "title": "The Book", "authors": "B",
                "formats": ["EPUB"]},
        }
        state = {}
        rows = compare_inventory(
            metadata, state,
            [{"id": "t1", "title": "the book"}, {"id": "t2", "title": "THE BOOK"}],
            ["EPUB"],
        )
        self.assertEqual({"review"}, {row["status"] for row in rows})
        self.assertEqual(4, len(rows))
        self.assertTrue(all(row["status"] != "new_in_calibre" for row in rows))
        self.assertFalse(any(row.get("tolino_id") for row in rows
                             if row.get("book_id") is not None))
        self.assertEqual({}, state)

    def test_upload_gate_follows_selection_and_stored_signatures(self):
        """Uploads follow the confirmed choice; state stores separate signals."""
        unchanged = {"u1": {"tolino_id": "t1",
                             "content_fp": content_fingerprint(self.book, "EPUB"),
                             "meta_fp": metadata_fingerprint(self.book)}}
        self.assertEqual([], plan_sync({1: self.book}, unchanged, ["EPUB"],
                                       upload_book_ids=set()))
        changed = dict(self.book, title="Changed")
        self.assertEqual([], plan_sync({1: changed}, unchanged, ["EPUB"],
                                       upload_book_ids=set()))
        self.assertEqual(
            [(1, "u1", "EPUB", "t1")],
            plan_sync({1: self.book}, unchanged, ["EPUB"],
                      upload_book_ids={1}))

    def test_plan_sync_only_builds_selected_uploads(self):
        state = {"gone": {"tolino_id": "t2", "fingerprint": "x"}}
        self.assertEqual([], plan_sync({}, state, ["EPUB"], upload_book_ids=set()))
        self.assertEqual([], plan_sync({1: dict(self.book)}, state,
                                       ["EPUB"], upload_book_ids=set()))

    def test_stale_cloud_id_is_not_carried_into_upload_plan(self):
        metadata = {1: {"uuid": "u1", "title": "A", "formats": ["EPUB"]}}
        state = {"u1": {"tolino_id": "deleted-cloud-id"}}
        row = compare_inventory(metadata, state, [], ["EPUB"])[0]
        self.assertEqual("", row["tolino_id"])
        self.assertEqual("deleted-cloud-id", row["stale_cloud_id"])
        state["u1"].pop("tolino_id")
        uploads = plan_sync(
            metadata, state, ["EPUB"], upload_book_ids={1})
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)

    def test_unexpected_deletion_argument_is_not_supported(self):
        with self.assertRaises(TypeError):
            plan_sync({}, {}, ["EPUB"], upload_book_ids=set(), deletions=True)

    def test_books_without_upload_format_are_not_planned(self):
        pdf_only = {"uuid": "u-pdf", "title": "Nur PDF", "formats": ["PDF"]}
        state = {"u-pdf": {"tolino_id": "t-pdf"}}
        self.assertEqual([], plan_sync(
            {1: pdf_only}, state, ["EPUB"], upload_book_ids=set()))
        self.assertEqual({"u-pdf": {"tolino_id": "t-pdf"}}, state)

    def test_selected_replacement_keeps_the_old_cloud_id_in_its_job(self):
        state = {"u1": {"tolino_id": "t1"}}
        uploads = plan_sync(
            {1: dict(self.book)}, state, ["EPUB"],
            upload_book_ids={1})
        self.assertEqual([(1, "u1", "EPUB", "t1")], uploads)

    def test_invalid_or_empty_uuids_are_ignored_for_upload_jobs(self):
        existing = {1: dict(self.book)}
        state = {
            "": {"tolino_id": "t-empty"},
            "   ": {"tolino_id": "t-space"},
            "none": {"tolino_id": "t-none"},
            "u1": {"tolino_id": "t-still-here"},
            "u-gone": {"tolino_id": "t-gone"},
        }
        uploads = plan_sync(
            existing, state, ["EPUB"], upload_book_ids=set())
        self.assertEqual([], uploads)

    def test_reference_partners_and_stable_hardware_shape(self):
        for partner_id in (1, 2, 4, 5, 6, 7, 8):
            self.assertIn(partner_id, PARTNERS)
        # Hardware IDs use the web reader's 8-4-4-4-12 UUID format.
        self.assertRegex(hardware_id(), r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

    def test_reseller_id_matches_protocol_partner(self):
        # The protocol reseller IDs stay stable while plugin IDs are
        # consecutive; Orell Fuessli keeps reseller 8 (new plugin ID 4).
        self.assertEqual("8", PARTNERS[4]["reseller_id"])
        self.assertEqual("3", PARTNERS[1]["reseller_id"])
        self.assertEqual("13", PARTNERS[5]["reseller_id"])

    def test_compact_hardware_id_is_normalized_to_uuid(self):
        self.assertEqual(
            "aff2e436-beb8-53e8-23b8-e02163f55220",
            normalize_hardware_id("aff2e436beb853e823b8e02163f55220"))
        self.assertEqual(
            "dc37788f-8ff3-4e8e-b0e3-5059c3c08ce1",
            normalize_hardware_id("dc37788f-8ff3-4e8e-b0e3-5059c3c08ce1"))
        self.assertEqual("", normalize_hardware_id(""))
        self.assertEqual("custom-device", normalize_hardware_id("custom-device"))

    def test_partner_8_refresh_configuration_matches_reference(self):
        self.assertEqual("webreader", PARTNERS[4]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[4]["scope"])
        self.assertEqual("https://www.orellfuessli.ch/auth/oauth2/token",
                         PARTNERS[4]["token_url"])
        self.assertEqual("https://webreader.mytolino.com/library/",
                         WEBREADER_URL)
        self.assertEqual("TOLINO_WEBREADER", WEBREADER_CLIENT_TYPE)
        self.assertEqual("5.2.0", WEBREADER_CLIENT_VERSION)
        self.assertEqual("https://webreader.mytolino.com",
                         WEBREADER_TOKEN_HEADERS["Origin"])
        self.assertEqual("https://webreader.mytolino.com/",
                         WEBREADER_TOKEN_HEADERS["Referer"])
        self.assertNotIn("client_type", PARTNERS[4])
        self.assertNotIn("client_version", PARTNERS[4])
        self.assertNotIn("token_headers", PARTNERS[4])

    def test_refresh_token_normalization_strips_quotes_and_whitespace(self):
        token = normalize_refresh_token('  "refresh-secret-value"  ')
        self.assertEqual("refresh-secret-value", token)

    def test_partner_8_authenticated_headers_match_web_reader(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def request(request, timeout):
            captured["headers"] = request.headers
            return Response()

        with patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(4, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  "refresh-token")
            client.access = "access-token"
            client.expires_at = time.time() + 60
            client._request("https://bosh.pageplace.de/bosh/rest/inventory/delta")
        headers = {key.casefold(): value for key, value in captured["headers"].items()}
        self.assertEqual("8", headers["reseller_id"])
        self.assertEqual("TOLINO_WEBREADER", headers["client_type"])
        self.assertEqual("5.2.0", headers["client_version"])

    def test_partner_8_refresh_payload_is_url_encoded_and_not_access_token(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"access_token":"access-secret","expires_in":3600}'

        def request(request, timeout):
            captured["url"] = request.full_url
            captured["body"] = request.data.decode("utf-8")
            captured["content_type"] = request.headers["Content-type"]
            captured["headers"] = request.headers
            return Response()

        with patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(4, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  " refresh-token ")
            client.login()
        self.assertEqual(PARTNERS[4]["token_url"], captured["url"])
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])
        self.assertEqual("application/x-www-form-urlencoded",
                         captured["content_type"])
        headers = {key.casefold(): value for key, value in captured["headers"].items()}
        self.assertEqual("https://webreader.mytolino.com", headers["origin"])
        self.assertEqual("https://webreader.mytolino.com/", headers["referer"])
        self.assertIn("scope=SCOPE_BOSH", captured["body"])
        self.assertNotIn("client_type", headers)
        self.assertNotIn("client_version", headers)
        self.assertNotIn("reseller_id", headers)
        self.assertNotIn("cookie", headers)
        self.assertNotIn("authorization", headers)
        self.assertEqual("refresh-token", client.refresh)
        self.assertNotEqual("access-secret", client.refresh)

    def test_rotated_refresh_token_is_returned_and_persisted_before_followup_work(self):
        captured = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return (b'{"access_token":"access-secret",'
                        b'"refresh_token":"rotated-refresh","expires_in":3600}')

        with patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            client = TolinoClient(4, "hardware", "old-refresh",
                                  token_callback=captured.append)
            self.assertEqual("rotated-refresh", client.login())
            self.assertEqual(["rotated-refresh"], captured)
            self.assertEqual("rotated-refresh", client.refresh)
            # A second call uses the cached access token and cannot reuse the old grant.
            self.assertEqual("rotated-refresh", client.login())

    def test_reuse_exceeded_is_not_retried_and_gives_user_action(self):
        from urllib.error import HTTPError

        body = b'{"error":"invalid_grant","error_description":"Maximum allowed refresh token reuse exceeded"}'
        error = HTTPError("https://example.invalid/token", 400, "Bad Request", {}, None)
        error.read = lambda: body
        with patch("calibre_plugin.tolino.urlopen", side_effect=error) as request:
            with self.assertRaisesRegex(TolinoAuthError, "Anmeldung abgelehnt"):
                TolinoClient(4, "", "old-refresh").login()
        self.assertEqual(1, request.call_count)

    def test_403_with_invalid_grant_body_gives_user_action(self):
        from urllib.error import HTTPError

        body = b'{"error":"invalid_grant","error_description":"token reused"}'
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        with patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Anmeldung abgelehnt"):
                TolinoClient(4, "", "old-refresh").login()

    def test_403_bot_protection_reports_response_detail(self):
        from urllib.error import HTTPError

        body = b"Access denied | bot protection"
        error = HTTPError("https://example.invalid/token", 403, "Forbidden", {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", "old-refresh")
        with patch("calibre_plugin.tolino.urlopen", side_effect=error), \
                        patch("calibre_plugin.tolino.time.sleep"):
            with self.assertRaisesRegex(TolinoAuthError, "Access denied"):
                client.login()
        self.assertIn("Access denied", client.last_error_text)

    def test_waf_403_on_token_endpoint_waits_and_retries(self):
        """Feldbefund: der erste Token-Aufruf nach der Browser-
        Anmeldung traf 403 (Bot-Schutz-Laufzeitbahn), jeder spaetere
        Klick lief – jetzt wird kurz gewartet und zwei Mal wiederholt."""
        from urllib.error import HTTPError

        bot = HTTPError("https://example.invalid/token", 403, "Forbidden",
                        {}, None)
        bot.read = lambda: (b"<!doctype html><title>Zugriff geblockt"
                            b"</title>")
        calls = []
        sleeps = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return (b'{"access_token":"access-secret",'
                        b'"refresh_token":"rotated-refresh","expires_in":3600}')

        def fake_urlopen(request, timeout=None):
            calls.append(request.full_url)
            if len(calls) < 3:
                raise bot
            return Response()

        with patch("calibre_plugin.tolino.urlopen",
                   side_effect=fake_urlopen), \
                patch("calibre_plugin.tolino.time.sleep",
                      side_effect=sleeps.append):
            client = TolinoClient(4, "hardware", "old-refresh")
            self.assertEqual("rotated-refresh", client.login())
        self.assertEqual(3, len(calls))
        self.assertEqual([2.0, 5.0], sleeps)
        self.assertEqual("rotated-refresh", client.refresh)

    def test_other_partner_refresh_payload_keeps_configured_scope(self):
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"access_token":"access-secret","expires_in":3600}'

        def request(request, timeout):
            captured["body"] = request.data.decode("utf-8")
            return Response()

        with patch("calibre_plugin.tolino.urlopen", request):
            TolinoClient(1, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                         "refresh-token").login()
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])

    def test_sanitize_error_redacts_configured_variants_jwt_and_traceback(self):
        refresh = '  "refresh-secret-value"  '
        access = "access-secret-value"
        jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.signature"
        traceback_text = (
            'Tolino HTTP 400: {"error":"invalid_grant",'
            '"error_description":"refresh_token=%s access_token=%s"}\n'
            "Authorization: Bearer %s\n"
            "Traceback: leaked refresh-secret-value"
        ) % (refresh.strip()[1:-1], access, jwt)
        rendered = sanitize_error(traceback_text, (refresh, access))
        self.assertIn("invalid_grant", rendered)
        self.assertNotIn("refresh-secret-value", rendered)
        self.assertNotIn(access, rendered)
        self.assertNotIn(jwt, rendered)
        self.assertIn("[REDACTED]", rendered)

    def test_http_error_keeps_safe_fields_without_echoing_tokens(self):
        from urllib.error import HTTPError

        token = "refresh-secret-value"
        body = ('{"error":"invalid_grant","error_description":"bad %s",'
                '"refresh_token":"%s"}' % (token, token)).encode("utf-8")
        error = HTTPError("https://example.invalid/token", 400, "Bad Request",
                          {}, None)
        error.read = lambda: body
        client = TolinoClient(4, "", token)
        with patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "Anmeldung abgelehnt"):
                client.login()
        self.assertIn("invalid_grant", client.last_error_text)
        self.assertIn("error_description", client.last_error_text)
        self.assertNotIn(token, client.last_error_text)

    def test_first_install_prefers_epub_only(self):
        """Erstinstallation: nur EPUB als bevorzugtes Format."""
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            self.assertEqual(["EPUB"], config.settings()["preferred_formats"])
        finally:
            config.PREFERENCES = original

    def test_settings_do_not_return_obsolete_options(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = config.settings()
            for key in ("enable_deletions", "compare_authors",
                        "compare_title", "compare_isbn"):
                self.assertNotIn(key, values)
        finally:
            config.PREFERENCES = original

    def test_preferred_formats_are_a_global_setting(self):
        self.assertNotIn(config.PREFERRED_FORMATS, config.ACCOUNT_KEYS)
        self.assertIsInstance(
            config.settings()[config.PREFERRED_FORMATS], list)

    def test_settings_only_commits_when_it_writes_a_value(self):
        """Lesen committet nie; nur save_account schreibt."""
        before = dict(config.PREFERENCES)
        commits = []
        original = config.PREFERENCES.commit
        config.PREFERENCES.commit = lambda: commits.append(1)
        try:
            config.settings()
            self.assertEqual([], commits)
            config.save_account("konto", {"refresh_token": "tok"},
                                active="konto")
            self.assertEqual(1, len(commits))
        finally:
            config.PREFERENCES.commit = original
            config.PREFERENCES.clear()
            config.PREFERENCES.update(before)
    def test_configuration_contract_keeps_supported_preference_keys(self):
        """Gespeicherte UI-Optionen und Konten haben feste Schluessel."""
        legacy_global = ("preferred_formats",)
        supported_accounts = ("partner_id", "hardware_id", "refresh_token",
                              "state")
        self.assertEqual((config.PREFERRED_FORMATS,), legacy_global)
        self.assertEqual(supported_accounts, config.ACCOUNT_KEYS)
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.settings()
            config.save_settings({config.PREFERRED_FORMATS: ["PDF"]})
            values = config.settings()
            for key in legacy_global:
                self.assertIn(key, values)
        finally:
            config.PREFERENCES = original

    def test_plugin_metadata_keeps_the_calibre_entry_point(self):
        """Calibre findet die Oberflaeche nur unter dem festen Modulpfad."""
        metadata_path = Path(__file__).resolve().parents[2] / "__init__.py"
        metadata = runpy.run_path(str(metadata_path))
        plugin = metadata["TolinoSyncPlugin"]
        self.assertEqual("Tolino Cloud Sync", plugin.name)
        self.assertEqual(metadata["PLUGIN_VERSION"], tuple(plugin.version))
        self.assertEqual(
            "calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction",
            plugin.actual_plugin)
        self.assertFalse(hasattr(plugin, "icon"))
        self.assertFalse(hasattr(plugin, "has_html"))
        self.assertEqual((5, 0, 0), tuple(plugin.minimum_calibre_version))
        self.assertEqual("InterfaceAction", plugin.type)

    def test_report_error_logs_no_tokens_passwords_or_cookies(self):
        """Auch die Logzeile und der Traceback enthalten keine Geheimnisse."""
        from ..tolino import report_error

        refresh = "refresh-secret-log"
        password = "password-secret-log"
        cookie = "cookie-secret-log"
        access = "access-secret-log"
        logged = []
        with patch("calibre_plugin.tolino.log_warning",
                   side_effect=logged.append):

            error = TolinoAuthError(
                "refresh_token=%s password=%s Cookie: session=%s"
                % (refresh, password, cookie),
                code="A02", detail="access_token=%s" % access)
            message = report_error(error, secrets=(refresh, password, cookie,
                                                   access))
        rendered = "\n".join(logged) + "\n" + message
        for secret in (refresh, password, cookie, access):
            self.assertNotIn(secret, rendered)
        self.assertIn("[REDACTED]", "\n".join(logged))

    def test_obsolete_cover_and_collection_options_are_not_returned(self):
        values = config.settings()
        self.assertNotIn("sync_collections", values)
        self.assertNotIn("upload_covers", values)

    def test_partial_settings_ignore_unscoped_legacy_account_fields(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES["refresh_token"] = "configured-token"
            values = config.settings()
            self.assertEqual("", values["refresh_token"])
            self.assertEqual("", values["hardware_id"])
            self.assertNotIn("enable_deletions", values)
            self.assertIsInstance(values["state"], dict)
            self.assertEqual("configured-token", config.PREFERENCES["refresh_token"])
        finally:
            config.PREFERENCES = original

    def test_named_accounts_keep_tokens_and_state_isolated(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.settings()
            config.save_account("alice", {"partner_id": 1, "refresh_token": "a",
                                          "state": {"u": {"tolino_id": "a"}}},
                                active="alice")
            config.save_account("bob", {"partner_id": 8, "refresh_token": "b",
                                        "state": {"u": {"tolino_id": "b"}}},
                                active="bob")
            values = config.settings()
            accounts = {item["name"]: item for item in values["accounts"]}
            self.assertEqual("bob", values["active_account"])
            self.assertEqual("a", accounts["alice"]["state"]["u"]["tolino_id"])
            self.assertEqual("b", accounts["bob"]["state"]["u"]["tolino_id"])
            self.assertEqual("a", accounts["alice"]["refresh_token"])
            self.assertEqual("b", accounts["bob"]["refresh_token"])
        finally:
            config.PREFERENCES = original

    def test_jwt_helpers_parse_iat_and_age(self):
        """JWT payload parsing extracts iat and shape."""
        import base64 as b64
        from ..token_candidates import _refresh_token_iat, _jwt_shaped

        def make_jwt(payload):
            head = b64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
            body = b64.urlsafe_b64encode(json.dumps(payload).encode())
            return "%s.%s.sig" % (head.decode().rstrip("="),
                                  body.decode().rstrip("="))

        now = time.time()
        fresh = make_jwt({"iat": now - 30, "typ": "Refresh"})
        self.assertEqual(now - 30, _refresh_token_iat(fresh))
        self.assertIsNone(_refresh_token_iat("plain-not-a-jwt"))
        self.assertTrue(_jwt_shaped(fresh))
        self.assertFalse(_jwt_shaped("short.not.jwt"))


    def test_keepalive_reuses_the_locked_login_factory(self):
        class FakeClient:
            def __init__(self, partner, hw, token, token_callback=None,
                         hardware_callback=None):
                self.refresh = token + "-rotated"
                self.token_callback = token_callback

            def login(self):
                self.token_callback(self.refresh)

        creds = {"name": "konto", "partner_id": 4, "hardware_id": "hw",
                 "refresh_token": "tok"}
        with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
             patch("calibre_plugin.config.account_by_name",
                   return_value={}):
            with patch.object(vault_module.TOKEN_VAULT, "login",
                              wraps=vault_module.TOKEN_VAULT.login) as login:
                result = vault_module.TOKEN_VAULT.keepalive_refresh(
                    lambda: {"accounts": [creds]})
        self.assertEqual("tok-rotated", result)
        login.assert_called_once()
        self.assertEqual("tok-rotated", creds["refresh_token"])

    def test_rotation_persister_writes_each_new_value_exactly_once(self):
        """Login darf denselben Token zweimal melden; gespeichert wird einmal."""
        saved = []
        persist = vault_module.TOKEN_VAULT._callback_for_value("old", saved.append)
        persist("new")
        persist("new")
        persist("newer")
        persist(None)
        persist("old")
        self.assertEqual(["new", "newer", "old"], saved)
        # Ohne Speicherfunktion bleibt die Rotation trotzdem nutzbar.
        no_store = vault_module.TOKEN_VAULT._callback_for_value("a", None)
        no_store("b")
        no_store("b")

    def test_keepalive_refresh_skips_when_there_is_nothing_to_do(self):
        """Ohne Token und bei laufender Rotation passiert nichts."""
        with patch("calibre_plugin.token_vault.TolinoClient") as client_mock:
            self.assertIsNone(
                vault_module.TOKEN_VAULT.keepalive_refresh(lambda: {}))
            client_mock.assert_not_called()
            with vault_module.TOKEN_VAULT._lock:
                self.assertIsNone(vault_module.TOKEN_VAULT.keepalive_refresh(
                    lambda: {"refresh_token": "tok"}))
            client_mock.assert_not_called()

class MetadataUpdatePlanTests(unittest.TestCase):
    """In-place Metadaten-Updates (/meta) nur für ausgewählte Bücher.

    Jede Zeile im Bestandsvergleich hat zwei Auswahlfelder: "Hochladen" lädt die
    Buchdatei, "Metadaten" gleicht nur Titel/Autor(en)/ISBN in der Cloud ab.
    Es gibt keine stille Umwandlung des einen in das andere.
    """

    def _book(self, **extra):
        book = {"uuid": "u1", "title": "A", "authors": "Fred Vargas",
                "isbn": "123", "series": "Adamsberg",
                "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "100:1000"}}
        book.update(extra)
        return book

    def _state(self, book, **extra):
        entry = {"tolino_id": "d1",
                 "content_fp": content_fingerprint(book, "EPUB"),
                 "meta_fp": metadata_fingerprint(book)}
        entry.update(extra)
        return {"u1": entry}

    def test_selected_book_is_updated_in_place(self):
        book = self._book()
        state = self._state(book, meta_fp="stale")
        updates = plan_metadata_updates({1: book}, state, [], {1})
        self.assertEqual(1, len(updates))
        record = updates[0]
        self.assertEqual(1, record.book_id)
        self.assertEqual("u1", record.book_uuid)
        self.assertEqual("d1", record.tolino_id)
        self.assertEqual({"title": "A", "author": "Fred Vargas", "isbn": "123"},
                         record.fields)

    def test_unchecked_books_are_never_touched(self):
        """Ohne Auswahl kein /meta-Schreibvorgang – auch wenn sich die
        Metadaten unterscheiden (genau das war vorher unsichtbar)."""
        book = self._book()
        state = self._state(book, meta_fp="stale")
        for only in (set(), {"2"}):
            updates = plan_metadata_updates({1: book}, state, [], only)
            self.assertEqual([], updates)

    def test_selected_upload_is_never_silently_converted(self):
        """"Hochladen" heisst hochladen – kein heimliches /meta."""
        book = self._book()
        state = self._state(book, meta_fp="stale")
        uploads = [UploadRecord(1, "u1", "EPUB", "d1")]
        updates = plan_metadata_updates({1: book}, state, uploads, {1})
        self.assertEqual([], updates)

    def test_replaced_file_stays_a_full_upload(self):
        book = self._book()
        state = self._state(book, content_fp="other-file", meta_fp="stale")
        uploads = [UploadRecord(1, "u1", "EPUB", "d1")]
        updates = plan_metadata_updates({1: book}, state, uploads, {1})
        self.assertEqual([], updates)

    def test_selected_metadata_update_does_not_need_the_book_file(self):
        """Metadaten lassen sich ohne ausgewählte Buchdatei patchen."""
        book = self._book(format_signatures={})
        state = {"u1": {"tolino_id": "d1", "meta_fp": "stale"}}
        updates = plan_metadata_updates({1: book}, state, [], {1})
        self.assertEqual(1, len(updates))

    def test_metadata_update_does_not_require_a_preferred_upload_format(self):
        """PDF-only/formatless books still support in-place metadata updates."""
        book = self._book(formats=["PDF"], format_signatures={})
        state = {"u1": {"tolino_id": "d1", "meta_fp": "stale"}}
        updates = plan_metadata_updates(
            {1: book}, state, [], {1})
        self.assertEqual(1, len(updates))
        self.assertEqual("d1", updates[0][2])

    def test_legacy_state_without_fingerprints_is_updated_when_selected(self):
        """Ohne gespeicherte Fingerabdrücke ist der Cloud-Stand unbekannt --
        wer auswählt, bekommt den Abgleich."""
        book = self._book()
        state = {"u1": {"tolino_id": "d1", "fingerprint": "x"}}
        updates = plan_metadata_updates({1: book}, state, [], {1})
        self.assertEqual(1, len(updates))

    def test_selected_metadata_update_is_planned(self):
        book = self._book()
        state = self._state(book, meta_fp="stale")
        updates = plan_metadata_updates({1: book}, state, [], {1})
        self.assertEqual(1, len(updates))
        self.assertEqual((1, "u1", "d1"), (updates[0][0], updates[0][1],
                                           updates[0][2]))

    def test_selected_metadata_update_reconciles_even_if_fingerprint_matches(self):
        book = self._book()
        state = self._state(book)
        updates = plan_metadata_updates({1: book}, state, [], {1})
        self.assertEqual(1, len(updates))

    def test_comparison_detected_metadata_difference_is_not_filtered_by_fp(self):
        """Ein Upload kann die Tolino-ID und den lokalen Fingerabdruck bereits
        gespeichert haben, obwohl die Cloud-Metadaten abweichen. Eine dann
        explizit gewaehlte Metadatenaktion muss trotzdem eingeplant werden."""
        book = self._book()
        state = self._state(book)
        rows = compare_inventory(
            {1: book}, state,
            [{"deliverableId": "d1", "title": "Alter Titel",
              "authors": "Fred Vargas", "isbn": "123"}],
            ["EPUB"])
        self.assertEqual("changed", rows[0]["status"])
        self.assertFalse(rows[0]["file_changed"])
        updates = plan_metadata_updates(
            {1: book}, state, [], {rows[0]["book_id"]})
        self.assertEqual(1, len(updates))
        self.assertEqual("d1", updates[0][2])

    def test_book_without_cloud_copy_is_skipped(self):
        book = self._book()
        updates = plan_metadata_updates({1: book}, {}, [], {1})
        self.assertEqual([], updates)

    def test_uuid_object_and_uppercase_form_find_the_cloud_copy(self):
        """Die Kennung kommt aus der Datenbankschicht: `Metadata.uuid` kann
        ein `uuid.UUID`-Objekt sein, und dieselbe Kennung kann als
        Grossbuchstabe ankommen. Beides muss den Zustandseintrag treffen --
        ein blosses `str()` verfehlt die normalisierte Kennung, und der
        Metadaten-Abgleich waere ein stiller Leerlauf.
        """
        canonical = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"
        state = {canonical: {"tolino_id": "d1", "meta_fp": "stale"}}
        for raw in (uuid.UUID(canonical), canonical.upper(),
                    "UUID('%s')" % canonical):
            with self.subTest(raw=raw):
                book = self._book(uuid=raw)
                updates = plan_metadata_updates(
                    {1: book}, state, [], {1})
                self.assertEqual(1, len(updates))
                self.assertEqual(canonical, updates[0][1])

    def test_series_change_is_updated_without_sending_the_series(self):
        """Serien stehen nicht im /meta-Payload (dafür gibt es die
        Sammlungs-Zuordnung), zählen aber als Änderung."""
        old_book = self._book()
        new_book = self._book(series="Neue Reihe")
        state = {"u1": {"tolino_id": "d1",
                        "content_fp": content_fingerprint(new_book, "EPUB"),
                        "meta_fp": metadata_fingerprint(old_book)}}
        updates = plan_metadata_updates({1: new_book}, state, [], {1})
        self.assertEqual(1, len(updates))
        self.assertNotIn("series", updates[0][3])

    def test_plan_sync_preserves_old_state_until_the_worker_succeeds(self):
        previous = {"u1": {"tolino_id": "d1", "content_fp": "old-content",
                           "meta_fp": "old-meta", "collections": ["Serie"]}}
        book = self._book(title="Neu",
                          format_signatures={"EPUB": "200:2000"})
        uploads = plan_sync({1: book}, previous, ["EPUB"], upload_book_ids={1})
        self.assertEqual([(1, "u1", "EPUB", "d1")], uploads)
        self.assertEqual("old-content", previous["u1"]["content_fp"])
        self.assertEqual("old-meta", previous["u1"]["meta_fp"])
        self.assertEqual(["Serie"], previous["u1"]["collections"])

    def test_metadata_update_fields_skips_empty_values(self):
        self.assertEqual({}, metadata_update_fields({"title": " ", "isbn": ""}))
        self.assertEqual({"title": "T", "author": "A, B"},
                         metadata_update_fields({"title": " T ",
                                                 "authors": ["A", "B"]}))

class CollectionPlanTests(unittest.TestCase):
    """Serien → Sammlungen: Planung ohne Cloud-Zugriff.

    Der Abgleich selbst ist idempotent – hier wird nur geprüft, welche
    Paare überhaupt in Frage kommen.
    """

    def test_series_map_keeps_only_books_with_a_series(self):
        metadata = {
            1: {"uuid": "u1", "series": "Adamsberg"},
            2: {"uuid": "u2", "series": "   "},
            3: {"uuid": "", "series": "Ohne UUID"},
            4: {"uuid": "u4"},
        }
        self.assertEqual({"u1": "Adamsberg"},
                         series_map_from_metadata(metadata))
        self.assertEqual({}, series_map_from_metadata(None))

    def test_pairs_only_include_books_already_in_the_cloud(self):
        series_map = {"u1": "Adamsberg", "u2": "Adamsberg", "u3": "Neu"}
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": ""},
                 "u3": {}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates(series_map, state))

    def test_pairs_are_sorted_and_deduplicated(self):
        series_map = {"u1": "SciFi", "u2": "Adamsberg"}
        state = {"u1": {"tolino_id": "t2"}, "u2": {"tolino_id": "t1"}}
        self.assertEqual([("u2", "t1", "Adamsberg"), ("u1", "t2", "SciFi")],
                         plan_collection_updates(series_map, state))

    def test_pairs_are_empty_for_missing_input(self):
        self.assertEqual([], plan_collection_updates(None, {"u": {}}))
        self.assertEqual([], plan_collection_updates({"u": "Serie"}, None))
        self.assertEqual([], plan_collection_updates({}, {}))

    def test_remembered_collection_is_not_planned_again(self):
        """Feldbefund: «Serie in der Cloud wird jedes Mal ausgeführt».
        Einmal geschrieben heisst gemerkt – der nächste Sync plant nichts
        und fragt die Cloud gar nicht erst."""
        series_map = {"u1": "Adamsberg"}
        state = {"u1": {"tolino_id": "t1"}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates(series_map, state))
        self.assertTrue(remember_collection(state, "u1", "Adamsberg"))
        self.assertEqual(["Adamsberg"], state["u1"]["collections"])
        self.assertEqual([], plan_collection_updates(series_map, state))
        # Doppeltes Merken ändert nichts.
        self.assertFalse(remember_collection(state, "u1", "Adamsberg"))
        # Eine zweite Serie wird weiterhin geplant.
        series_map["u2"] = "Neu"
        state["u2"] = {"tolino_id": "t2"}
        self.assertEqual([("u2", "t2", "Neu")],
                         plan_collection_updates(series_map, state))

    def test_recorded_collections_ignore_broken_state(self):
        state = {"u1": {"tolino_id": "t1", "collections": "kaputt"}}
        self.assertEqual([("u1", "t1", "Adamsberg")],
                         plan_collection_updates({"u1": "Adamsberg"}, state))
        self.assertEqual(set(), recorded_collections(None))
        self.assertEqual(set(), recorded_collections({"collections": ["  "] }))
        self.assertFalse(remember_collection(None, "u1", "Adamsberg"))
        self.assertFalse(remember_collection({}, "", "Adamsberg"))
        self.assertFalse(remember_collection({}, "u1", "   "))

    def test_recorded_collections_survive_plan_sync(self):
        """Der Zustand wird bei jedem Sync neu aufgebaut – die gemerkten
        Zuordnungen dürfen dabei nicht verloren gehen."""
        book = {"uuid": "u1", "title": "A", "authors": "A", "isbn": "1",
                "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "1:1"}}
        state = {"u1": {"tolino_id": "t1", "collections": ["Adamsberg"]}}
        plan_sync({1: book}, state, ["EPUB"], upload_book_ids=set())
        self.assertEqual(["Adamsberg"], state["u1"]["collections"])
        self.assertEqual("t1", state["u1"]["tolino_id"])

class ConservativeMatchingTests(unittest.TestCase):
    """Titel allein, widersprechende Felder und Mehrdeutigkeit (P1/B)."""

    def test_sync_planning_exports_are_compatible(self):
        from ..plan import (            plan_collection_updates as sync_collections,
                            plan_metadata_updates as sync_metadata,
                            plan_sync as sync_plan_sync)

        self.assertIs(sync_plan_sync, plan_module_plan_sync)
        self.assertIs(sync_collections, plan_module_collection_plan)
        self.assertIs(sync_metadata, plan_module_metadata_plan)

    def test_sync_keeps_comparison_imports_compatible(self):
        self.assertIs(comparison_compare_inventory,
                      __import__("calibre_plugin.comparison",
                                 fromlist=["compare_inventory"]).compare_inventory)
        self.assertEqual("new_in_calibre", compare_inventory(
            {1: {"uuid": "u1", "formats": ["EPUB"]}}, {}, [], ["EPUB"])[0]["status"])

    def test_comparison_groups_cover_every_status(self):
        from ..comparison import comparison_group
        self.assertEqual("action", comparison_group("new_in_calibre"))
        self.assertEqual("action", comparison_group("changed"))
        self.assertEqual("review", comparison_group("review"))
        self.assertEqual("review", comparison_group("duplicate_tolino"))
        self.assertEqual("review", comparison_group("not_matchable"))
        self.assertEqual("unchanged", comparison_group("identical"))
        self.assertEqual("tolino_only", comparison_group("only_tolino"))

    def test_the_ignore_column_is_only_used_as_a_yes_no_field(self):
        """Nur ein Ja/Nein-Feld zählt; Text- oder Zahlenfelder nicht."""
        class Bool:
            field_metadata = {TOLINO_IGNORE_COLUMN: {"datatype": "bool"}}

        class Text:
            field_metadata = {TOLINO_IGNORE_COLUMN: {"datatype": "text"}}

        class Missing:
            field_metadata = {}

        self.assertTrue(ignore_column_available(Bool()))
        self.assertFalse(ignore_column_available(Text()))
        self.assertFalse(ignore_column_available(Missing()))

    def test_only_a_clear_yes_excludes_a_book(self):
        """Leer oder unlesbar ist KEIN Ausschluss."""
        cases = ((True, True), (False, False), ("ja", True), ("Ja", True),
                 (1, True), (0, False), ("nein", False), ("", False),
                 (None, False), ("vielleicht", False))
        for value, expected in cases:
            metadata = {TOLINO_IGNORE_COLUMN: value}
            self.assertEqual(expected, metadata_ignored(metadata), value)
        self.assertFalse(metadata_ignored({}))

    def test_an_ignored_book_is_not_offered_for_upload(self):
        """Feldwunsch: gekaufte Bücher nicht erneut in die Cloud laden.

        Beide Wege zählen: die rohe Calibre-Spalte und die fertige
        Kennzeichnung, die der Vergleich vom Dashboard bekommt.
        """
        metadata = self._metadata(ignored=True)
        rows = compare_inventory(metadata, {}, [], ["EPUB"])
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["ignored"], metadata)
        self.assertEqual("ignored", rows[0]["group"], metadata)
        self.assertIn("ignoriert", rows[0]["explanation"], metadata)

    def test_an_ignored_book_still_matches_its_cloud_copy(self):
        """Die gekaufte Cloud-Kopie taucht nicht zusätzlich als Prüffall auf."""
        rows = compare_inventory(
            self._metadata(ignored=True), {},
            [{"id": "t1", "title": "Titel", "authors": "Autor"}], ["EPUB"])
        self.assertEqual(1, len(rows))
        self.assertEqual("t1", rows[0]["tolino_id"])
        self.assertTrue(rows[0]["ignored"])

    def test_an_unset_or_unreadable_column_changes_nothing(self):
        """Ohne deutliches Ja bleibt die Zeile eine normale Upload-Zeile."""
        for value in (False, "", None, "nein"):
            rows = compare_inventory(
                self._metadata(ignored=False), {}, [], ["EPUB"])
            self.assertFalse(rows[0]["ignored"], value)
            self.assertEqual("action", rows[0]["group"], value)

    def test_ignored_books_are_filtered_out_of_the_selection_helpers(self):
        from ..dashboard_plan import syncable_book_ids, syncable_metadata
        metadata = {1: {"ignored": True}, 2: {}, 3: {"ignored": False}}
        self.assertEqual({2, 3}, syncable_book_ids(metadata, [1, 2, 3]))
        self.assertEqual({2, 3}, set(syncable_metadata(metadata)))

    def test_plan_actions_drops_ignored_books_but_keeps_the_others(self):
        """Letzte Sicherung: auch eine erzwungene Auswahl bleibt wirkungslos."""
        from ..dashboard_plan import plan_actions
        state = {"u2": {"tolino_id": "t2"}}

        def planned(ignored):
            metadata = {
                1: {"uuid": "u1", "title": "Gekauft",
                    "authors": "Autorin", "formats": ["EPUB"]},
                2: {"uuid": "u2", "title": "Eigenes",
                    "authors": "Autorin", "formats": ["EPUB"]},
            }
            if ignored:
                metadata[1]["ignored"] = True
            seen = []

            with patch("calibre_plugin.dashboard_plan.upload_jobs",
                       side_effect=lambda _dialog, _metadata, uploads:
                       (seen.extend(uploads) or [], [])):
                plan_actions(
                    object(), {"preferred_formats": ["EPUB"]}, metadata,
                    dict(state),
                    {"uploads": [1, 2], "metadata": [1], "read": [],
                     "unread": [], "cloud_deletions": []})
            return [record[0] for record in seen]

        self.assertEqual([2], planned(True))
        self.assertEqual([1, 2], planned(False))

    def test_an_ignored_book_creates_no_collection(self):
        """Die Serien-Zuordnung folgt den gefilterten Metadaten."""
        from ..dashboard_plan import syncable_metadata
        metadata = {1: {"uuid": "u1", "series": "Reihe", "ignored": True}}
        state = {"u1": {"tolino_id": "t1"}}
        self.assertEqual([("u1", "t1", "Reihe")],
                         plan_collection_updates(
                             series_map_from_metadata(metadata), state))
        self.assertEqual([], plan_collection_updates(
            series_map_from_metadata(syncable_metadata(metadata)), state))

    def test_automatic_collections_skip_ignored_books(self):
        """Ein ignoriertes Buch zählt auch automatisch nicht mit."""
        from ..dashboard_plan import automatic_action_count
        metadata = {1: {"uuid": "u1", "series": "Reihe", "ignored": True},
                    2: {"uuid": "u2", "series": "Reihe"}}
        state = {"u1": {"tolino_id": "t1"}, "u2": {"tolino_id": "t2"}}
        self.assertEqual(1, automatic_action_count(metadata, state))

    @staticmethod
    def _metadata(**values):
        base = {"uuid": "u1", "title": "Titel", "authors": "Autor",
                "isbn": "", "formats": ["EPUB"], "last_modified": "1"}
        base.update(values)
        return {1: base}

    def test_same_title_different_author_is_only_a_review(self):
        rows = compare_inventory(
            self._metadata(authors="Autor A"), {},
            [{"id": "t1", "title": "Titel", "authors": "Autor B"}], ["EPUB"])
        self.assertEqual("review", rows[0]["status"])
        self.assertEqual("title_only", rows[0]["match_reason"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_same_title_different_valid_isbn_is_review(self):
        rows = compare_inventory(
            self._metadata(authors="Autor", isbn="9783161484100"), {},
            [{"id": "t1", "title": "Titel", "authors": "Autor",
              "isbn": "9780306406157"}], ["EPUB"])
        self.assertEqual("review", rows[0]["status"])
        self.assertEqual("conflict", rows[0]["match_reason"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])
        # Die gefundene Cloud-Kopie bleibt technisch sichtbar, wird aber
        # nicht als automatischer Treffer verbucht.
        self.assertEqual("t1", rows[0]["tolino_id"])

    def test_unique_title_and_author_match_identically(self):
        rows = compare_inventory(
            self._metadata(title="Der Fall", authors="Donna Leon"), {},
            [{"id": "t1", "title": "der fall", "authors": "Donna  Leon"}],
            ["EPUB"])
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("title_author", rows[0]["match_reason"])
        self.assertEqual("t1", rows[0]["tolino_id"])

    def test_duplicate_isbn_is_ambiguous(self):
        rows = compare_inventory(
            self._metadata(isbn="9783161484100"), {},
            [{"id": "t1", "title": "A", "isbn": "9783161484100"},
             {"id": "t2", "title": "B", "isbn": "978-3-16-148410-0"}],
            ["EPUB"])
        local = rows[0]
        self.assertEqual("review", local["status"])
        self.assertEqual("ambiguous", local["match_reason"])
        self.assertEqual("", local["tolino_id"])
        self.assertNotEqual("new_in_calibre", local["status"])
        self.assertEqual({"review"}, {row["status"] for row in rows[1:]})

    def test_known_tolino_id_wins_for_the_comparison(self):
        state = {"u1": {"tolino_id": "t-known"}}
        rows = compare_inventory(
            self._metadata(title="Neuer Titel", authors="Neu"), state,
            [{"id": "t-other", "title": "Anderes"},
             {"id": "t-known", "title": "Alter Titel", "authors": "Alt"}],
            ["EPUB"], use_metadata_ids=False)
        self.assertEqual("t-known", rows[0]["tolino_id"])
        self.assertEqual("conflict", rows[0]["match_reason"])
        self.assertEqual("review", rows[0]["status"])
        self.assertEqual("t-known", state["u1"]["tolino_id"])

    def test_matching_always_uses_isbn_or_title_and_author_pair(self):
        metadata = self._metadata(title="Gleich", authors="Autor")
        inventory = [{"id": "t1", "title": "Gleich", "authors": "Autor"}]
        rows = compare_inventory(metadata, {}, inventory, ["EPUB"])
        self.assertEqual("identical", rows[0]["status"])
        self.assertEqual("title_author", rows[0]["match_reason"])

        isbn_metadata = self._metadata(
            title="Anders", authors="Andere", isbn="9783161484100")
        rows = compare_inventory(
            isbn_metadata, {},
            [{"id": "t2", "title": "Titel", "isbn": "9783161484100"}],
            ["EPUB"])
        self.assertEqual("isbn", rows[0]["match_reason"])

    def test_isbn_checksum_is_required_for_isbn_matching(self):
        invalid = compare_inventory(
            self._metadata(isbn="978-1-2"), {},
            [{"id": "t1", "title": "Anders", "authors": "Anders",
              "isbn": "97812"}], ["EPUB"])
        self.assertNotEqual("isbn", invalid[0]["match_reason"])
        from ..comparison import is_valid_isbn
        self.assertTrue(is_valid_isbn("978-3-16-148410-0"))
        self.assertTrue(is_valid_isbn("0-306-40615-2"))
        self.assertFalse(is_valid_isbn("978-1-2"))
        self.assertFalse(is_valid_isbn("0-306-40615-3"))

class ComparisonSortTests(unittest.TestCase):
    """Bestandsvergleich: Upload-Auswahl gruppiert oben, dann
    alphabetisch nach Titel und Autoren."""

    def test_upload_group_first_then_title_then_authors(self):
        from ..comparison import sort_comparison_rows
        rows = [
            {"upload": False, "title": "Alpha", "authors": "A", "book_id": 1},
            {"upload": True, "title": "Zulu", "authors": "Z", "book_id": 2},
            {"upload": False, "title": "beta", "authors": "B", "book_id": 3},
            {"upload": True, "title": "alpha", "authors": "M", "book_id": 4},
        ]
        ordered = sort_comparison_rows(rows)
        self.assertEqual([4, 2, 1, 3],
                         [row["book_id"] for row in ordered])
        # Eingabeliste bleibt unverändert.
        self.assertEqual([1, 2, 3, 4], [row["book_id"] for row in rows])

    def test_missing_fields_do_not_crash(self):
        from ..comparison import sort_comparison_rows
        rows = [{"book_id": None, "tolino_id": "cloud-only"},
                {"upload": True, "title": "Titel", "book_id": 7}]
        ordered = sort_comparison_rows(rows)
        self.assertEqual([7, None], [row["book_id"] for row in ordered])
        self.assertEqual("cloud-only", ordered[1]["tolino_id"])

class ReadStateComparisonTests(unittest.TestCase):
    """Der Bestandsvergleich bekommt die Gelesen-Marken der Cloud und
    belegt damit die Spalte «Gelesen» vor."""

    BOOK = {"uuid": "u1", "title": "Der Fall Collini",
            "authors": "Ferdinand von Schirach", "isbn": "1",
            "formats": ["EPUB"], "last_modified": "1",
            "format_signatures": {"EPUB": "1:1"}}

    def _rows(self, read_ids):
        inventory = [{"deliverableId": "t-1",
                      "epubMetaData": {"title": "Der Fall Collini",
                                       "author": [{"name": "F. v. Schirach"}]}},
                     {"deliverableId": "t-9",
                      "epubMetaData": {"title": "Die Bucht",
                                       "author": [{"name": "Unbekannt"}]}}]
        state = {"u1": {"tolino_id": "t-1"}}
        return compare_inventory({1: self.BOOK}, state, inventory, ["EPUB"],
                                 read_ids=read_ids)

    def test_matching_book_carries_the_cloud_read_state(self):
        rows = self._rows({"t-1"})
        matched = next(row for row in rows if row["book_id"] == 1)
        self.assertTrue(matched["read_in_cloud"])
        self.assertTrue(matched["read"])
        self.assertEqual("t-1", matched["tolino_id"])

    def test_cloud_only_book_carries_the_read_state_too(self):
        rows = self._rows({"t-9"})
        only_cloud = next(row for row in rows if row["tolino_id"] == "t-9")
        self.assertEqual("only_tolino", only_cloud["status"])
        self.assertTrue(only_cloud["read_in_cloud"])
        self.assertTrue(only_cloud["read"])

    def test_without_read_ids_nothing_is_prechecked(self):
        for row in self._rows(()):
            self.assertFalse(row["read_in_cloud"])
            self.assertFalse(row["read"])

class FileChangeIntegrityTests(unittest.TestCase):
    """A: eine geänderte Buchdatei darf nicht als «identisch» gelten."""

    @staticmethod
    def _metadata(**overrides):
        base = {"uuid": "u1", "title": "Gleich", "authors": "Autor",
                "isbn": "", "formats": ["EPUB"], "last_modified": "7",
                "format_signatures": {"EPUB": "100:1000"}}
        base.update(overrides)
        return {1: base}

    @staticmethod
    def _previous_signature(metadata, signature="90:900"):
        previous = dict(metadata[1], format_signatures={"EPUB": signature})
        return content_fingerprint(previous, "EPUB")

    def _row(self, metadata, state):
        return compare_inventory(
            metadata, state,
            [{"deliverableId": "t1", "title": "Gleich", "authors": "Autor"}],
            ["EPUB"])[0]

    def test_changed_file_wins_over_equal_metadata(self):
        metadata = self._metadata()
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": self._previous_signature(metadata),
            "meta_fp": metadata_fingerprint(metadata[1]),
        }}
        row = self._row(metadata, state)
        self.assertEqual("changed", row["status"])
        self.assertTrue(row["file_changed"])
        self.assertEqual("action", row["group"])
        self.assertIn("Buchdatei unterscheidet sich", row["explanation"])
        self.assertNotIn("keine Unterschiede", row["explanation"])

    def test_unchanged_file_with_equal_metadata_is_identical(self):
        metadata = self._metadata()
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": content_fingerprint(metadata[1], "EPUB"),
            "meta_fp": metadata_fingerprint(metadata[1]),
        }}
        row = self._row(metadata, state)
        self.assertEqual("identical", row["status"])
        self.assertFalse(row["file_changed"])
        self.assertIn("keine Unterschiede", row["explanation"])

    def test_pure_metadata_change_does_not_claim_a_new_file(self):
        metadata = self._metadata(title="Neuer Titel")
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": content_fingerprint(metadata[1], "EPUB"),
            "meta_fp": "alter-stand",
        }}
        row = self._row(metadata, state)
        self.assertEqual("changed", row["status"])
        self.assertFalse(row["file_changed"])
        self.assertIn("Nur Metadaten unterscheiden sich", row["explanation"])

    def test_confirmed_metadata_update_is_not_recommended_again(self):
        metadata = self._metadata(title="Neuer Titel")
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": content_fingerprint(metadata[1], "EPUB"),
            "meta_fp": metadata_fingerprint(metadata[1]),
            "meta_verified_fp": metadata_fingerprint(metadata[1]),
        }}
        # Das Inventar liefert noch den alten Titel; ein Read-back nach PUT
        # hat den neuen Stand jedoch bestaetigt und unterdrueckt die Folgeaktion.
        row = self._row(metadata, state)
        self.assertEqual("identical", row["status"])
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            self.assertEqual(
                (False, False),
                ui_module.dialogs.InventoryDialog.default_actions(row))
        finally:
            ui_module._restore_stubs_for_tests()

    def test_remote_title_difference_overrides_unverified_fingerprint(self):
        metadata = self._metadata(title="Neuer Titel")
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": content_fingerprint(metadata[1], "EPUB"),
            "meta_fp": metadata_fingerprint(metadata[1]),
        }}
        rows = compare_inventory(
            metadata, state,
            [{"deliverableId": "t1", "title": "Alter Titel",
              "authors": "Autor"}], ["EPUB"])
        self.assertEqual("changed", rows[0]["status"])
        self.assertIn("Nur Metadaten unterscheiden sich", rows[0]["explanation"])

    def test_missing_previous_file_signature_is_not_a_change_claim(self):
        """Ohne alten Datei-Fingerabdruck gibt es keinen Vergleich -- und
        damit auch keine Behauptung, die Datei habe sich geändert."""
        metadata = self._metadata()
        state = {"u1": {"tolino_id": "t1",
                         "meta_fp": metadata_fingerprint(metadata[1])}}
        row = self._row(metadata, state)
        self.assertEqual("identical", row["status"])
        self.assertFalse(row["file_changed"])
        self.assertIn("keine Unterschiede", row["explanation"])

    def test_default_action_is_upload_for_a_changed_file(self):
        """Die geänderte Datei empfiehlt Hochladen -- auch bei gleichen Feldern."""
        metadata = self._metadata()
        state = {"u1": {
            "tolino_id": "t1",
            "content_fp": self._previous_signature(metadata),
            "meta_fp": metadata_fingerprint(metadata[1]),
        }}
        row = self._row(metadata, state)
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None)
        try:
            self.assertEqual(
                (True, False),
                ui_module.dialogs.InventoryDialog.default_actions(row))
        finally:
            ui_module._restore_stubs_for_tests()

class MissingPreferredFormatStateTests(unittest.TestCase):
    """B: Bücher ohne bevorzugtes Format behalten ihren Sync-Zustand."""

    OLD = {"tolino_id": "t1", "fingerprint": "fp-alt",
           "content_fp": "content-alt", "meta_fp": "meta-alt",
           "calibre_id": 1, "title": "Nur PDF",
           "collections": ["Reihe A", "Reihe B"]}

    @staticmethod
    def _pdf_book(**overrides):
        book = {"uuid": "u1", "title": "Nur PDF", "authors": "Autor",
                "formats": ["PDF"], "last_modified": "3",
                "format_signatures": {"PDF": "10:100"}}
        book.update(overrides)
        return book

    def test_pdf_only_book_keeps_ids_fingerprints_and_collections(self):
        metadata = {1: self._pdf_book()}
        state = {"u1": dict(self.OLD)}
        uploads = plan_sync(metadata, state, ["EPUB"], upload_book_ids=set())
        self.assertEqual([], uploads)
        entry = state["u1"]
        self.assertEqual("t1", entry["tolino_id"])
        self.assertEqual("content-alt", entry["content_fp"])
        self.assertEqual("meta-alt", entry["meta_fp"])
        self.assertEqual(["Reihe A", "Reihe B"], entry["collections"])
        self.assertEqual(1, entry["calibre_id"])

    def test_missing_format_does_not_touch_other_books(self):
        metadata = {1: self._pdf_book(),
                    2: {"uuid": "u2", "title": "Epub", "authors": "Autor",
                        "formats": ["EPUB"], "last_modified": "4",
                        "format_signatures": {"EPUB": "20:200"}}}
        state = {"u1": dict(self.OLD),
                 "u2": {"tolino_id": "t2", "fingerprint": "fp-u2"}}
        uploads = plan_sync(metadata, state, ["EPUB"], upload_book_ids={2})
        current = state
        self.assertEqual([2], [item.book_id for item in uploads])
        self.assertEqual("t2", current["u2"]["tolino_id"])
        self.assertEqual("t2", current["u2"]["tolino_id"])
        self.assertEqual("t1", current["u1"]["tolino_id"])

    def test_epub_added_later_reuses_the_saved_tolino_id(self):
        state = {"u1": dict(self.OLD)}
        later = {1: self._pdf_book(formats=["PDF", "EPUB"],
                                   format_signatures={"PDF": "10:100",
                                                      "EPUB": "30:300"})}
        uploads = plan_sync(later, state, ["EPUB"], upload_book_ids={1})
        current = state
        self.assertEqual(1, len(uploads))
        record = uploads[0]
        self.assertEqual("t1", record.old_id)
        self.assertEqual("t1", current["u1"]["tolino_id"])
        # Auch der Vergleich findet das Buch über die gemerkte ID wieder,
        # statt eine zweite Cloud-Kopie anzunehmen.
        rows = compare_inventory(
            later, current,
            [{"id": "t1", "title": "Nur PDF", "authors": "Autor"}], ["EPUB"])
        self.assertEqual("t1", rows[0]["tolino_id"])
        self.assertNotEqual("new_in_calibre", rows[0]["status"])

    def test_absent_calibre_book_is_not_automatically_deleted(self):
        state = {"u1": dict(self.OLD)}
        plan_sync(
            {2: {"uuid": "u2", "title": "Anderes", "formats": ["EPUB"]}},
            state, ["EPUB"], upload_book_ids=set())
        self.assertIn("u1", state)

class PrepareStateCopyTests(unittest.TestCase):
    """Die Vorbereitung veraendert den gespeicherten Zustand nicht."""

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]
    UUID = "8f3c5f0e-1a2b-4c3d-9e8f-001122334455"

    def test_load_state_accepts_mappings_and_rejects_invalid_values(self):
        state = {"book": {"tolino_id": "copy"}}
        self.assertIs(state, load_state(state))
        for raw in (None, "", "not-json", "[]", "null", "1", [], 7):
            with self.subTest(raw=raw):
                self.assertEqual({}, load_state(raw))

    def test_the_comparison_does_not_mutate_the_stored_state(self):
        ui_module = ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None, qt_stub={"pyqtSignal": self.Signal})
        stored = {self.UUID: {"calibre_id": 7}}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot"
                self.hardware = "hw"

            def login(self):
                return self.refresh

            def inventory(self):
                return parsed_inventory([{
                    "deliverableId": "t-1", "title": "Der Schwarm",
                    "authors": "Frank Schatzing"}])

            def read_ids(self):
                return set()

        metadata = {7: {"uuid": self.UUID, "title": "Der Schwarm",
                        "authors": "Frank Schatzing", "formats": ["EPUB"],
                        "last_modified": "1", "format_signatures": {}}}
        payloads = []
        settings = {"account_name": "A", "partner_id": 4,
                    "hardware_id": "hw-A", "refresh_token": "tok-A", "state": stored,
                    "preferred_formats": ["EPUB"],
                    "sync_column_enabled": False}
        try:
            with patch("calibre_plugin.token_vault.TolinoClient", FakeClient), \
             patch("calibre_plugin.config.account_by_name",
                   return_value={}):
                worker = ui_module.workers.PrepareWorker(settings, metadata)
                worker.ready.connect(payloads.append)
                worker.failed.connect(payloads.append)
                worker.run()
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual(1, len(payloads))
        self.assertIn("state", payloads[0])
        self.assertEqual("t-1", payloads[0]["state"][self.UUID]["tolino_id"])
        # Der gespeicherte Zustand bleibt unberuehrt: die Tolino-ID steht erst
        # nach dem bestaetigten Lauf in den Einstellungen.
        self.assertNotIn("tolino_id", stored[self.UUID])

class StateKeyMigrationTests(unittest.TestCase):
    """«calibre-<Nummer>» und die echte UUID sind derselbe Zustand.

    Liefert Calibre bei einem Lauf keine Kennung, liegt der Zustand unter
    der Ersatzkennung. Kommt später die echte UUID, fand der Vergleich
    nichts mehr: die Tolino-ID galt als verloren (doppelter Upload) und
    der alte Schlüssel als «Buch verschwunden» (Löschkandidat).
    """

    UUID = "8f3c5f0e-1a2b-4c3d-9e8f-001122334455"

    def _metadata(self, uuid=None):
        return {7: {"uuid": uuid if uuid is not None else self.UUID,
                    "title": "Der Schwarm", "authors": "Frank Schatzing",
                    "formats": ["EPUB"], "last_modified": "1",
                    "format_signatures": {}}}

    def _state(self):
        return {"calibre-7": {"calibre_id": 7, "tolino_id": "t1",
                              "fingerprint": "1:1",
                              "collections": [["Serie", "S1"]]}}

    def test_fallback_key_is_not_migrated_by_the_sync_planner(self):
        state = self._state()
        uploads = plan_sync(
            self._metadata(), state, ["EPUB"], upload_book_ids={7})
        self.assertIsNone(uploads[0][3])
        self.assertIn("calibre-7", state)

    def test_a_book_present_in_calibre_is_not_deleted_implicitly(self):
        self.assertEqual([], plan_sync(self._metadata(), self._state(),
                                       ["EPUB"], upload_book_ids=set()))

    def test_a_book_gone_from_calibre_is_not_automatically_removed(self):
        gone = "11111111-2222-3333-4444-555555555555"
        state = {gone: {"calibre_id": 9, "tolino_id": "t9"}}
        uploads = plan_sync(self._metadata(), state, ["EPUB"],
                            upload_book_ids=set())
        self.assertEqual([], uploads)

class FingerprintCommitTests(unittest.TestCase):
    """Ein Fingerabdruck gilt erst nach der Cloud-Aktion.

    `plan_sync` schreibt den Zielstand fuer jedes Buch. Ohne die Trennung
    von Plan und bestaetigtem Stand liess ein gescheiterter Upload oder ein
    gescheitertes /meta-Update das Buch beim naechsten Vergleich als
    «aktuell» aussehen -- der Schritt wurde nie wieder angeboten.
    """

    Signal = worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]

    def _ui(self):
        return ui_module_for(self, qt_stub={"pyqtSignal": self.Signal})

    @staticmethod
    def _settings(state=None):
        return {"preferred_formats": ("EPUB",),
                "account_name": "A", "partner_id": 4, "hardware_id": "hw-A",
                "refresh_token": "tok-A",
                "state": state or {}}

    @staticmethod
    def _metadata(uuid, title, signature, book_id=1):
        return {book_id: {"uuid": uuid, "title": title, "formats": ["EPUB"],
                          "format_signatures": {"EPUB": signature},
                          "last_modified": signature}}

    @staticmethod
    def _selection(uploads=(), metadata=()):
        return {"uploads": list(uploads), "metadata": list(metadata),
                "read": [], "unread": [], "cloud_deletions": [],
                }

    @staticmethod
    def _dashboard(ui_module):
        dashboard = ui_module.dashboard.SyncDashboard.__new__(
            ui_module.dashboard.SyncDashboard)
        api = type("DatabaseAPI", (), {
            "has_id": staticmethod(lambda _book_id: True),
            "format_abspath": staticmethod(lambda _book_id, _fmt: __file__),
            "cover": staticmethod(lambda _book_id, as_file=False: None),
        })()
        dashboard.gui = type("Gui", (), {
            "current_db": type("Database", (), {"new_api": api})(),
        })()
        dashboard.temp_files = []
        return dashboard

    def _synced_state(self, metadata):
        """Simulate state persisted after successful uploads."""
        state = {}
        for book_id, item in metadata.items():
            key = item["uuid"]
            state[key] = {
                "calibre_id": book_id,
                "tolino_id": "t%d" % book_id,
                "content_fp": content_fingerprint(item, "EPUB"),
                "meta_fp": metadata_fingerprint(item),
            }
        return state

    def _prepare(self, ui_module, metadata, previous, selection):
        settings = self._settings(previous)
        preparation = ui_module.dashboard._dashboard_plan.plan_actions(
            self._dashboard(ui_module), settings, metadata, previous, selection)
        return settings, preparation

    @staticmethod
    def _client(upload=None, update_metadata=None):
        class FakeClient:
            def __init__(self, *args, **kwargs):
                self.refresh = "rot-A"
                self.access = "access"

            def login(self):
                return self.refresh

            def upload(self, path):
                return upload(path) if upload else "new-1"

            def update_metadata(self, tolino_id, fields):
                if update_metadata is not None:
                    return update_metadata(tolino_id, fields)
                return True

            def delete(self, tolino_id):
                return True

        return FakeClient()

    @staticmethod
    def _run_worker(ui_module, settings, preparation, client, holder=None):
        emitted = {}
        settings["metadata"] = preparation.get("metadata", {})
        with patch("calibre_plugin.token_vault.TolinoClient",
                   lambda *args, **kwargs: client), \
             patch("calibre_plugin.ui_workers.save_account",
                   lambda *args, **kwargs: None), \
             patch("calibre_plugin.config.save_credentials",
                   lambda *args, **kwargs: None):
            worker = ui_module.workers.SyncWorker(
                settings, preparation["jobs"], preparation["removals"],
                preparation["meta_updates"], preparation["series_map"],
                preparation["selected_read"], preparation["selected_unread"],
                titles=preparation.get("titles"))
            if holder is not None:
                holder["worker"] = worker
            worker.completed.connect(
                lambda *args: emitted.update(completed=args))
            worker.failed.connect(lambda *args: emitted.update(failed=args))
            worker.run()
        return worker, emitted

    def test_the_plan_holds_back_the_new_fingerprint(self):
        ui_module = self._ui()
        previous = self._synced_state(
            self._metadata("u1", "Alt", "100:1000"))
        changed = self._metadata("u1", "Neu", "200:2000")
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection([1]))
        self.assertEqual(1, len(preparation["jobs"]))
        entry = settings["state"]["u1"]
        self.assertEqual(previous["u1"]["meta_fp"], entry["meta_fp"])
        self.assertEqual(previous["u1"]["content_fp"], entry["content_fp"])
        self.assertNotIn("fingerprint", entry)

    def test_a_successful_upload_confirms_the_planned_fingerprint(self):
        ui_module = self._ui()
        previous = self._synced_state(
            self._metadata("u1", "Alt", "100:1000"))
        changed = self._metadata("u1", "Neu", "200:2000")
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection([1]))
        worker, emitted = self._run_worker(
            ui_module, settings, preparation, self._client())
        self.assertIsNone(emitted.get("failed"))
        entry = worker.state["u1"]
        self.assertEqual("new-1", entry["tolino_id"])
        self.assertNotIn("meta_verified_fp", entry)
        self.assertEqual(content_fingerprint(changed[1], "EPUB"),
                         entry["content_fp"])
        self.assertNotIn("fingerprint", entry)

    def test_a_failed_upload_leaves_the_old_state_and_replans(self):
        ui_module = self._ui()
        previous = self._synced_state(
            self._metadata("u1", "Alt", "100:1000"))
        changed = self._metadata("u1", "Neu", "200:2000")
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection([1]))

        def failing(path):
            raise RuntimeError("Netz weg")

        worker, emitted = self._run_worker(
            ui_module, settings, preparation, self._client(upload=failing))
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        # Der Fehlschlag ist gezaehlt, aber nicht als Erfolg gespeichert.
        self.assertEqual(1, worker.outcome["failed"])
        entry = worker.state["u1"]
        self.assertEqual("t1", entry["tolino_id"])
        self.assertEqual(previous["u1"]["meta_fp"], entry["meta_fp"])
        self.assertEqual(previous["u1"]["content_fp"], entry["content_fp"])
        self.assertNotIn("fingerprint", entry)
        # Der naechste Vergleich bietet denselben Upload wieder an.
        uploads = plan_sync(changed, worker.state, ["EPUB"], upload_book_ids={1})
        self.assertEqual([(1, "u1", "EPUB", "t1")], uploads)

    def test_successful_metadata_update_writes_confirmed_id_to_column(self):
        ui_module = self._ui()
        previous = self._synced_state(
            self._metadata("u1", "Alt", "100:1000"))
        changed = self._metadata("u1", "Neu", "100:1000")
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection(metadata=[1]))
        worker, emitted = self._run_worker(
            ui_module, settings, preparation, self._client())
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        self.assertEqual([(1, "t1")], worker.updates)
        self.assertEqual(metadata_fingerprint(changed[1]),
                         worker.state["u1"]["meta_verified_fp"])

    def test_a_failed_metadata_update_keeps_the_old_meta_fingerprint(self):
        ui_module = self._ui()
        previous = self._synced_state(
            self._metadata("u1", "Alt", "100:1000"))
        # Nur der Titel aendert sich: die Datei bleibt dieselbe.
        changed = self._metadata("u1", "Neu", "100:1000")
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection(metadata=[1]))
        self.assertEqual([], preparation["jobs"])
        self.assertEqual(1, len(preparation["meta_updates"]))
        self.assertEqual(previous["u1"]["meta_fp"],
                         settings["state"]["u1"]["meta_fp"])

        def failing(tolino_id, fields):
            raise RuntimeError("/meta kaputt")

        worker, emitted = self._run_worker(
            ui_module, settings, preparation,
            self._client(update_metadata=failing))
        self.assertIsNone(emitted.get("failed"))
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(previous["u1"]["meta_fp"],
                         worker.state["u1"]["meta_fp"])
        self.assertNotIn("meta_verified_fp", worker.state["u1"])
        self.assertEqual([], worker.updates)
        # Und der naechste Lauf plant das Update erneut.
        updates = plan_metadata_updates(
            changed, worker.state, [], {1})
        self.assertEqual(1, len(updates))

    def test_only_the_successful_upload_of_a_batch_is_confirmed(self):
        ui_module = self._ui()
        metadata = dict(self._metadata("u1", "A", "100:1000", book_id=1))
        metadata.update(self._metadata("u2", "B", "300:3000", book_id=2))
        previous = self._synced_state(metadata)
        changed = dict(self._metadata("u1", "A2", "200:2000", book_id=1))
        changed.update(self._metadata("u2", "B2", "400:4000", book_id=2))
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection([1, 2]))

        attempts = []

        def failing(_path):
            attempts.append(True)
            if len(attempts) == 2:
                raise RuntimeError("Netz weg")
            return "new-1"

        worker, emitted = self._run_worker(
            ui_module, settings, preparation, self._client(upload=failing))
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["failed"])
        self.assertEqual(content_fingerprint(changed[1], "EPUB"),
                         worker.state["u1"]["content_fp"])
        self.assertEqual(previous["u2"]["content_fp"],
                         worker.state["u2"]["content_fp"])
        self.assertEqual("t2", worker.state["u2"]["tolino_id"])

    def test_a_cancel_before_the_second_upload_confirms_nothing_for_it(self):
        ui_module = self._ui()
        metadata = dict(self._metadata("u1", "A", "100:1000", book_id=1))
        metadata.update(self._metadata("u2", "B", "300:3000", book_id=2))
        previous = self._synced_state(metadata)
        changed = dict(self._metadata("u1", "A2", "200:2000", book_id=1))
        changed.update(self._metadata("u2", "B2", "400:4000", book_id=2))
        settings, preparation = self._prepare(
            ui_module, changed, previous, self._selection([1, 2]))
        holder = {}

        def first_upload(path):
            holder["worker"].cancel()
            return "new-1"

        worker, emitted = self._run_worker(
            ui_module, settings, preparation,
            self._client(upload=first_upload), holder=holder)
        self.assertIsNone(emitted.get("failed"))
        self.assertIsNotNone(emitted.get("completed"))
        self.assertTrue(worker.outcome["cancelled"])
        self.assertEqual(1, worker.outcome["uploaded"])
        self.assertEqual(1, worker.outcome["not_started"])
        self.assertEqual("new-1", worker.state["u1"]["tolino_id"])
        self.assertEqual(content_fingerprint(changed[1], "EPUB"),
                         worker.state["u1"]["content_fp"])
        # Das zweite Buch wurde nie begonnen: alter Stand bleibt.
        self.assertEqual("t2", worker.state["u2"]["tolino_id"])
        self.assertEqual(previous["u2"]["content_fp"],
                         worker.state["u2"]["content_fp"])

class VersionDocumentationTests(unittest.TestCase):
    """README, CHANGELOG und PLUGIN_VERSION bleiben synchron."""

    @staticmethod
    def _plugin_version():
        """PLUGIN_VERSION aus dem Wurzel-__init__.py (Plugin-Metadaten)."""
        base = Path(__file__).resolve().parent.parent.parent
        tree = ast.parse((base / "__init__.py").read_text(encoding="utf-8"))
        for node in tree.body:
            if (isinstance(node, ast.Assign)
                    and any(getattr(target, "id", "") == "PLUGIN_VERSION"
                            for target in node.targets)):
                return ast.literal_eval(node.value)
        raise AssertionError("PLUGIN_VERSION fehlt in __init__.py")

    def test_the_readme_badge_names_the_plugin_version(self):
        version = self._plugin_version()
        base = Path(__file__).resolve().parent.parent.parent
        readme = (base / "README.md").read_text(encoding="utf-8")
        version_text = "%d.%d.%02d" % version
        expected = "Version-%s-blue" % version_text
        self.assertIn(expected, readme)

    def test_the_top_changelog_entry_names_the_plugin_version(self):
        """Der oberste CHANGELOG-Eintrag ist die aktuelle Version."""
        version = "%d.%d.%02d" % self._plugin_version()
        base = Path(__file__).resolve().parent.parent.parent
        changelog = (base / "CHANGELOG.md").read_text(encoding="utf-8")
        match = re.search(r"^## (\d+\.\d+\.\d+)\s*$", changelog,
                          re.MULTILINE)
        self.assertIsNotNone(match, "CHANGELOG ohne Versions-Ueberschrift")
        self.assertEqual(version, match.group(1))
        section = changelog.split("## " + version, 1)[1].split("\n## ", 1)[0]
        bullets = [line for line in section.splitlines()
                   if line.startswith("- ")]
        self.assertGreaterEqual(len(bullets), 3)
        self.assertLessEqual(len(bullets), 6)
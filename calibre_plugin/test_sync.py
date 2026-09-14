import unittest
import ast
import os
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from . import config
from .sync import (compare_inventory, cover_bytes, fingerprint, iter_book_ids,
                   normalize_formats, plan_sync, safe_format_path,
                   selected_book_ids, selected_table_rows, sync_summary,
                   unpack_plan_result, unpack_upload_record, redact_sensitive,
                   format_diagnostic_report, diagnose_preparation,
                   _diagnostic_formats, metadata_by_id, format_error_details)
from .tolino import (PARTNERS, TolinoAuthError, callback_redirect_uri,
                     TolinoClient, hardware_id, normalize_refresh_token,
                     sanitize_error, validate_callback)


class SyncPlanTests(unittest.TestCase):
    def setUp(self):
        self.book = {"uuid": "u1", "title": "A", "formats": ["EPUB"], "last_modified": "1"}

    def test_new_book_is_uploaded(self):
        uploads, removals, state = plan_sync({1: self.book}, {}, ["EPUB"])
        self.assertEqual([(1, "u1", "EPUB", None)], uploads)
        self.assertFalse(removals)
        self.assertIn("u1", state)

    def test_iter_book_ids_uses_rows_and_deduplicates(self):
        class Row:
            def __init__(self, book_id):
                self.book_id = book_id

        class Data:
            def iterall(self):
                return iter((Row(4), Row(2), Row(4), Row(9)))

        class Database:
            data = Data()

        self.assertEqual([4, 2, 9], list(iter_book_ids(Database())))

    def test_iter_book_ids_falls_back_to_library_database_all_ids(self):
        class Database:
            data = object()

            def all_ids(self):
                return (8, 3, 8)

        self.assertEqual([8, 3], list(iter_book_ids(Database())))

    def test_iter_book_ids_uses_gui_all_book_ids_before_legacy_all_ids(self):
        class Database:
            data = object()

            def all_book_ids(self):
                return (18, 5, 18)

            def all_ids(self):
                raise AssertionError("must prefer all_book_ids")

        self.assertEqual([18, 5], list(iter_book_ids(Database())))

    def test_iter_book_ids_uses_data_id_fallback_before_all_ids(self):
        class Data:
            def iterallids(self):
                return iter((7, 1, 7))

        class Database:
            data = Data()

            def all_ids(self):
                raise AssertionError("must prefer data.iterallids")

        self.assertEqual([7, 1], list(iter_book_ids(Database())))

    def test_iter_book_ids_ignores_rows_without_existing_book_id(self):
        class Data:
            def iterall(self):
                return iter((object(), {"title": "not a book id"}))

        class Database:
            data = Data()

            def all_ids(self):
                raise AssertionError("must not use fallback after iterall")

        self.assertEqual([], list(iter_book_ids(Database())))

    def test_iter_book_ids_accepts_calibre_tuple_rows_and_skips_short_rows(self):
        class Data:
            def iterall(self):
                return iter(((12, "title"), (), (13,), ("not-an-id", "title")))

        class Database:
            data = Data()

        self.assertEqual([12, 13], list(iter_book_ids(Database())))

    def test_metadata_by_id_uses_real_id_not_filtered_view_index(self):
        class Data:
            def iterall(self):
                return iter(((41, "first"), (99, "second")))

        class Database:
            data = Data()

            def has_id(self, book_id):
                return book_id in {41, 99}

            def get_metadata(self, book_id, index_is_id=False):
                if not index_is_id:
                    raise AssertionError("filtered-view index was passed")
                return {"uuid": "u%d" % book_id}

        database = Database()
        ids = list(iter_book_ids(database))
        self.assertEqual([41, 99], ids)
        self.assertEqual({"uuid": "u41"}, metadata_by_id(database, ids[0]))

    def test_metadata_by_id_rejects_missing_real_id(self):
        class Database:
            def has_id(self, book_id):
                return False

            def get_metadata(self, book_id, index_is_id=False):
                raise AssertionError("must validate before reading")

        with self.assertRaisesRegex(ValueError, "no book with id 7"):
            metadata_by_id(Database(), 7)

    def test_normalize_formats_handles_all_calibre_shapes(self):
        self.assertEqual(["EPUB", "PDF"], normalize_formats(("EPUB", "PDF")))
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
            plan_sync({1: metadata}, {}, (".EPUB",))[0],
        )

    def test_selected_book_without_preferred_format_has_contextual_warning(self):
        with self.assertRaisesRegex(ValueError, r"Buch 1 \(A\).*kein bevorzugtes Format"):
            plan_sync(
                {1: {"uuid": "u1", "title": "A", "formats": ("MOBI",)}},
                {}, ("EPUB",), upload_book_ids={1},
            )

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

    def test_plan_result_helpers_reject_short_records_with_context(self):
        with self.assertRaisesRegex(ValueError, "invalid sync plan"):
            unpack_plan_result(([], {}))
        with self.assertRaisesRegex(ValueError, "invalid upload record"):
            unpack_upload_record((1, "uuid"))
        self.assertEqual(
            {"book_id": 1, "book_uuid": "u1", "format_name": "EPUB", "old_id": None},
            unpack_upload_record((1, "u1", "EPUB", None)),
        )

    def test_complete_preparation_path_handles_empty_single_and_short_data(self):
        cases = (
            ({}, [], []),
            ({1: dict(self.book)}, [], [(1, "u1", "EPUB", None)]),
            ({2: {"uuid": "u2", "formats": ("EPUB",)}}, [], [(2, "u2", "EPUB", None)]),
        )
        for metadata, inventory, expected_uploads in cases:
            comparison = compare_inventory(metadata, {}, inventory, ("EPUB",))
            selected = selected_book_ids(comparison)
            plan = unpack_plan_result(
                plan_sync(metadata, {}, ("EPUB",), upload_book_ids=selected)
            )
            uploads, removals, current = plan
            self.assertEqual(expected_uploads, uploads)
            self.assertEqual([], removals)
            self.assertEqual(set(metadata), {item["calibre_id"] for item in current.values()})
            for record in uploads:
                self.assertEqual(expected_uploads[0][0] if expected_uploads else None,
                                 unpack_upload_record(record)["book_id"])

    def test_safe_format_path_accepts_wrapped_path_and_rejects_empty_result(self):
        class Database:
            def format_abspath(self, book_id, format_name):
                return ("/library/book.epub", format_name)

        self.assertEqual("/library/book.epub", safe_format_path(Database(), 1, "EPUB"))

        class EmptyDatabase:
            def format_abspath(self, book_id, format_name):
                return ()

        with self.assertRaises(ValueError):
            safe_format_path(EmptyDatabase(), 1, "EPUB")

    def test_diagnosis_reports_found_and_missing_format_paths(self):
        with tempfile.NamedTemporaryFile(suffix=".epub") as book_file:
            class FormatsList:
                def __iter__(self):
                    return iter((".EPUB", "pdf"))

            class Database:
                def format_abspath(self, book_id, format_name):
                    if format_name == "EPUB":
                        return book_file.name
                    return ()

            report = _diagnostic_formats(
                Database(), {1: {"formats": FormatsList()}})
            self.assertEqual(["EPUB", "PDF"], report["1"]["metadata_formats"])
            self.assertEqual("found", report["1"]["paths"]["EPUB"]["status"])
            self.assertEqual("missing", report["1"]["paths"]["PDF"]["status"])

    def test_cover_bytes_handles_bytes_paths_and_unexpected_values(self):
        class Database:
            def __init__(self, value):
                self.value = value

            def cover(self, book_id, as_file=False):
                return self.value

        self.assertEqual(b"cover", cover_bytes(Database((b"cover", "jpg")), 1))
        self.assertIsNone(cover_bytes(Database(("missing.jpg",)), 1))
        with tempfile.NamedTemporaryFile(delete=False) as cover_file:
            cover_file.write(b"cover-file")
            path = cover_file.name
        try:
            self.assertEqual(b"cover-file", cover_bytes(Database((path, "jpg")), 1))
        finally:
            os.remove(path)

    def test_inventory_comparison_matches_uuid_and_marks_changed(self):
        metadata = {
            1: {"uuid": "u1", "title": "A", "authors": "Author",
                "isbn": "123", "formats": ["EPUB"], "last_modified": "2"},
            2: {"uuid": "u2", "title": "B", "authors": "Writer",
                "isbn": "456", "formats": ["EPUB"], "last_modified": "1"},
        }
        state = {
            "u1": {"tolino_id": "t1", "fingerprint": fingerprint(metadata[1], "EPUB")},
            "u2": {"tolino_id": "t2", "fingerprint": "old"},
        }
        rows = compare_inventory(
            metadata, state,
            [{"deliverableId": "t2", "uuid": "u2", "title": "B", "authors": "Writer"},
             {"deliverableId": "t1", "uuid": "u1", "title": "A", "authors": "Author"}],
            ["EPUB"],
        )
        self.assertEqual(["identical", "changed"], [row["status"] for row in rows])
        self.assertEqual({2}, selected_book_ids(rows))

    def test_inventory_comparison_uses_isbn_and_reports_remote_only(self):
        metadata = {
            1: {"uuid": "u1", "title": "Local", "authors": "Author",
                "isbn": "978-1-2", "formats": ["EPUB"]},
        }
        rows = compare_inventory(
            metadata, {},
            [{"id": "t1", "title": "Remote title", "authors": "Other",
              "isbn": "97812"}, {"id": "t2", "title": "Cloud only"}],
            ["EPUB"],
        )
        self.assertEqual("changed", rows[0]["status"])
        self.assertEqual("t1", rows[0]["tolino_id"])
        self.assertEqual("only_tolino", rows[1]["status"])
        self.assertFalse(rows[1]["selected"])

    def test_unchanged_book_is_not_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual([], plan_sync({1: self.book}, old, ["EPUB"])[0])

    def test_selected_unchanged_book_can_be_uploaded_again(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": fingerprint(self.book, "EPUB")}}
        self.assertEqual(
            [(1, "u1", "EPUB", "t1")],
            plan_sync({1: self.book}, old, ["EPUB"], upload_book_ids={1})[0],
        )

    def test_changed_book_is_uploaded(self):
        old = {"u1": {"tolino_id": "t1", "fingerprint": "old"}}
        self.assertEqual("t1", plan_sync({1: self.book}, old, ["EPUB"])[0][0][3])

    def test_deletions_are_opt_in(self):
        state = {"gone": {"tolino_id": "t2", "fingerprint": "x"}}
        self.assertEqual([], plan_sync({}, state, ["EPUB"])[1])
        self.assertEqual([("gone", "t2")], plan_sync({}, state, ["EPUB"], True)[1])

    def test_reference_partners_and_stable_hardware_shape(self):
        for partner_id in (3, 4, 6, 8, 13, 23, 30):
            self.assertIn(partner_id, PARTNERS)
        self.assertRegex(hardware_id(), r"^[123x]..A-00BCD-EFGHI-JKLMN-OPQRh$")

    def test_partner_8_refresh_configuration_matches_reference(self):
        self.assertEqual("webreader", PARTNERS[8]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[8]["scope"])
        self.assertEqual("https://www.orellfuessli.ch/auth/oauth2/token",
                         PARTNERS[8]["token_url"])

    def test_refresh_token_normalization_reports_only_safe_metadata(self):
        token, info = normalize_refresh_token('  "refresh-secret-value"  ')
        self.assertEqual("refresh-secret-value", token)
        self.assertEqual(20, info["token_length"])
        self.assertEqual("refr...", info["token_prefix"])
        self.assertTrue(info["outer_quotes_removed"])
        self.assertTrue(info["surrounding_whitespace_removed"])

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
            return Response()

        with patch("calibre_plugin.tolino.urlopen", request):
            client = TolinoClient(8, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                  " refresh-token ")
            client.login()
        self.assertEqual(PARTNERS[8]["token_url"], captured["url"])
        self.assertEqual(
            "client_id=webreader&grant_type=refresh_token&"
            "refresh_token=refresh-token&scope=SCOPE_BOSH",
            captured["body"])
        self.assertEqual("application/x-www-form-urlencoded",
                         captured["content_type"])
        self.assertEqual("refresh-token", client.refresh)
        self.assertNotEqual("access-secret", client.refresh)

    def test_auth_diagnostics_redacts_token_content(self):
        client = TolinoClient(8, "", '"secret-refresh"')
        diagnostics = client.auth_diagnostics()
        rendered = format_diagnostic_report([{
            "step": "auth", "status": "error", "value": diagnostics,
        }])
        self.assertIn("partner_id", rendered)
        self.assertIn("Books.ch / orellfuessli.ch", rendered)
        self.assertIn("token_length", rendered)
        self.assertIn("token_prefix", rendered)
        self.assertNotIn("secret-refresh", rendered)
        self.assertNotIn("authorization", rendered.casefold())

    def test_callback_requires_matching_state_and_fresh_timestamp(self):
        query = {"state": ["expected"], "code": ["opaque-code"]}
        self.assertEqual("opaque-code", validate_callback(query, "expected", 100, 120))
        with self.assertRaises(TolinoAuthError):
            validate_callback(query, "wrong", 100, 120)
        with self.assertRaises(TolinoAuthError):
            validate_callback(query, "expected", 100, 401)

    def test_callback_rejects_provider_errors_and_missing_code(self):
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": ["s"], "error": ["denied"]}, "s", 100, 101)
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": ["s"]}, "s", 100, 101)
        with self.assertRaises(TolinoAuthError):
            validate_callback({"state": [], "code": ["c"]}, "s", 100, 101)

    def test_redirect_uri_is_loopback_only(self):
        self.assertEqual("http://127.0.0.1:4321/callback", callback_redirect_uri(4321))

    def test_sync_summary_is_deterministic_for_dashboard(self):
        self.assertEqual({"uploads": 2, "deletions": 1, "errors": 0, "total": 3},
                         sync_summary(2, 1))

    def test_diagnostic_redaction_removes_credentials_and_headers(self):
        value = redact_sensitive({
            "refresh_token": "refresh-secret",
            "password": "password-secret",
            "headers": "Authorization: Bearer access-secret",
            "nested": {"access_token": "access-secret"},
        })
        rendered = format_diagnostic_report([{"step": "response", "status": "ok",
                                              "value": value}])
        self.assertNotIn("refresh-secret", rendered)
        self.assertNotIn("password-secret", rendered)
        self.assertNotIn("access-secret", rendered)
        self.assertIn("[REDACTED]", rendered)

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
        client = TolinoClient(8, "", token)
        with patch("calibre_plugin.tolino.urlopen", side_effect=error):
            with self.assertRaisesRegex(TolinoAuthError, "authentication failed"):
                client.login()
        self.assertIn("invalid_grant", client.last_error_text)
        self.assertIn("error_description", client.last_error_text)
        self.assertNotIn(token, client.last_error_text)

    def test_diagnosis_reports_steps_and_continues_after_metadata_error(self):
        class Row:
            def __init__(self, book_id):
                self.book_id = book_id

        class Data:
            def iterall(self):
                return (Row(1), Row(2))

        class Database:
            data = Data()

            def get_metadata(self, book_id, index_is_id=False):
                if book_id == 2:
                    raise IndexError("tuple index out of range")
                return {"uuid": "u1", "formats": ("EPUB",)}

            def format_abspath(self, book_id, format_name):
                return "/library/book.epub"

            def cover(self, book_id, as_file=False):
                return b"cover"

        results = diagnose_preparation(Database(), ["EPUB"], upload_covers=True)
        report = format_diagnostic_report(results)
        self.assertIn("Calibre-Version", report)
        self.assertIn("Erkannte Buch-ID-Anzahl", report)
        self.assertIn("IndexError", report)
        self.assertIn("tuple index out of range", report)
        self.assertIn("plan_sync-Ergebnisform", report)

    def test_preparation_error_keeps_traceback_and_redacts_without_dashboard_settings(self):
        try:
            raise IndexError("tuple index out of range; refresh-secret")
        except Exception as exc:
            rendered = format_error_details(exc, ("refresh-secret",))
        self.assertIn("IndexError", rendered)
        self.assertIn("tuple index out of range", rendered)
        self.assertIn("Traceback", rendered)
        self.assertNotIn("refresh-secret", rendered)
    def test_empty_settings_are_migrated_without_calibre(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = config.settings()
            self.assertEqual(config.DEFAULTS["partner_id"], values["partner_id"])
            self.assertEqual(config.DEFAULTS["preferred_formats"], values["preferred_formats"])
            self.assertEqual(config.DEFAULTS["state"], values["state"])
            self.assertEqual(set(config.DEFAULTS), set(config.PREFERENCES))
        finally:
            config.PREFERENCES = original

    def test_partial_settings_get_missing_defaults(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.PREFERENCES["refresh_token"] = "configured-token"
            values = config.settings()
            self.assertEqual("configured-token", values["refresh_token"])
            self.assertEqual("", values["hardware_id"])
            self.assertFalse(values["enable_deletions"])
            self.assertIsInstance(values["state"], dict)
        finally:
            config.PREFERENCES = original

    def test_zip_entrypoint_uses_calibre_namespace_package(self):
        archive = Path(__file__).parent.parent / "tolino_cloud_sync.zip"
        if not archive.exists():
            self.skipTest("build_plugin.py has not been run")
        with zipfile.ZipFile(archive) as plugin:
            self.assertEqual({
                "__init__.py",
                "plugin-import-name-tolino_cloud_sync.txt",
                "ui.py",
                "config.py",
                "sync.py",
                "tolino.py",
            }, set(plugin.namelist()))
            marker = next(name for name in plugin.namelist()
                          if name.startswith("plugin-import-name-") and name.endswith(".txt"))
            self.assertEqual("tolino_cloud_sync",
                             marker[len("plugin-import-name-"):-len(".txt")])
            self.assertEqual(b"", plugin.read(marker))
            metadata = ast.parse(plugin.read("__init__.py").decode("utf-8"))
            values = [
                node.value.value
                for node in ast.walk(metadata)
                if isinstance(node, ast.Assign)
                and any(getattr(target, "id", "") == "actual_plugin"
                        for target in node.targets)
                and isinstance(node.value, ast.Constant)
            ]
            self.assertEqual(["calibre_plugins.tolino_cloud_sync.ui:TolinoSyncAction"], values)


if __name__ == "__main__":
    unittest.main()

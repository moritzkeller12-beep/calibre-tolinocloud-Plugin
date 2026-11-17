"""Anmeldung, Token-Rotation und Schreibpfade -- über echte Sockets.

Der Client spricht einen Loopback-HTTP-Server an, statt `urlopen` zu
ersetzen: geprüft wird, was tatsächlich über die Leitung geht (Formular,
Kopfdaten, Rotationsreihenfolge, Multipart-Body, PATCH-Nutzlast). Das sind
die Fehler, die den Nutzer ausloggen, einen Einmal-Token verbrennen oder
falsche Daten in die Cloud schreiben.
"""
import contextlib
import json
import time
import unittest
from urllib.parse import parse_qs, urlsplit

from ..errors import TolinoApiError, TolinoAuthError, TolinoError
from ..sync_data import READ_TAG
from ..tolino import TolinoClient, _download_token, sanitize_error
from ._support import LocalHTTP, local_tolino_endpoints

HARDWARE = "11111111-1111-4111-8111-111111111111"
TOKEN_BODY = json.dumps({"access_token": "acc-1", "refresh_token": "rot-1",
                         "expires_in": 3600}).encode()


def token_server(body=TOKEN_BODY, status=200, extra=None):
    """Server, der den Token-Endpunkt bedient; alles andere sind Fehler."""

    def responder(method, path, headers, payload):
        if path.startswith("/token/"):
            if extra is not None:
                return extra(method, path, headers, payload)
            return status, {"Content-Type": "application/json"}, body
        return 404, {}, b""

    return LocalHTTP(responder)


class LoginTests(unittest.TestCase):
    """Der Refresh-Token ist einmalig: Reihenfolge und Meldung muessen stimmen."""

    def test_login_exchanges_the_token_and_reports_the_rotation(self):
        saved = []
        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_callback=saved.append)
            self.assertEqual("rot-1", client.login())
            request = server.requests[0]
        self.assertEqual(["rot-1"], saved)
        self.assertIn("grant_type=refresh_token", request["body"].decode())
        self.assertIn("refresh_token=start", request["body"].decode())

    def test_a_rejected_token_is_reported_and_cleared_once(self):
        rejected = []
        body = json.dumps({"error": "invalid_grant"}).encode()
        with token_server(body=body, status=400) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_rejected_callback=rejected.append)
            with self.assertRaises(TolinoAuthError) as caught:
                client.login()
        self.assertEqual("A01", caught.exception.code)
        self.assertIn("abgelehnt", str(caught.exception))
        self.assertEqual(["start"], rejected)

    def test_a_network_error_keeps_the_stored_token(self):
        with token_server() as server:
            origin = server.origin
        # Server ist wieder zu: der Port antwortet nicht mehr.
        with local_tolino_endpoints(origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoError) as caught:
                client.login()
        self.assertEqual("N01", caught.exception.code)
        self.assertEqual("start", client.refresh)

    def test_a_second_login_reuses_the_valid_access_token(self):
        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            client.login()
            client.login()
            requests = len(server.requests)
        self.assertEqual(1, requests)

    def test_an_unknown_partner_is_rejected(self):
        with self.assertRaises(TolinoError):
            TolinoClient(99, HARDWARE, refresh="start")


class ErrorTextTests(unittest.TestCase):
    """Fehlertexte nennen den Grund, aber nie Zugangsdaten."""

    def test_tokens_and_tracebacks_are_redacted(self):
        exc = TolinoError("abgelehnt für tok-123", code="A01")
        text = sanitize_error(exc, ("tok-123",))
        self.assertNotIn("tok-123", text)
        self.assertIn("abgelehnt", text)

    def test_a_broken_token_response_is_an_auth_error(self):
        body = json.dumps({"refresh_token": "rot-1"}).encode()
        with token_server(body=body) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoAuthError):
                client.login()


JSON_HEADERS = {"Content-Type": "application/json"}


def json_reply(payload):
    return 200, JSON_HEADERS, json.dumps(payload).encode()


class Backend:
    """Kleine Loopback-Cloud: Upload, Metadaten, sync-data, Löschen, Download."""

    def __init__(self):
        self.metadata = {}
        self.revision = 7
        self.patches = []
        self.patch_bodies = []
        self.upload_reply = {"metadata": {"deliverableId": "neu-1"}}
        self.download_info = {"DownloadInfo": {
            "contentUrl": "https://example.invalid/buch.epub"}}
        self.ignore_put = False
        self.hide_metadata = False
        self.deleted = []
        self.server = LocalHTTP(self.respond)

    def respond(self, method, path, headers, payload):
        if path.startswith("/bosh/rest/upload"):
            return json_reply(self.upload_reply)
        if path.startswith("/bosh/rest/meta/"):
            query = parse_qs(urlsplit(path).query)
            deliverable = query.get("deliverableId", [""])[0]
            if method == "PUT":
                if not self.ignore_put:
                    sent = json.loads(payload or b"{}")
                    self.metadata.setdefault(deliverable, {}).update(
                        sent.get("uploadMetaData") or {})
                return json_reply({})
            if self.hide_metadata:
                return json_reply({})
            return json_reply({
                "metadata": dict(self.metadata.get(deliverable, {}))})
        if path.startswith("/bosh/rest/sync-data"):
            body = json.loads(payload or b"{}")
            self.patch_bodies.append(body)
            if body.get("patches"):
                self.revision += 1
            return json_reply({"revision": self.revision,
                               "patches": self.patches})
        if path.startswith("/bosh/rest/deletecontent"):
            self.deleted.append(path)
            return json_reply({})
        if path.startswith("/bosh/rest/cloud/downloadinfo"):
            return json_reply(self.download_info)
        return 404, {}, b""


@contextlib.contextmanager
def write_client(backend):
    """Client gegen die Loopback-Cloud; Zugang gesetzt, kein Token-Tausch."""
    with backend.server as server, local_tolino_endpoints(server.origin):
        client = TolinoClient(1, HARDWARE, refresh="start")
        client.access, client.expires_at = "acc-1", time.time() + 3600
        yield client


def sent_body(backend, index):
    return json.loads(backend.server.requests[index]["body"] or b"{}")


def read_tag_path(book_id, tag_id=None):
    path = "/publications/%s/tags" % book_id
    return "%s/%s" % (path, tag_id) if tag_id else path


class UploadTests(unittest.TestCase):
    """Ein Upload muss als vollständiger Multipart-Body ankommen."""

    def test_the_file_is_sent_as_multipart_and_its_new_id_returned(self):
        backend = Backend()
        with write_client(backend) as client, self.temporary_epub() as path:
            self.assertEqual("neu-1", client.upload(path))
        request = backend.server.requests[0]
        self.assertEqual("POST", request["method"])
        self.assertTrue(request["path"].endswith("/upload"))
        self.assertIn("multipart/form-data; boundary=",
                      request["headers"]["content-type"])
        self.assertIn(b'filename="Buch.epub"', request["body"])
        self.assertIn(b"EPUB-DATEN", request["body"])
        self.assertIn(b"application/epub+zip", request["body"])
        self.assertEqual("acc-1", request["headers"]["t_auth_token"])

    def test_an_unconfirmed_upload_is_an_error(self):
        backend = Backend()
        backend.upload_reply = {"metadata": {}}
        with write_client(backend) as client, self.temporary_epub() as path:
            with self.assertRaises(TolinoApiError) as caught:
                client.upload(path)
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("deliverableId", str(caught.exception))

    def temporary_epub(self):
        """Wegwerf-Datei mit EPUB-Inhalt, damit der MIME-Typ echt ist."""
        import tempfile, pathlib
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = pathlib.Path(directory.name, "Buch.epub")
        path.write_bytes(b"EPUB-DATEN")
        return contextlib.nullcontext(str(path))


class MetadataTests(unittest.TestCase):
    """Metadaten werden gemerged und nur nach Read-back bestätigt."""

    def test_fields_are_merged_and_read_back(self):
        backend = Backend()
        backend.metadata["d1"] = {"title": "Alt", "author": "A", "isbn": ""}
        with write_client(backend) as client:
            result = client.update_metadata(
                "d1", {"title": "Neu", "isbn": "9780306406157"})
        self.assertEqual({"title": "Neu", "author": "A",
                          "isbn": "9780306406157"}, result)
        self.assertEqual(["GET", "PUT", "GET"],
                         [r["method"] for r in backend.server.requests])
        self.assertEqual({"title": "Neu", "author": "A",
                          "isbn": "9780306406157"},
                         sent_body(backend, 1)["uploadMetaData"])

    def test_a_silently_ignored_update_is_reported(self):
        backend = Backend()
        backend.metadata["d1"] = {"title": "Alt"}
        backend.ignore_put = True
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.update_metadata("d1", {"title": "Neu"})
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("nicht best", str(caught.exception))

    def test_an_empty_update_never_reaches_the_network(self):
        backend = Backend()
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.update_metadata("d1", {})
        self.assertEqual("E99", caught.exception.code)
        self.assertEqual([], backend.server.requests)

    def test_a_book_without_metadata_in_the_cloud_is_an_error(self):
        backend = Backend()
        backend.hide_metadata = True
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.update_metadata("unbekannt", {"title": "Neu"})
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("keine Metadaten", str(caught.exception))


class ReadMarkTests(unittest.TestCase):
    """Gelesen-Marken: ein PATCH mit der aktuellen Revision, nie doppelt."""

    def test_all_books_are_marked_in_one_patch_with_the_read_revision(self):
        backend = Backend()
        with write_client(backend) as client:
            self.assertEqual(8, client.mark_read_many(["d1", "d2", "d1"]))
        body = backend.patch_bodies[-1]
        self.assertEqual(7, body["revision"])
        self.assertEqual([read_tag_path("d1"), read_tag_path("d2")],
                         [patch["path"] for patch in body["patches"]])
        for patch in body["patches"]:
            self.assertEqual("add", patch["op"])
            self.assertEqual(READ_TAG, patch["value"]["name"])
            self.assertEqual("system", patch["value"]["category"])

    def test_unreading_removes_only_the_mark_that_exists(self):
        backend = Backend()
        backend.patches = [
            {"op": "add", "path": read_tag_path("d1", "tag-9"),
             "value": {"name": READ_TAG, "category": "system",
                       "revision": 3}},
            {"op": "add", "path": "/publications/d2/tags/tag-8",
             "value": {"name": "Grenzwelten", "category": "collection"}},
        ]
        with write_client(backend) as client:
            client.mark_read_many(["d1", "d2"], finished=False)
        body = backend.patch_bodies[-1]
        self.assertEqual(1, len(body["patches"]))
        patch = body["patches"][0]
        self.assertEqual(("remove", read_tag_path("d1", "tag-9")),
                         (patch["op"], patch["path"]))
        self.assertEqual(("system", 3),
                         (patch["value"]["category"],
                          patch["value"]["revision"]))

    def test_an_already_read_book_causes_no_write(self):
        backend = Backend()
        backend.patches = [{"op": "add", "path": read_tag_path("d1"),
                            "value": {"name": READ_TAG,
                                      "category": "system"}}]
        with write_client(backend) as client:
            self.assertEqual(7, client.mark_read_many(["d1"]))
        self.assertEqual([], backend.patch_bodies[-1]["patches"])

    def test_marking_without_a_book_is_an_error(self):
        backend = Backend()
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.mark_read_many(["", "  "])
        self.assertEqual("E99", caught.exception.code)
        self.assertEqual([], backend.server.requests)

    def test_a_collection_is_added_with_the_current_revision(self):
        backend = Backend()
        with write_client(backend) as client:
            self.assertEqual(8, client.add_to_collection("d1", "Grenzwelten"))
        body = backend.patch_bodies[-1]
        self.assertEqual(7, body["revision"])
        self.assertEqual({"op": "add", "path": read_tag_path("d1")},
                         {key: body["patches"][0][key]
                          for key in ("op", "path")})
        self.assertEqual(("Grenzwelten", "collection"),
                         (body["patches"][0]["value"]["name"],
                          body["patches"][0]["value"]["category"]))


class DeleteAndDownloadTests(unittest.TestCase):
    """Löschen und Download nennen genau eine Kennung -- kodiert, nie roh."""

    def test_delete_names_the_deliverable(self):
        backend = Backend()
        with write_client(backend) as client:
            client.delete("d 1/ä")
        self.assertEqual(1, len(backend.deleted))
        self.assertIn("deliverableId=d%201/%C3%A4", backend.deleted[0])

    def test_the_download_info_asks_for_the_base64_id(self):
        backend = Backend()
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError):
                client.download("d1")
        expected = _download_token("d1")
        self.assertEqual("/bosh/rest/cloud/downloadinfo/%s/%s/type/external-download"
                         % (expected, expected),
                         backend.server.requests[0]["path"])
        self.assertNotIn("d1", expected)

    def test_a_download_without_a_link_is_an_error(self):
        backend = Backend()
        backend.download_info = {"DownloadInfo": {}}
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.download("d1")
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("Download-Link", str(caught.exception))

    def test_an_http_download_link_is_refused(self):
        backend = Backend()
        backend.download_info = {"DownloadInfo": {
            "contentUrl": "http://example.invalid/buch.epub"}}
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.download("d1")
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("HTTPS", caught.exception.short)


if __name__ == "__main__":
    unittest.main()

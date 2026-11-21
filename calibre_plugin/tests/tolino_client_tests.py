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
import types
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request

from .. import tolino as tolino_module
from ..errors import TolinoApiError, TolinoAuthError, TolinoError
from ..sync_data import READ_TAG
from ..tolino import TolinoClient, report_error, sanitize_error
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


DEVICES_BODY = json.dumps({"deviceListResponse": {"devices": [
    {"deviceId": HARDWARE, "deviceLastUsage": "2026-01-01"}]}}).encode()


def devices_reply(_method, _path, _headers, _payload):
    return 200, JSON_HEADERS, DEVICES_BODY


def token_and(devices):
    """Server: Token-Endpunkt normal, jeder andere Pfad an `devices`."""

    def responder(method, path, headers, payload):
        if path.startswith("/token/"):
            return 200, JSON_HEADERS, TOKEN_BODY
        return devices(method, path, headers, payload)

    return responder


class ReportErrorTests(unittest.TestCase):
    """Die Meldung im Dialog ist eine Zeile -- mit Code, ohne Zugangsdaten."""

    def test_the_short_text_and_the_code_form_the_user_message(self):
        exc = TolinoApiError(
            "Serverfehler", code="N03",
            short="tolino meldet einen Fehler (HTTP 400).",
            detail="Body mit tok-123")
        self.assertEqual(
            "tolino meldet einen Fehler (HTTP 400). (Code N03)",
            report_error(exc, "Sync", ("tok-123",)))

    def test_an_unexpected_error_gets_the_generic_code(self):
        self.assertEqual("Unerwarteter Fehler: kaputt (Code E99)",
                         report_error(RuntimeError("kaputt"), "Lauf"))


class RetryTests(unittest.TestCase):
    """Einmal nutzbare Tokens: jede Wiederholung laeuft genau einmal."""

    def test_a_401_renews_the_access_token_and_repeats_the_call(self):
        calls = {"token": 0, "devices": 0}

        def responder(method, path, headers, payload):
            if path.startswith("/token/"):
                calls["token"] += 1
                return 200, JSON_HEADERS, json.dumps({
                    "access_token": "acc-%d" % calls["token"],
                    "refresh_token": "rot-%d" % calls["token"],
                    "expires_in": 3600}).encode()
            calls["devices"] += 1
            if calls["devices"] == 1:
                return 401, {}, b'{"error": "expired"}'
            return devices_reply(method, path, headers, payload)

        with LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            self.assertEqual(HARDWARE, client.fetch_hardware_id())
            sent = [request["headers"].get("t_auth_token")
                    for request in server.requests
                    if request["path"].endswith("/handshake/devices/list")]
        self.assertEqual(2, calls["token"])
        self.assertEqual(["acc-1", "acc-2"], sent)

    def test_a_403_at_the_token_endpoint_waits_and_retries(self):
        calls = {"token": 0}

        def responder(_method, path, _headers, _payload):
            if not path.startswith("/token/"):
                return 404, {}, b""
            calls["token"] += 1
            if calls["token"] == 1:
                # Nur eine echte Bot-Prüfung rechtfertigt die Wartezeit.
                return 403, {}, b"<html><body>captcha</body></html>"
            return 200, JSON_HEADERS, TOKEN_BODY

        with patch("calibre_plugin.tolino.time.sleep") as slept, \
                LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            self.assertEqual("rot-1", client.login())
        self.assertEqual(2, calls["token"])
        self.assertEqual([2.0], [call.args[0] for call in slept.call_args_list])

    def test_a_failing_token_callback_stops_the_login(self):
        def fail(_token):
            raise OSError("Platte voll")

        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_callback=fail)
            with self.assertRaises(TolinoAuthError) as caught:
                client.login()
        self.assertEqual("A01", caught.exception.code)
        self.assertIsNone(client.access)

    def test_a_token_callback_that_returns_false_stops_the_login(self):
        with token_server() as server, local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  token_callback=lambda _token: False)
            with self.assertRaises(TolinoAuthError) as caught:
                client.login()
        self.assertEqual("A01", caught.exception.code)
        self.assertIsNone(client.access)

    def test_an_adopted_id_is_saved_once_after_a_successful_call(self):
        saved = []
        with LocalHTTP(token_and(devices_reply)) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  hardware_callback=saved.append)
            client.hardware = "adopted"
            client._hw_adopted = True
            client.fetch_hardware_id()
        self.assertEqual(["adopted"], saved)
        self.assertFalse(client._hw_adopted)

    def test_a_hardware_callback_that_returns_false_is_reported(self):
        with LocalHTTP(token_and(devices_reply)) as server, \
                local_tolino_endpoints(server.origin), patch(
                    "calibre_plugin.tolino.log_warning") as warned:
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  hardware_callback=lambda _hardware: False)
            client.hardware = "adopted"
            client._hw_adopted = True
            client.fetch_hardware_id()
        self.assertEqual(1, warned.call_count)
        self.assertIn("Geräte-ID", str(warned.call_args[0][0]))

    def test_a_failed_call_keeps_the_adopted_id_unsaved(self):
        def refuse(_method, _path, _headers, _payload):
            return 500, {}, b""

        saved = []
        with LocalHTTP(token_and(refuse)) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  hardware_callback=saved.append)
            client.hardware = "adopted"
            client._hw_adopted = True
            # Gueltiger Zugang: der Fehler kommt aus dem Geraete-Aufruf, nicht
            # aus einer Anmeldung (die wuerde die ID schon bestaetigen).
            client.access = "acc-1"
            client.expires_at = time.time() + 3600
            with self.assertRaises(TolinoError):
                client.fetch_hardware_id()
        self.assertEqual([], saved)
        self.assertTrue(client._hw_adopted)

    def test_a_400_adopts_the_registered_id_and_repeats_the_call(self):
        """Unbekannte Geräte-ID: die registrierte übernehmen, dann einmal neu."""
        calls = {"devices": 0}

        def responder(_method, path, _headers, _payload):
            if path.startswith("/token/"):
                return 200, JSON_HEADERS, TOKEN_BODY
            calls["devices"] += 1
            if calls["devices"] == 1:
                return 400, {}, b'{"error": "unknown hardware"}'
            return 200, JSON_HEADERS, json.dumps({"deviceListResponse": {
                "devices": [{"deviceId": "reg-1",
                              "deviceLastUsage": "2026-01-01"}]}}).encode()

        saved = []
        with patch("calibre_plugin.tolino.time.sleep"), \
                LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start",
                                  hardware_callback=saved.append)
            self.assertEqual("reg-1", client.fetch_hardware_id())
            sent = [request["headers"].get("hardware_id")
                    for request in server.requests
                    if request["path"].endswith("/devices/list")]
        # Erster Aufruf, Suche nach der registrierten ID, Wiederholung.
        self.assertEqual(3, calls["devices"])
        self.assertEqual([HARDWARE, HARDWARE, "reg-1"], sent)
        self.assertEqual(["reg-1"], saved)

    def test_a_400_for_the_registered_id_is_not_retried(self):
        """Schon die registrierte ID: der 400 hat eine andere Ursache."""
        calls = {"devices": 0}

        def responder(method, path, headers, payload):
            if path.startswith("/token/"):
                return 200, JSON_HEADERS, TOKEN_BODY
            calls["devices"] += 1
            if calls["devices"] == 1:
                return 400, {}, b"{}"
            return devices_reply(method, path, headers, payload)

        with patch("calibre_plugin.tolino.time.sleep") as slept, \
                LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoApiError) as caught:
                client.fetch_hardware_id()
        self.assertEqual("N03", caught.exception.code)
        self.assertEqual(2, calls["devices"])
        self.assertFalse(slept.called)
        self.assertEqual(HARDWARE, client.hardware)

    def test_a_second_rejection_after_registering_stops_the_call(self):
        """Registrierung gelingt, der Server lehnt trotzdem ab: Ende, kein Kreis."""
        calls = {"devices": 0, "register": 0}

        def responder(_method, path, _headers, _payload):
            if path.startswith("/token/"):
                return 200, JSON_HEADERS, TOKEN_BODY
            if path.endswith("/registerhw"):
                calls["register"] += 1
                return 200, JSON_HEADERS, b"{}"
            calls["devices"] += 1
            if calls["devices"] == 2:
                # Leere Geräteliste: es gibt nichts zu übernehmen.
                return 200, JSON_HEADERS, b'{"deviceListResponse": {"devices": []}}'
            return 400, {}, b"{}"

        with patch("calibre_plugin.tolino.time.sleep"), \
                LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoApiError) as caught:
                client.fetch_hardware_id()
        self.assertEqual("N03", caught.exception.code)
        # Ein Versuch nach der Registrierung, danach der Warte-Versuch.
        self.assertEqual((1, 4), (calls["register"], calls["devices"]))
        self.assertEqual(HARDWARE, client.hardware)

    def test_a_403_without_a_bot_check_is_not_repeated(self):
        with patch("calibre_plugin.tolino.time.sleep") as slept, \
                token_server(body=b'{"error": "forbidden"}',
                             status=403) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoAuthError) as caught:
                client.login()
        self.assertEqual("A01", caught.exception.code)
        self.assertEqual(1, len(server.requests))
        self.assertFalse(slept.called)

    def test_a_403_away_from_the_token_endpoint_is_no_login_error(self):
        def responder(_method, _path, _headers, _payload):
            return 403, {}, b"quota exceeded"

        with patch("calibre_plugin.tolino.time.sleep") as slept, \
                LocalHTTP(token_and(responder)) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            client.access, client.expires_at = "acc-1", time.time() + 3600
            with self.assertRaises(TolinoApiError) as caught:
                client.fetch_hardware_id()
        self.assertEqual("N03", caught.exception.code)
        self.assertFalse(slept.called)

    def test_the_bot_check_retries_stop_at_the_limit(self):
        def responder(_method, _path, _headers, _payload):
            return 403, {}, b"<html>captcha</html>"

        with patch("calibre_plugin.tolino.time.sleep") as slept, \
                LocalHTTP(responder) as server, \
                local_tolino_endpoints(server.origin):
            client = TolinoClient(1, HARDWARE, refresh="start")
            with self.assertRaises(TolinoAuthError):
                client.login()
        limit = len(tolino_module._WAF_RETRY_SECONDS)
        self.assertEqual(limit + 1, len(server.requests))
        self.assertEqual(limit, slept.call_count)


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
        self.inventory = {"PublicationInventory": {"edata": []}}
        self.sync_response = None
        self.ignore_put = False
        self.hide_metadata = False
        self.deleted = []
        self.cover_posts = []
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
        if path.startswith("/bosh/rest/inventory/delta"):
            return json_reply(self.inventory)
        if path.startswith("/bosh/rest/sync-data"):
            body = json.loads(payload or b"{}")
            self.patch_bodies.append(body)
            if self.sync_response is not None:
                return json_reply(self.sync_response)
            if body.get("patches"):
                self.revision += 1
            return json_reply({"revision": self.revision,
                               "patches": self.patches})
        if path.startswith("/bosh/rest/cover"):
            self.cover_posts.append({"headers": headers, "body": payload})
            return json_reply({})
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


class CoverTests(unittest.TestCase):
    """Cover-Upload: Control-Block und Deliverable-ID wie vom Server erwartet."""

    def test_the_cover_is_sent_with_control_block_and_deliverable(self):
        backend = Backend()
        data = b"\x89PNG\r\n\x1a\nCOVERDATEN"
        with write_client(backend) as client:
            client.upload_cover("d1", data, ".png")
        self.assertEqual(1, len(backend.cover_posts))
        post = backend.cover_posts[0]
        body = post["body"]
        self.assertIn("multipart/form-data; boundary=",
                      post["headers"]["content-type"])
        self.assertIn(b'name="deliverableId"', body)
        self.assertIn(b"d1", body)
        self.assertIn(b'name="control"', body)
        self.assertIn(b'"filesize":%d' % len(data), body)
        self.assertIn(b'filename="cover.png"', body)
        self.assertIn(data, body)


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


class FakeResponse:
    """Opener-Antwort an der Netznaht; liefert die fertigen Bytes."""

    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


def download_backend(content_url):
    backend = Backend()
    backend.download_info = {"DownloadInfo": {"contentUrl": content_url}}
    return backend


def credentials(request):
    """ALLE Kopfzeilen: regulaere und nicht weiterleitbare."""
    return ({name.lower() for name in request.headers}
            | {name.lower() for name in request.unredirected_hdrs})


def content_opener(payload):
    """Opener-Doppel nur fuer den Inhalt; die lokale Cloud bleibt echt."""
    seen = []

    def open_(request, timeout=None, **_kwargs):
        seen.append(request)
        return FakeResponse(payload)

    return types.SimpleNamespace(open=open_), seen


class DownloadContentTests(unittest.TestCase):
    """Der Inhalt kommt als Bytes -- Zugangsdaten nur an pageplace."""

    def test_the_downloaded_bytes_are_returned(self):
        backend = download_backend("https://cdn.pageplace.de/buch.epub")
        opener, seen = content_opener(b"EPUB-INHALT")
        with write_client(backend) as client, patch(
                "calibre_plugin.tolino._DOWNLOAD_OPENER", opener):
            self.assertEqual(b"EPUB-INHALT", client.download("d1"))
        request = seen[0]
        self.assertEqual("https://cdn.pageplace.de/buch.epub",
                         request.full_url)
        self.assertIn("t_auth_token", credentials(request))
        # Der Zugangstoken darf einer Weiterleitung NICHT folgen.
        self.assertIn("t_auth_token",
                      {name.lower() for name in request.unredirected_hdrs})
        self.assertNotIn("t_auth_token",
                         {name.lower() for name in request.headers})

    def test_a_foreign_cdn_gets_no_credentials(self):
        backend = download_backend("https://cdn.example.test/buch.epub")
        opener, seen = content_opener(b"PDF")
        with write_client(backend) as client, patch(
                "calibre_plugin.tolino._DOWNLOAD_OPENER", opener):
            self.assertEqual(b"PDF", client.download("d1"))
        headers = credentials(seen[0])
        for name in ("t_auth_token", "hardware_id", "reseller_id"):
            self.assertNotIn(name, headers)

    


class RedirectTests(unittest.TestCase):
    """Eine unsichere Weiterleitung wird abgelehnt, bevor sie kontaktiert wird.

    Der Handler sitzt in `_DOWNLOAD_OPENER`; `download` folgt jeder
    Weiterleitung nur ueber ihn.
    """

    def handler(self):
        return tolino_module._SafeRedirects()

    def test_an_https_target_is_followed(self):
        request = Request("https://cdn.example.test/buch.epub")
        followed = self.handler().redirect_request(
            request, None, 302, {}, {},
            "https://cdn.example.test/neu.epub")
        self.assertEqual("https://cdn.example.test/neu.epub",
                         followed.full_url)

    def test_a_plain_http_target_is_refused(self):
        request = Request("https://cdn.example.test/buch.epub")
        with self.assertRaises(TolinoApiError) as caught:
            self.handler().redirect_request(
                request, None, 302, {}, {},
                "http://cdn.example.test/neu.epub")
        self.assertEqual("N02", caught.exception.code)


class ReadMarkTests(unittest.TestCase):
    """Gelesen-Marken: ein PATCH mit der aktuellen Revision, nie doppelt."""

    def test_all_books_are_marked_in_one_patch_with_the_read_revision(self):
        backend = Backend()
        with write_client(backend) as client:
            self.assertEqual(8, client.mark_read_many(["d1", "d2", "d1"]))
        body = backend.patch_bodies[-1]
        self.assertEqual(7, body["revision"])
        self.assertEqual([read_tag_path("d1"), read_tag_path("d2")],
                         [entry["path"] for entry in body["patches"]])
        for entry in body["patches"]:
            self.assertEqual("add", entry["op"])
            self.assertEqual(READ_TAG, entry["value"]["name"])
            self.assertEqual("system", entry["value"]["category"])

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
        entry = body["patches"][0]
        self.assertEqual(("remove", read_tag_path("d1", "tag-9")),
                         (entry["op"], entry["path"]))
        self.assertEqual(("system", 3),
                         (entry["value"]["category"],
                          entry["value"]["revision"]))

    def test_a_missing_revision_is_never_sent_as_null(self):
        backend = Backend()
        backend.revision = None
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.mark_read_many(["d1"])
        self.assertEqual("N02", caught.exception.code)
        # Nur der lesende PATCH ging raus; ohne Revision wird nicht geschrieben.
        self.assertEqual([{"revision": None, "patches": []}],
                         backend.patch_bodies)

    def test_unreading_removes_both_cloud_spellings(self):
        backend = Backend()
        backend.patches = [
            {"op": "add", "path": read_tag_path("d1", "tag-1"),
             "value": {"name": READ_TAG, "category": "system",
                       "revision": 3}},
            {"op": "add", "path": read_tag_path("d1", "tag-2"),
             "value": {"name": READ_TAG, "category": "collection",
                       "revision": 4}},
        ]
        with write_client(backend) as client:
            client.mark_read_many(["d1"], finished=False)
        body = backend.patch_bodies[-1]
        self.assertEqual(
            [("remove", "system", "tag-1"),
             ("remove", "collection", "tag-2")],
            [(entry["op"], entry["value"]["category"],
              entry["path"].rsplit("/", 1)[-1])
             for entry in body["patches"]])

    def test_an_empty_revision_passed_on_is_refused(self):
        backend = Backend()
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.add_to_collection("d1", "Serie", revision="")
        self.assertEqual("N02", caught.exception.code)
        self.assertEqual([], backend.patch_bodies)

    def test_a_malformed_snapshot_is_not_an_empty_read_state(self):
        backend = Backend()
        backend.sync_response = {}
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.read_ids()
            with self.assertRaises(TolinoApiError):
                client.mark_read_many(["d1"])
        self.assertEqual("N02", caught.exception.code)
        # Nur lesende PATCHes, kein Schreibvorgang mit leerem Bestand.
        self.assertTrue(all(body["patches"] == []
                            for body in backend.patch_bodies))

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


class SyncSnapshotCacheTests(unittest.TestCase):
    """Ein Lesevorgang bedient beide Abfragen; ein Schreibvorgang verwirft ihn."""

    def test_reads_share_one_snapshot_and_a_write_forces_a_new_one(self):
        backend = Backend()
        backend.patches = [{"op": "add", "path": read_tag_path("d1"),
                            "value": {"name": READ_TAG, "category": "system"}}]
        with write_client(backend) as client:
            self.assertEqual({"d1"}, client.read_ids())
            self.assertEqual(set(), client.collection_pairs())
            client.add_to_collection("d1", "Grenzwelten")
            self.assertEqual({"d1"}, client.read_ids())
        reads = [body for body in backend.patch_bodies
                 if body == {"revision": None, "patches": []}]
        # Zwei lesende Abrufe: vor und nach dem Schreibvorgang.
        self.assertEqual(2, len(reads))


class InventoryTests(unittest.TestCase):
    """Ein teilweise defekter Bestand ist kein gueltiger Bestand."""

    def test_a_complete_inventory_is_returned(self):
        backend = Backend()
        backend.inventory = {"PublicationInventory": {"edata": [
            {"deliverableId": "d1", "title": "Gut", "authors": "Autor"}]}}
        with write_client(backend) as client:
            self.assertEqual(["d1"],
                             [row["tolino_id"] for row in client.inventory()])

    def test_a_partly_broken_inventory_is_refused(self):
        backend = Backend()
        backend.inventory = {"PublicationInventory": {"edata": [
            {"deliverableId": "d1", "title": "Gut"},
            {"title": "Ohne Kennung"}]}}
        with write_client(backend) as client:
            with self.assertRaises(TolinoApiError) as caught:
                client.inventory()
        self.assertEqual("N02", caught.exception.code)
        self.assertIn("unvollständig", str(caught.exception))


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
        opener = content_opener(b"x")[0]
        with write_client(backend) as client, patch(
                "calibre_plugin.tolino._DOWNLOAD_OPENER", opener):
            self.assertEqual(b"x", client.download("d1"))
        # Literaler Wire-Wert: ein Fehler in der Kodierung darf den Sollwert
        # nicht mitverfaelschen (base64("d1") = "ZDE=", URL-kodiert).
        self.assertEqual(
            "/bosh/rest/cloud/downloadinfo/ZDE%3D/ZDE%3D/type/external-download",
            backend.server.requests[0]["path"])

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

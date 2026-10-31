"""Tolino-Client, Transport, Tokens und Fehlertexte."""

import ast
import base64
import calibre_plugin.tolino as tolino_module
import os
import re
import tempfile
import time
import unittest
from . import ui_base_tests, worker_base_tests
from .. import config
from ..partners import PARTNERS, WEBREADER_URL
from ..tolino import TolinoAuthError, TolinoClient
from ..sync_data import READ_TAG, active_tags, tag_id_from_patch
from ..run_context import RunContext
from ..browser_login import grab_live_refresh
from ._support import (compare_inventory, start_cdp_stubs, stop_cdp_stubs)
from pathlib import Path
from unittest.mock import patch


# Der Einzellauf richtet die CDP-Stubs selbst ein.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs

class TolinoClientFeatureTests(unittest.TestCase):
    """Tests for the cloud client: device list, download, sync-data
    collections/read-state and refresh expiry tracking."""

    def _client(self):
        return TolinoClient(4, "hardware", "refresh-token")

    def _login_client(self, token_response=None):
        client = self._client()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return token_response or (
                    b'{"access_token":"access-secret",'
                    b'"refresh_token":"rotated","expires_in":3600,'
                    b'"refresh_expires_in":36000}')

        with patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            client.login()
        return client

    def _device_list_client(self, devices_payload):
        client = self._login_client()
        captured = {}

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return devices_payload

        def request(url_request, timeout):
            captured["url"] = url_request.full_url
            captured["body"] = url_request.data.decode("utf-8")
            return Response()

        with patch("calibre_plugin.tolino.urlopen", request):
            hardware = client.fetch_hardware_id()
        self.assertIn("handshake/devices/list", captured["url"])
        self.assertIn("deviceListRequest", captured["body"])
        self.assertIn("auth_token", captured["body"])
        return hardware

    def test_fetch_hardware_id_returns_most_recent_device(self):
        payload = (b'{"deviceListResponse":{"devices":[{'
                   b'"deviceId":"older","deviceLastUsage":"100"},{'
                   b'"deviceId":"newest","deviceLastUsage":"200"}]}}')
        self.assertEqual("newest", self._device_list_client(payload))

    def test_fetch_hardware_id_rejects_empty_device_list(self):
        from ..tolino import TolinoApiError
        client = self._login_client()

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"deviceListResponse":{"devices":[]}}'

        with patch("calibre_plugin.tolino.urlopen", return_value=Response()):
            with self.assertRaises(TolinoApiError):
                client.fetch_hardware_id()

    def _patching_client(self, responses):
        """Client whose _request returns queued dict responses."""
        client = self._login_client()
        queue = list(responses)
        calls = []

        def fake_request(url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None):
            calls.append({"url": url, "method": method, "data": data})
            return queue.pop(0)

        client._request = fake_request
        return client, calls

    def test_add_to_collection_sends_tag_add_patch(self):
        """Die Zuordnung wird mit der aktuellen Revision geschrieben.

        Ohne Revision könnte der PATCH den vorhandenen Stand ersetzen und
        damit fremde Marken (Lese-Status, andere Sammlungen) verlieren.
        """
        client, calls = self._patching_client([
            {"revision": 7, "patches": []},   # Revision lesen
            {"revision": 8},                  # add-Patch
        ])
        revision = client.add_to_collection("book-1", "SciFi")
        self.assertEqual(8, revision)
        self.assertEqual(2, len(calls))
        self.assertEqual(7, calls[1]["data"]["revision"])
        self.assertEqual("PATCH", calls[1]["method"])
        self.assertIn("sync-data", calls[1]["url"])
        patch = calls[1]["data"]["patches"][0]
        self.assertEqual("add", patch["op"])
        self.assertEqual("/publications/book-1/tags", patch["path"])
        self.assertEqual("SciFi", patch["value"]["name"])
        self.assertEqual("collection", patch["value"]["category"])

    def test_add_to_collection_reuses_a_given_revision(self):
        """Mehrere Zuordnungen brauchen nur ein Lesen."""
        client, calls = self._patching_client([{"revision": 5}])
        revision = client.add_to_collection("book-1", "SciFi", revision=4)
        self.assertEqual(5, revision)
        self.assertEqual(1, len(calls))
        self.assertEqual(4, calls[0]["data"]["revision"])

    def test_mark_read_uses_system_tag(self):
        """Ein Buch: eine System-Marke, geschrieben mit der aktuellen
        Revision – Sammlungen und andere Marken bleiben unberührt."""
        client, calls = self._patching_client([
            {"revision": 9, "patches": []},
            {"revision": 10},
        ])
        revision = client.mark_read_many(["book-2"], finished=True)
        self.assertEqual(10, revision)
        self.assertEqual(9, calls[1]["data"]["revision"])
        patch = calls[1]["data"]["patches"][0]
        self.assertEqual("system", patch["value"]["category"])
        self.assertEqual("collection_finished_readings_name",
                         patch["value"]["name"])

    def test_mark_read_many_writes_one_patch_for_all_books(self):
        """Mehrere Bücher: ein Lesevorgang, ein PATCH, und schon markierte
        Bücher werden nicht erneut geschrieben."""
        already = {"op": "add", "path": "/publications/book-1/tags/6438286",
                   "value": {"name": "collection_finished_readings_name",
                             "category": "system", "revision": 3,
                             "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [already]},
            {"revision": 13},
        ])
        revision = client.mark_read_many(["book-1", "book-2"])
        self.assertEqual(13, revision)
        self.assertEqual(2, len(calls))
        self.assertEqual(12, calls[1]["data"]["revision"])
        self.assertEqual(["/publications/book-2/tags"],
                         [item["path"] for item in calls[1]["data"]["patches"]])

    def test_mark_read_many_clears_only_existing_marks(self):
        """Ungelesen setzen: der Pfad trägt die Marken-ID der Cloud-Marke.

        Feldbefund: ohne ID antwortet der Server mit HTTP 400
        «missing mandatory unitid fragment inside property path for remove».
        """
        marked = {"op": "add", "path": "/publications/book-1/tags/6438286",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},
            {"revision": 13},
        ])
        client.mark_read_many(["book-1", "book-2"], finished=False)
        patches = calls[1]["data"]["patches"]
        self.assertEqual(["remove"], [item["op"] for item in patches])
        self.assertEqual(3, patches[0]["value"]["revision"])
        self.assertEqual("/publications/book-1/tags/6438286",
                         patches[0]["path"])
        self.assertEqual(12, calls[1]["data"]["revision"])

    def test_mark_read_many_mixes_add_and_remove_in_one_patch(self):
        """Zwei Bücher, zwei Richtungen: «gelesen» ohne ID (der Server
        vergibt sie), «ungelesen» mit der ID der vorhandenen Marke."""
        marked = {"op": "add", "path": "/publications/book-1/tags/6438286",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},   # lesen
            {"revision": 13},                        # entfernen
            {"revision": 14, "patches": [marked]},   # wieder lesen
            {"revision": 15},                        # hinzufügen
        ])
        client.mark_read_many(["book-1"], finished=False)
        client.mark_read_many(["book-3"], finished=True)
        removed = calls[1]["data"]["patches"]
        added = calls[3]["data"]["patches"]
        self.assertEqual(["/publications/book-1/tags/6438286"],
                         [item["path"] for item in removed])
        self.assertEqual(["/publications/book-3/tags"],
                         [item["path"] for item in added])

    def test_removing_a_mark_without_an_id_fails_loudly(self):
        """Liefert der Server keine Marken-ID, gibt es kein Raten: der
        Fehler kommt sofort und im Klartext, nicht als HTTP 400."""
        from ..tolino import TolinoApiError
        marked = {"op": "add", "path": "/publications/book-1/tags",
                  "value": {"name": "collection_finished_readings_name",
                            "category": "system", "revision": 3,
                            "modified": 1}}
        client, calls = self._patching_client([
            {"revision": 12, "patches": [marked]},
            {"revision": 13},
        ])
        with self.assertRaises(TolinoApiError) as caught:
            client.mark_read_many(["book-1"], finished=False)
        self.assertIn("Marken-ID", str(caught.exception))
        self.assertEqual(1, len(calls))   # kein Schreibversuch

    def test_mark_read_many_requires_ids(self):
        from ..tolino import TolinoApiError
        client, calls = self._patching_client([])
        with self.assertRaises(TolinoApiError):
            client.mark_read_many([])
        self.assertEqual([], calls)

    def test_update_metadata_merges_and_puts_upload_metadata(self):
        existing = {"metadata": {"deliverableId": "b-1", "identifier": "b-1",
                                 "title": "Alt"}}
        saved = {"metadata": {"deliverableId": "b-1", "identifier": "b-1",
                              "title": "Neu", "isbn": "123"}}
        client, calls = self._patching_client([existing, {"ok": True}, saved])
        result = client.update_metadata("b-1", {"title": "Neu", "isbn": "123"})
        self.assertEqual("Neu", result["title"])
        self.assertEqual("123", result["isbn"])
        self.assertEqual("b-1", result["deliverableId"])
        self.assertEqual("GET", calls[0]["method"])
        self.assertIn("/meta", calls[0]["url"])
        self.assertIn("deliverableId=b-1", calls[0]["url"])
        self.assertEqual("PUT", calls[1]["method"])
        payload = calls[1]["data"]
        self.assertEqual("Neu", payload["uploadMetaData"]["title"])
        self.assertEqual("b-1", payload["uploadMetaData"]["deliverableId"])
        self.assertEqual("GET", calls[2]["method"])

    def test_update_metadata_fails_if_read_back_keeps_old_title(self):
        from ..tolino import TolinoApiError
        existing = {"metadata": {"deliverableId": "b-1", "title": "Alt"}}
        stale = {"metadata": {"deliverableId": "b-1", "title": "Alt"}}
        client, calls = self._patching_client([existing, {"ok": True}, stale])
        with self.assertRaises(TolinoApiError) as caught:
            client.update_metadata("b-1", {"title": "Neu"})
        self.assertEqual("N02", caught.exception.code)
        self.assertEqual(["GET", "PUT", "GET"],
                         [call["method"] for call in calls])

    def test_update_metadata_requires_fields_and_metadata_response(self):
        from ..tolino import TolinoApiError
        client, _ = self._patching_client([])
        with self.assertRaises(TolinoApiError):
            client.update_metadata("b-1", {})
        client2, _ = self._patching_client([{"unexpected": {}}])
        with self.assertRaises(TolinoApiError):
            client2.update_metadata("b-1", {"title": "x"})

    def test_update_metadata_leaves_tags_and_read_state_alone(self):
        """Titel/Autor/ISBN wandern über /meta – Sammlungen und die
        Lese-Marke in der Cloud werden dabei nicht angefasst."""
        existing = {"metadata": {
            "deliverableId": "b-1", "title": "Alt",
            "tags": [{"name": "Adamsberg", "category": "collection"},
                     {"name": "collection_finished_readings_name",
                      "category": "system"}]}}
        updated = {"metadata": {
            "deliverableId": "b-1", "title": "Neu",
            "tags": [{"name": "Adamsberg", "category": "collection"},
                     {"name": "collection_finished_readings_name",
                      "category": "system"}]}}
        client, calls = self._patching_client(
            [existing, {"ok": True}, updated])
        client.update_metadata("b-1", {"title": "Neu"})
        self.assertEqual(["GET", "PUT", "GET"],
                         [call["method"] for call in calls])
        self.assertEqual([], [call for call in calls
                              if "sync-data" in call["url"]])
        sent = calls[1]["data"]["uploadMetaData"]
        self.assertEqual("Neu", sent["title"])
        # Die Marken aus der Cloud-Antwort bleiben unverändert erhalten.
        self.assertEqual(
            [{"name": "Adamsberg", "category": "collection"},
             {"name": "collection_finished_readings_name", "category": "system"}],
            sent["tags"])

    def test_read_ids_reads_the_system_tag(self):
        """Gelesen-Marken des Kontos – nur die System-Marke zählt, und eine
        remove-Marke nimmt das Buch wieder heraus."""
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "add", "path": "/publications/b-2/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-3/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "remove", "path": "/publications/b-3/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({"b-1"}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_snapshot_serves_collections_and_read_state_once(self):
        """Sammlungen und Lese-Status liegen in denselben Daten: EIN
        Lesevorgang reicht (Feldbefund: nicht bei jedem Sync alles neu)."""
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({("b-1", "Adamsberg")}, client.collection_pairs())
        self.assertEqual({"b-1"}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_snapshot_is_dropped_after_a_write(self):
        """Nach einem Schreibvorgang wird neu gelesen – keine veraltete
        Revision, kein zweiter Schreibversuch auf altem Stand."""
        client, calls = self._patching_client([
            {"revision": 5, "patches": []},   # snapshot
            {"revision": 6},                  # add_to_collection
            {"revision": 6, "patches": []},   # neuer snapshot
        ])
        client.collection_pairs()
        client.add_to_collection("b-1", "SciFi")
        client.read_ids()
        self.assertEqual(3, len(calls))
        self.assertEqual("PATCH", calls[1]["method"])

    def test_mark_read_many_uses_the_snapshot(self):
        """Der Bulk-Aufruf liest den Stand einmal und schreibt einmal."""
        client, calls = self._patching_client([
            {"revision": 7, "patches": []},
            {"revision": 8},
        ])
        client.mark_read_many(["b-1", "b-2"])
        self.assertEqual(2, len(calls))

    def test_collection_pairs_reads_sync_data_patches(self):
        patches = [
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "Adamsberg", "category": "collection"}},
            {"op": "add", "path": "/publications/b-2/tags",
             "value": {"name": "SciFi", "category": "collection"}},
            {"op": "remove", "path": "/publications/b-2/tags",
             "value": {"name": "SciFi", "category": "collection"}},
            {"op": "add", "path": "/publications/b-1/tags",
             "value": {"name": "read", "category": "system"}},
            {"op": "add", "path": "/somewhere/else",
             "value": {"name": "X", "category": "collection"}},
        ]
        client, calls = self._patching_client(
            [{"revision": 5, "patches": patches}])
        self.assertEqual({("b-1", "Adamsberg")}, client.collection_pairs())
        self.assertEqual("PATCH", calls[0]["method"])
        self.assertIn("sync-data", calls[0]["url"])

    def test_download_sends_credentials_only_to_pageplace_https_hosts(self):
        client = self._login_client()
        captured = []

        class RawResponse:
            status = 200

            def __enter__(self):
                return self

            def geturl(self):
                return "https://example.invalid/book"

            def __exit__(self, *args):
                return False

            def read(self):
                return b"content"

        def run_download(url):
            client._request = lambda *_args, **_kwargs: {
                "DownloadInfo": {"contentUrl": url}}

            def request(value, timeout):
                captured.append((value.full_url, dict(value.headers),
                                 dict(value.unredirected_hdrs)))
                return RawResponse()

            with patch("calibre_plugin.tolino.urlopen", request):
                return client.download("book-1")

        self.assertEqual(b"content", run_download("https://cdn.pageplace.de/file"))
        trusted_headers = {key.casefold(): value
                           for key, value in captured[-1][1].items()}
        trusted_unredirected = {
            key.casefold() for key in captured[-1][2]}
        self.assertNotIn("t_auth_token", trusted_headers)
        self.assertIn("t_auth_token", trusted_unredirected)
        self.assertIn("hardware_id", trusted_unredirected)
        self.assertIn("reseller_id", trusted_unredirected)

        self.assertEqual(b"content", run_download("https://cdn.example/file"))
        foreign_headers = {key.casefold(): value
                           for key, value in captured[-1][1].items()}
        self.assertNotIn("t_auth_token", foreign_headers)
        self.assertNotIn("hardware_id", foreign_headers)
        self.assertNotIn("reseller_id", foreign_headers)

        from ..tolino import TolinoApiError
        with self.assertRaises(TolinoApiError) as caught:
            run_download("http://cdn.pageplace.de/file")
        self.assertEqual("N02", caught.exception.code)

    def test_download_credentials_are_not_forwarded_across_redirects(self):
        from urllib.request import HTTPRedirectHandler, Request

        request = Request("https://cdn.pageplace.de/file")
        request.add_unredirected_header("t_auth_token", "secret-token")
        request.add_unredirected_header("hardware_id", "device-id")
        request.add_unredirected_header("reseller_id", "42")
        redirect = HTTPRedirectHandler().redirect_request(
            request, None, 302, "Found", {}, "https://attacker.example/collect")

        self.assertIsNotNone(redirect)
        forwarded = {key.casefold() for key in redirect.header_items()}
        self.assertNotIn("t_auth_token", forwarded)
        self.assertNotIn("hardware_id", forwarded)
        self.assertNotIn("reseller_id", forwarded)

    def test_download_resolves_content_url_and_returns_bytes(self):
        client, calls = self._patching_client([
            {"DownloadInfo": {"contentUrl": "https://cdn.example/epub"}},
        ])
        content = b"EPUB-BYTES"

        class RawResponse:
            status = 200

            def __enter__(self):
                return self

            def geturl(self):
                return "https://example.invalid/book"

            def __exit__(self, *args):
                return False

            def read(self):
                return content

        with patch("calibre_plugin.tolino.urlopen",
                   return_value=RawResponse()):
            data = client.download("b-9")
        self.assertEqual(content, data)
        self.assertIn("downloadinfo", calls[0]["url"])
        # Die Cloud will die Kennung base64-kodiert; roh antwortet sie 404.
        encoded = base64.b64encode(b"b-9").decode("ascii")
        self.assertEqual(2, calls[0]["url"].count(encoded))
        self.assertNotIn("/b-9/", calls[0]["url"])

    def test_download_encodes_an_urn_id_without_leaking_path_separators(self):
        client, calls = self._patching_client([
            {"DownloadInfo": {"contentUrl": "https://cdn.example/epub"}},
        ])

        class RawResponse:
            status = 200

            def __enter__(self):
                return self

            def geturl(self):
                return "https://example.invalid/book"

            def __exit__(self, *args):
                return False

            def read(self):
                return b"content"

        urn = "urn:uuid:0f8fad5b-d9cb-469f-a165-70867728950e"
        with patch("calibre_plugin.tolino.urlopen",
                   return_value=RawResponse()):
            client.download(urn)
        url = calls[0]["url"]
        self.assertEqual(2, url.count(
            base64.b64encode(urn.encode("utf-8")).decode("ascii")))
        self.assertTrue(url.endswith("/type/external-download"))

class BoshDeviceRecoveryTests(unittest.TestCase):
    """Ein authentifizierter BOSH-400 (leeres "{}") loest eine
    Geraete-Recovery aus: das registrierte Geraet des Kontos wird
    uebernommen, sonst wird unsere Hardware-ID per registerhw
    registriert – dann genau EIN Retry.
    """

    INVENTORY_BODY = b'{"PublicationInventory": {"edata": []}}'

    def _client(self, hardware="configured-hw"):
        from ..tolino import TolinoClient
        client = TolinoClient(4, hardware, "refresh-token-value")
        client.access = "access-value"
        client.expires_at = time.time() + 3600
        return client

    def _response(self, body, status=200):
        outer_body, outer_status = body, status

        class Response(object):
            status = outer_status

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return outer_body
        return Response()

    def _error400(self, body):
        from urllib.error import HTTPError
        error = HTTPError("https://bosh.pageplace.de/bosh/rest/x", 400,
                          "Bad Request", {}, None)
        error.read = lambda: body
        return error

    def test_error_text_surfaces_response_info_message(self):
        """BOSH meldet Fehler als ResponseInfo.message – ohne diesen
        Schluessel zeigte die Meldung nur ein nutzloses "{}"."""
        from ..tolino import TolinoApiError
        client = self._client()

        def fake_urlopen(request, timeout=None):
            raise self._error400(
                b'{"ResponseInfo": {"message": "hardware not registered"}}')

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   return_value=False):
            with self.assertRaises(TolinoApiError) as ctx:
                client.inventory()
        self.assertIn("hardware not registered", str(ctx.exception))

    def test_inventory_adopts_registered_device_and_retries(self):
        """Ein fremdes, konfiguriertes Geraet wird durch das
        registrierte des Kontos ersetzt, danach folgt GENAU ein Retry."""
        seen = []
        adopted = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if len(seen) == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("stale-hw-000")
        client.hardware_callback = adopted.append
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="eb22e4cf-bb01-4550-bff7-334446bb20b1"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(2, len(seen))
        self.assertEqual(seen[0], seen[1])
        self.assertEqual("eb22e4cf-bb01-4550-bff7-334446bb20b1",
                         client.hardware)
        self.assertEqual(["eb22e4cf-bb01-4550-bff7-334446bb20b1"], adopted)

    def test_inventory_registers_hardware_when_device_list_unavailable(self):
        """Ohne Geraeteliste wird die eigene Hardware-ID per registerhw
        registriert (wie die Referenz-Clients), dann der Retry."""
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append((request.full_url,
                         {key.casefold(): value
                          for key, value in request.headers.items()}))
            if "registerhw" in request.full_url:
                return self._response(b"{}")
            inventory_calls = len([1 for url, _ in seen
                                   if "inventory" in url])
            if inventory_calls == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("configured-hw")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.time.sleep"):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(3, len(seen))
        self.assertIn("/inventory/delta", seen[0][0])
        self.assertIn("v2/registerhw", seen[1][0])
        self.assertIn("/inventory/delta", seen[2][0])
        register_headers = seen[1][1]
        self.assertEqual("HTML5", register_headers.get("hardware_type"))
        self.assertEqual("TOLINO_WEBREADER",
                         register_headers.get("client_type"))
        self.assertEqual("configured-hw", client.hardware)

    def test_registered_current_device_raises_without_extra_calls(self):
        """Ist das konfigurierte Geraet bereits das registrierte, kommt
        kein Retry und kein registerhw – der echte Fehler 400 (andere
        Ursache) bleibt sichtbar."""
        from ..tolino import TolinoApiError
        client = self._client("configured-hw")
        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            raise self._error400(b"{}")

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="configured-hw"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")):
            with self.assertRaises(TolinoApiError):
                client.inventory()
        self.assertEqual(1, len(seen))

    def test_registration_settles_with_waiting_retries(self):
        """Feldbefund: Der Verbindungstest direkt nach registerhw
        warf noch 400 (die ID war noch nicht angekommen), der naechste
        Klick Sekunden spaeter lief. Der erste Retry wartet jetzt kurz,
        und es gibt GENAU EINEN zweiten Versuch danach."""
        seen = []
        sleeps = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if "registerhw" in request.full_url:
                return self._response(b"{}")
            inventory_calls = len([1 for url in seen if "inventory" in url])
            if inventory_calls <= 2:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("configured-hw")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   side_effect=Exception("no devices")), \
             patch("calibre_plugin.tolino.time.sleep",
                   side_effect=sleeps.append):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(4, len(seen))
        self.assertIn("/inventory/delta", seen[0])
        self.assertIn("v2/registerhw", seen[1])
        self.assertIn("/inventory/delta", seen[2])
        self.assertIn("/inventory/delta", seen[3])
        self.assertEqual([1.5, 1.5], sleeps)
        # Danach ist die ID bekannt: der naechste Lauf braucht keine
        # Recovery (Flag ist verbraucht).
        self.assertFalse(client._hw_registered_now)

    def test_adopted_device_needs_no_settle_wait(self):
        """Uebernommenes, registriertes Geraet: kein Warten, weiterhin
        genau ein direkter Retry."""
        seen = []
        sleeps = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.full_url)
            if len(seen) == 1:
                raise self._error400(b"{}")
            return self._response(self.INVENTORY_BODY)

        client = self._client("stale-hw-000")
        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
             patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                   return_value="eb22e4cf-bb01-4550-bff7-334446bb20b1"), \
             patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                   side_effect=AssertionError("register must not run")), \
             patch("calibre_plugin.tolino.time.sleep",
                   side_effect=sleeps.append):
            data = client.inventory()
        self.assertEqual([], data)
        self.assertEqual(2, len(seen))
        self.assertEqual([], sleeps)
        self.assertFalse(client._hw_registered_now)

class SharedWebreaderPartnerTests(unittest.TestCase):
    """Alle Shops mit dem gemeinsamen Web Reader verhalten sich wie
    Orell Fuessli – ein Profil statt pro Partner nachgebauter
    Einzelheiten.
    """

    SHARED = (1, 2, 4, 5, 6, 7, 8)

    def test_all_shops_use_the_shared_reader(self):
        self.assertEqual("https://webreader.mytolino.com/library/", WEBREADER_URL)
        self.assertEqual({1, 2, 4, 5, 6, 7, 8}, set(PARTNERS))

    def test_ebook_de_partner_matches_reference_config(self):
        partner = PARTNERS[8]
        self.assertEqual("eBook.de", partner["name"])
        self.assertEqual("81", partner["reseller_id"])
        self.assertEqual("ebookde0501html5readerV0001", partner["client_id"])
        self.assertEqual("e-publishing", partner["scope"])
        self.assertEqual("https://www.ebook.de/oauth/token",
                         partner["token_url"])

    def test_corrected_endpoints_match_reader_config(self):
        self.assertEqual("https://www.hugendubel.de/oauth/token",
                         PARTNERS[5]["token_url"])
        self.assertEqual("webreader", PARTNERS[7]["client_id"])
        self.assertEqual("SCOPE_BOSH", PARTNERS[7]["scope"])
        self.assertEqual("https://www.buecher.de/auth/oauth2/token",
                         PARTNERS[7]["token_url"])
        self.assertEqual("webreader", PARTNERS[2]["client_id"])
        self.assertEqual("https://www.thalia.at/auth/oauth2/token",
                         PARTNERS[2]["token_url"])

    def test_token_post_carries_reader_origin(self):
        """Der Token-POST sieht bei allen Shops aus wie der des Readers."""
        for pid in (1, 5, 8):
            captured = {}

            class Response:
                status = 200

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b'{"access_token":"a","expires_in":3600}'

            def request(request, timeout):
                captured.update(
                    {key.casefold(): value
                     for key, value in request.headers.items()})
                return Response()

            with patch("calibre_plugin.tolino.urlopen", request):
                TolinoClient(pid, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                             "refresh-token").login()
            self.assertEqual("https://webreader.mytolino.com",
                             captured.get("origin"), pid)
            self.assertEqual("https://webreader.mytolino.com/",
                             captured.get("referer"), pid)

    def test_authenticated_requests_send_webreader_client(self):
        for pid in (1, 5):
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
                captured.update(
                    {key.casefold(): value
                     for key, value in request.headers.items()})
                return Response()

            with patch("calibre_plugin.tolino.urlopen", request):
                client = TolinoClient(pid, "3xxA-00BCD-EFGHI-JKLMN-OPQRh",
                                      "refresh-token")
                client.access = "access-token"
                client.expires_at = time.time() + 60
                client._request(
                    "https://bosh.pageplace.de/bosh/rest/inventory/delta")
            self.assertEqual("TOLINO_WEBREADER",
                             captured.get("client_type"), pid)
            self.assertEqual("5.2.0", captured.get("client_version"), pid)

    def test_grab_live_refresh_reports_a_missing_chromium(self):
        """Shops auf dem gemeinsamen Reader: fehlt Chromium, wird der
        Fehler sichtbar statt still ein fremder Browser geoeffnet."""
        for pid in (1, 5, 8):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(pid, "test_hardware")
            self.assertIn("Kein Chromium-Browser gefunden",
                          str(ctx.exception))

class RequestRetryBudgetTests(unittest.TestCase):
    """Statt des Tri-States True/1/False in `_retry` hat jede der drei
    Rettungen einen eigenen, sichtbaren Zaehler (Neuanmeldung, Geraete-
    Recovery, WAF-Warteversuch)."""

    @staticmethod
    def _bot_error():
        from urllib.error import HTTPError
        error = HTTPError("https://example.invalid/token", 403, "Forbidden",
                          {}, None)
        error.read = lambda: (b"<!doctype html><title>Zugriff geblockt"
                              b"</title>")
        return error

    def test_waf_budget_counts_down_and_then_raises(self):
        from ..tolino import _WAF_RETRY_SECONDS, TolinoAuthError, TolinoClient
        sleeps = []

        def fake_urlopen(request, timeout=None):
            raise self._bot_error()

        with patch("calibre_plugin.tolino.urlopen",
                   side_effect=fake_urlopen), \
                patch("calibre_plugin.tolino.time.sleep",
                      side_effect=sleeps.append):
            client = TolinoClient(4, "hardware", "old-refresh")
            with self.assertRaises(TolinoAuthError):
                client.login()
        # Genau die zwei Warteversuche der Kaskade, dann der sichtbare Fehler.
        self.assertEqual(list(_WAF_RETRY_SECONDS), sleeps)

    def test_bosh_400_attempts_device_recovery(self):
        """Ein BOSH-400 versucht, die registrierte Geräte-ID zu übernehmen."""
        from urllib.error import HTTPError

        from ..tolino import TolinoApiError, TolinoClient
        error = HTTPError("https://bosh.pageplace.de/bosh/rest/x", 400,
                          "Bad Request", {}, None)
        error.read = lambda: b"{}"
        client = TolinoClient(4, "stale-hw", "refresh-token-value")
        client.access = "access-value"
        client.expires_at = time.time() + 3600

        def fake_urlopen(request, timeout=None):
            raise error

        with patch("calibre_plugin.tolino.urlopen", fake_urlopen), \
                patch("calibre_plugin.tolino.TolinoClient.fetch_hardware_id",
                      return_value="registered-hw"), \
                patch("calibre_plugin.tolino.TolinoClient._register_hardware",
                      return_value=False):
            with self.assertRaises(TolinoApiError):
                client._request("https://bosh.pageplace.de/bosh/rest/x")
        # Die Recovery lief trotz verbrauchtem Auth-Retry ...
        self.assertEqual("registered-hw", client.hardware)

class ErrorCodeTests(unittest.TestCase):
    """Fehlercodes: kurzer Nutzertext, Details nur im Log."""

    def test_short_text_hides_the_server_body_but_the_log_keeps_it(self):
        exc = tolino_module.TolinoApiError(
            "Tolino-Fehler (HTTP 500): <html>boom</html>", code="N03",
            short="Tolino meldet einen Fehler (HTTP 500).")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc)
        self.assertEqual("Tolino meldet einen Fehler (HTTP 500). (Code N03)",
                         shown)
        self.assertIn("boom", log.call_args[0][0])
        self.assertIn("Tolino-Fehler (HTTP 500)", str(exc))

    def test_secrets_never_reach_the_dialog_the_log_or_the_history(self):
        exc = tolino_module.TolinoAuthError(
            "Fehler mit geheimwert-123", code="A01",
            detail="auch hier geheimwert-123")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(
                exc, "Test", ("geheimwert-123",))
        blob = " ".join([shown, log.call_args[0][0]])
        self.assertNotIn("geheimwert-123", blob)

    def test_unexpected_errors_get_e99_and_a_traceback_in_the_log(self):
        with patch.object(tolino_module, "log_warning") as log:
            try:
                raise ValueError("kaputt")
            except ValueError as exc:
                shown = tolino_module.report_error(exc, "Test")
        self.assertIn("(Code E99)", shown)
        self.assertIn("kaputt", shown)
        logged = " ".join(call[0][0] for call in log.call_args_list)
        self.assertIn("Traceback", logged)

    def test_code_without_message_defaults_to_e99(self):
        self.assertEqual("E99", tolino_module.TolinoError("x").code)

    def test_error_codes_and_docs_stay_in_sync(self):
        """Genutzte Codes stehen in der Liste; die README verweist dorthin."""
        base = Path(__file__).resolve().parent.parent
        error_list = (base.parent / "docs" / "fehlercodes.md").read_text(
            encoding="utf-8")
        readme = (base.parent / "README.md").read_text(encoding="utf-8")
        used = set()
        for path in base.rglob("*.py"):
            if "/tests/" in str(path):
                continue
            used |= set(re.findall(r'code\s*=\s*"([A-Z][0-9]{2})"',
                                   path.read_text(encoding="utf-8")))
        used.discard("E99")
        documented = set(re.findall(r"^\| ([A-Z][0-9]{2}) \|", error_list,
                                    re.MULTILINE))
        documented.discard("E99")
        self.assertTrue(used)
        self.assertEqual(documented, used)
        for code in sorted(used):
            self.assertIn("| %s |" % code, error_list, code)
        self.assertIn("| E99 |", error_list)
        self.assertIn("docs/fehlercodes.md", readme)
        self.assertNotIn("| A02 |", readme)

class UserTextTests(unittest.TestCase):
    """Einheitliche, kurze Nutzertexte."""

    WORDS = ("häkchen", "anhaken", "angehakt", "deliverable", "von hand zuordnen")

    def _visible_strings(self, path):
        """Alle String-Konstanten ohne Docstrings (also die sichtbaren Texte)."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(
                        getattr(body[0], "value", None), ast.Constant):
                    docstrings.add(id(body[0].value))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings):
                yield node.value

    def test_reported_message_is_one_clean_line(self):
        """Genau «Text (Code A01)» – ohne Zeilenumbruch und ohne Traceback."""
        exc = tolino_module.TolinoAuthError(
            "Die Tolino-Sitzung ist beendet. " + tolino_module.RELOGIN,
            code="A01", detail="HTTP 400: Token is not active")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc, "Vorbereitung")
        self.assertEqual(
            "Die Tolino-Sitzung ist beendet. Bitte bei tolino anmelden. "
            "(Code A01)", shown)
        logged = log.call_args[0][0]
        self.assertIn("A01", logged)
        self.assertIn("Token is not active", logged)
        self.assertNotIn("Token is not active", shown)
        self.assertNotIn("Traceback", shown)

    def test_auth_errors_keep_their_traceback_chain_in_the_log(self):
        """A01 und Co.: Dialog eine Zeile, die Kette samt Werten nur im Log."""
        secret = "geheimwert-77"
        try:
            try:
                raise tolino_module.TolinoApiError("HTTP 400: %s" % secret,
                                                  code="N03")
            except tolino_module.TolinoApiError as inner:
                raise tolino_module.TolinoAuthError(
                    "Die Tolino-Sitzung ist beendet. " + tolino_module.RELOGIN,
                    code="A01", detail="HTTP 400") from inner
        except tolino_module.TolinoAuthError as exc:
            with patch.object(tolino_module, "log_warning") as log:
                shown = tolino_module.report_error(
                    exc, "Vorbereitung", (secret,))
        calls = [call[0][0] for call in log.call_args_list]
        self.assertEqual(1, shown.count("\n") + 1)
        self.assertTrue(shown.endswith("(Code A01)"))
        self.assertTrue(any(c.startswith("Tolino-Details") for c in calls), calls)
        blob = " ".join(calls)
        self.assertIn("Traceback", blob)
        self.assertIn("direct cause", blob)
        self.assertNotIn(secret, blob)
        self.assertNotIn(secret, shown)

    def test_report_error_without_a_traceback_logs_a_single_line(self):
        """Ohne Traceback (z. B. selbst gebaute Meldung) bleibt es eine Zeile."""
        exc = tolino_module.TolinoError("Fehler ohne Traceback", code="E99")
        with patch.object(tolino_module, "log_warning") as log:
            shown = tolino_module.report_error(exc)
        self.assertEqual(1, log.call_count)
        self.assertEqual("Fehler ohne Traceback (Code E99)", shown)


class DownloadMetadataTests(unittest.TestCase):
    """Der Speichern-Dialog braucht keine Metadaten."""

    class Response:
        @staticmethod
        def read():
            return b"EPUB-DATA"

        @staticmethod
        def geturl():
            return "https://example.invalid/book"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    @staticmethod
    def _client():
        from ..tolino import TolinoClient
        client = TolinoClient(4, "hw", "refresh-token")
        client.access = "access"
        client._request = (lambda *args, **kwargs:
                           {"DownloadInfo": {"contentUrl":
                                             "https://example.invalid/book"}})
        return client

    def test_upload_handles_null_metadata_response_as_missing_id(self):
        client = self._client()
        client._multipart = lambda *_args, **_kwargs: (b"body", "multipart/form-data")
        client._request = lambda *_args, **_kwargs: {"metadata": None}
        from ..tolino import TolinoApiError
        with tempfile.NamedTemporaryFile(suffix=".epub") as source:
            with self.assertRaises(TolinoApiError) as caught:
                client.upload(source.name)
        self.assertEqual("N02", caught.exception.code)

    def test_download_returns_content_without_an_inventory_request(self):
        calls = []
        client = self._client()
        client.inventory = lambda: calls.append(1) or []
        with patch("calibre_plugin.tolino.urlopen",
                   return_value=self.Response()):
            content = client.download("d-1")
        self.assertEqual(b"EPUB-DATA", content)
        self.assertEqual([], calls)

    def test_download_does_not_fetch_inventory(self):
        client = self._client()
        client.inventory = lambda: (_ for _ in ()).throw(
            AssertionError("download must not load inventory"))
        with patch("calibre_plugin.tolino.urlopen",
                   return_value=self.Response()):
            content = client.download("d-1")
        self.assertEqual(b"EPUB-DATA", content)

class MultipartMemoryTests(unittest.TestCase):
    """Peak memory of the in-memory multipart body for a large EPUB.

    The transport is stdlib-only (urllib) and sends one contiguous body. A
    file is therefore held about twice (file bytes + joined body) -- this
    test pins that bound so a future change cannot silently hold several
    copies, and checks that the large payload is still encoded correctly.
    """

    def test_large_upload_body_is_correct_and_bounded(self):
        import tracemalloc
        from ..tolino import TolinoClient
        size = 20 * 1024 * 1024
        with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
            tmp.write(b"EPUB" * (size // 4))
            path = tmp.name
        try:
            tracemalloc.start()
            with open(path, "rb") as source:
                payload = source.read()
            body, content_type = TolinoClient._multipart(
                {"deliverableId": "abc"}, payload, path)
            _current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        finally:
            os.unlink(path)
        self.assertTrue(content_type.startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="deliverableId"', body)
        self.assertIn(b'filename="', body)
        self.assertIn(b"Content-Type: application/epub+zip", body)
        self.assertTrue(body.endswith(b"--\r\n"))
        self.assertGreater(len(body), size)
        # ~2x the file (bytes + joined body), never a multiple of it.
        self.assertLess(peak, size * 3)

class SyncDataResponseShapeTests(unittest.TestCase):
    """Die sync-data-Antwort des Servers trägt die Marken-ID im Pfad.

    Gelesen-Marken und Sammlungen kommen als
    ``/publications/<Buch-ID>/tags/<Marken-ID>`` zurück – die Marken-ID
    vergibt der Server. Der Plugin schrieb nur die Form ohne Marken-ID und
    verlangte beim Lesen genau vier Pfadteile: damit war jede Server-Marke
    unsichtbar. Folge: die Spalte «Gelesen» blieb leer, obwohl die Cloud die
    Bücher als gelesen führte, und jede Serie schien bei jedem Lauf zu
    fehlen.
    """

    PUB = "bosh_8_543897185873248682836029182"
    READ = {"op": "replace",
            "path": "/publications/%s/tags/6438286" % PUB,
            "value": {"revision": "1-abc", "modified": 17,
                      "name": "collection_finished_readings_name",
                      "category": "system"}}
    SERIES = {"op": "replace",
              "path": "/publications/%s/tags/6438287" % PUB,
              "value": {"revision": "1-def", "modified": 18,
                        "name": "Brunetti", "category": "collection"}}
    OWN_ADD = {"op": "add", "path": "/publications/%s/tags" % PUB,
               "value": {"modified": 19, "name": "SciFi",
                         "category": "collection"}}

    def _client(self, patches, revision="42"):
        """Client mit echter Antwortform als Lesegrundlage."""
        client = TolinoClient(4, "hardware", "refresh-token")
        calls = []

        def fake_request(url, method="GET", data=None, form=False,
                         authenticated=True, content_type=None):
            calls.append({"url": url, "method": method, "data": data})
            return {"revision": revision, "patches": list(patches)}

        client._request = fake_request
        return client, calls

    def test_read_ids_understands_the_server_path_with_tag_id(self):
        client, calls = self._client([self.READ, self.OWN_ADD])
        self.assertEqual({self.PUB}, client.read_ids())
        self.assertEqual(1, len(calls))

    def test_collection_pairs_understands_the_server_path_with_tag_id(self):
        client, _ = self._client([self.SERIES, self.OWN_ADD])
        self.assertEqual({(self.PUB, "Brunetti"), (self.PUB, "SciFi")},
                         client.collection_pairs())

    def test_read_state_and_collections_come_from_one_read(self):
        client, calls = self._client([self.READ, self.SERIES])
        self.assertEqual({self.PUB}, client.read_ids())
        self.assertEqual({(self.PUB, "Brunetti")}, client.collection_pairs())
        self.assertEqual(1, len(calls))

    def test_remove_with_tag_id_takes_the_book_out_again(self):
        removed = {"op": "remove",
                   "path": "/publications/%s/tags/6438286" % self.PUB,
                   "value": {"revision": "1-abc",
                             "name": "collection_finished_readings_name",
                             "category": "system"}}
        client, _ = self._client([self.READ, removed])
        self.assertEqual(set(), client.read_ids())

    def test_foreign_paths_and_malformed_patches_are_ignored(self):
        patches = [
            {"op": "replace", "path": "/publications/%s/bookmark/12" % self.PUB,
             "value": {"progress": 0.4}},
            {"op": "replace", "path": "/publications//tags/1",
             "value": {"name": "collection_finished_readings_name",
                       "category": "system"}},
            {"op": "replace", "path": "/publications/%s/tags/1" % self.PUB},
            "kein dict",
        ]
        client, _ = self._client(patches)
        self.assertEqual(set(), client.read_ids())
        self.assertEqual(set(), client.collection_pairs())

    def test_mark_read_many_skips_books_the_server_already_calls_read(self):
        """Die Server-Form wird erkannt: eine vorhandene Marke wird nicht
        noch einmal geschrieben."""
        client, calls = self._client([self.READ], revision="43")
        self.assertEqual("43", client.mark_read_many([self.PUB]))
        self.assertEqual(1, len(calls))

    def test_mark_read_uses_the_revision_of_a_real_response(self):
        client, calls = self._client([self.SERIES], revision="77")
        client.mark_read_many(["neues-buch"])
        self.assertEqual(2, len(calls))
        self.assertEqual("77", calls[1]["data"]["revision"])
        self.assertEqual("/publications/neues-buch/tags",
                         calls[1]["data"]["patches"][0]["path"])

    def test_cloud_read_state_reaches_the_comparison_rows(self):
        """Von der Server-Antwort bis zur angehakten Spalte – der Fall aus
        dem Feldbericht."""
        client, _ = self._client([self.READ])
        book = {"uuid": "u1", "title": "Feuerprobe", "authors": "Donna Leon",
                "isbn": "", "formats": ["EPUB"], "last_modified": "1",
                "format_signatures": {"EPUB": "1:1"}}
        inventory = [{"deliverableId": self.PUB,
                      "epubMetaData": {"title": "Feuerprobe",
                                       "author": [{"name": "Donna Leon"}]}}]
        rows = compare_inventory({1: book}, {"u1": {"tolino_id": self.PUB}},
                                 inventory, ["EPUB"],
                                 read_ids=client.read_ids())
        row = rows[0]
        self.assertTrue(row["read"].cloud)
        self.assertTrue(row["read"])
        self.assertEqual(self.PUB, row["tolino_id"])

class TagStateReducerTests(unittest.TestCase):
    """Add/Remove-Patches werden als Verlauf ausgewertet (P1/D)."""

    BOOK = "book-1"

    @staticmethod
    def _patch(op, name, category="system", tag_id="1", book="book-1"):
        path = "/publications/%s/tags" % book
        if tag_id is not None:
            path += "/%s" % tag_id
        return {"op": op, "path": path,
                "value": {"revision": "r-%s" % op, "name": name,
                          "category": category}}

    def test_add_remove_leaves_no_tag(self):
        patches = [self._patch("add", "gelesen"),
                   self._patch("remove", "gelesen")]
        self.assertIsNone(active_tags(
            patches, "system", "gelesen").get((self.BOOK, "gelesen")))

    def test_add_remove_add_leaves_an_active_tag(self):
        patches = [self._patch("add", "gelesen"),
                   self._patch("remove", "gelesen"),
                   self._patch("add", "gelesen", tag_id="2")]
        active = active_tags(
            patches, "system", "gelesen").get((self.BOOK, "gelesen"))
        self.assertIsNotNone(active)
        self.assertEqual("2", tag_id_from_patch(active))

    def test_other_books_and_foreign_tags_stay_untouched(self):
        patches = [
            self._patch("add", "gelesen", book="book-2"),
            self._patch("add", "SciFi", category="collection"),
            self._patch("add", "gelesen"),
            self._patch("remove", "gelesen", book="book-2"),
        ]
        active = active_tags(
            patches, "system", "gelesen").get((self.BOOK, "gelesen"))
        self.assertIsNotNone(active)
        pairs = active_tags(patches, "collection")
        self.assertEqual({("book-1", "SciFi")}, set(pairs))

    def test_mark_read_does_not_add_an_already_active_tag(self):
        client = TolinoClient(4, "hardware", "refresh-token")
        patches = [self._patch("add", READ_TAG),
                   self._patch("remove", READ_TAG),
                   self._patch("add", READ_TAG, tag_id="3")]
        client.sync_snapshot = lambda: ("rev", patches)
        calls = []
        client._sync_patch = lambda payload: calls.append(payload) or {"revision": "r"}
        client.mark_read_many([self.BOOK])
        self.assertEqual([], calls)  # effektiv gelesen: kein Schreibvorgang

    def test_unmark_removes_only_the_active_tag_id(self):
        client = TolinoClient(4, "hardware", "refresh-token")
        patches = [self._patch("add", READ_TAG, tag_id="1"),
                   self._patch("remove", READ_TAG, tag_id="1"),
                   self._patch("add", READ_TAG, tag_id="7")]
        client.sync_snapshot = lambda: ("rev", patches)
        calls = []
        client._sync_patch = lambda payload: calls.append(payload) or {"revision": "r"}
        client.mark_read_many([self.BOOK], finished=False)
        self.assertEqual(1, len(calls))
        patch = calls[0]["patches"][0]
        self.assertEqual("remove", patch["op"])
        self.assertEqual("/publications/%s/tags/7" % self.BOOK, patch["path"])

class PersistCredentialsDedupTests(unittest.TestCase):
    """Token und Hardware-ID werden mit config.save_credentials gespeichert."""

    @staticmethod
    def _ui_module():
        return ui_base_tests.ToolbarIconTests._load_ui_module(
            lambda *args, **kwargs: None,
            qt_stub={"pyqtSignal":
                     worker_base_tests.CollectionReconcileWorkerTests._signal_descriptor()[0]})

    def test_save_credentials_writes_token_and_hardware(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.save_credentials("konto", "tok-1", "hw-1")
            account = config.account_by_name("konto")
            self.assertEqual("tok-1", account["refresh_token"])
            self.assertEqual("hw-1", account["hardware_id"])
            # Der Keep-alive rotiert alle Konten: das aktive bleibt stehen.
            self.assertEqual("default", config.settings()["active_account"])
        finally:
            config.PREFERENCES = original

    def test_the_store_writes_the_snapshot_and_persists_at_once(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = {"refresh_token": "alt", "hardware_id": "hw-alt"}
            store = config.CredentialStore(values, "konto")
            store.save_refresh("neu")
            self.assertEqual("neu", values["refresh_token"])
            account = config.account_by_name("konto")
            self.assertEqual("neu", account["refresh_token"])
            # Die Hardware-ID bleibt erhalten, wenn nur der Token rotiert.
            self.assertEqual("hw-alt", account["hardware_id"])
        finally:
            config.PREFERENCES = original

    def test_the_store_takes_only_non_empty_stored_values(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            values = {"refresh_token": "alt", "hardware_id": "hw-alt"}
            store = config.CredentialStore(values, "konto")
            # Nichts gespeichert: der Snapshot bleibt unangetastet.
            store.freshest()
            self.assertEqual("alt", values["refresh_token"])
            config.save_credentials("konto", "gespeichert", "hw-neu")
            self.assertEqual("gespeichert", store.freshest()["refresh_token"])
            self.assertEqual("hw-neu", values["hardware_id"])
        finally:
            config.PREFERENCES = original

    def test_save_credentials_without_a_name_does_nothing(self):
        original = config.PREFERENCES
        try:
            config.PREFERENCES = config.JSONConfig("test")
            config.save_credentials("", "tok", "hw")
            active = config.settings()["active_account"]
            self.assertEqual("", config.account_by_name(active)["refresh_token"])
        finally:
            config.PREFERENCES = original

    def test_sync_worker_persists_through_the_shared_helper(self):
        ui_module = self._ui_module()
        saved = []
        settings = {"account_name": "konto", "hardware_id": "hw-alt",
                    "refresh_token": "tok-alt"}
        try:
            with patch("calibre_plugin.config.save_credentials",
                       lambda *args: saved.append(args)):
                worker = ui_module.workers.SyncWorker(
                    RunContext.from_values(settings), [], [],
                    state=settings.get("state"))
                worker.store.save_refresh("tok-neu")
                worker.store.save_hardware("hw-neu")
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("konto", "tok-neu", "hw-alt"),
                          ("konto", "tok-neu", "hw-neu")], saved)

    def test_dashboard_stores_a_manually_entered_token(self):
        ui_module = self._ui_module()

        class Field:
            def __init__(self, value=""):
                self.value = value

            def text(self):
                return self.value

            def setText(self, value):
                self.value = value

        saved = []
        dashboard = type("Dashboard", (), {})()
        dashboard.account_name = "konto"
        # Das Feld ist leer: der Token kommt ins Konto, nicht in die UI.
        dashboard.refresh = Field("")
        dashboard.hardware = Field("hw-alt")
        dashboard.update_status = lambda: None
        try:
            with patch("calibre_plugin.ui_dashboard.save_credentials",
                       lambda *args: saved.append(args)):
                ui_module.dashboard.SyncDashboard.store_refresh(
                    dashboard, "tok-neu")
        finally:
            ui_module._restore_stubs_for_tests()
        self.assertEqual([("konto", "tok-neu", "hw-alt")], saved)

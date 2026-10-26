"""Browser-Anmeldung, DevTools und Flatpak."""

import base64
import calibre_plugin.cdp as cdp_module
from .. import browser_finder
import json
import os
import shutil
import tempfile
import time
import unittest
from . import ui_base_tests
from ..tolino import TolinoAuthError
from ..browser_login import browser_login
from ._support import (
    _REAL_CLOSE_WINDOW, _full, start_cdp_stubs, stop_cdp_stubs)
from unittest.mock import patch


# Modul-Hooks für den Einzellauf; die Gesamtsuite ruft sie zentral auf.
setUpModule = start_cdp_stubs
tearDownModule = stop_cdp_stubs


def _read_exact(conn, size):
    """Genau `size` Bytes von `conn` lesen (oder bis EOF)."""
    data = b""
    while len(data) < size:
        chunk = conn.recv(size - len(data))
        if not chunk:
            break
        data += chunk
    return data


def _jwt_token(iat, sid=None):
    """Minimales, datierbares Refresh-JWT (Kopf, Nutzlast, Signatur)."""
    head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
    payload = {"iat": int(iat), "typ": "Refresh"}
    if sid:
        payload["sid"] = sid
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8"))
    return "%s.%s.sig" % (head.decode().rstrip("="),
                          body.decode().rstrip("="))


class _SidJwtTest(unittest.TestCase):
    """Refresh-JWT mit fester Session-ID je Testklasse."""

    SID = ""

    def _refresh_jwt(self, iat):
        return _jwt_token(iat, self.SID)


class LiveGrabTests(unittest.TestCase):
    """Der Live-Grab (CDP) ist der Primaerpfad der Browser-Anmeldung."""

    def setUp(self):
        self._launched = dict(cdp_module._LAUNCHED)
        self.addCleanup(cdp_module._LAUNCHED.update, self._launched)
        cdp_module._LAUNCHED.update({"process": None, "port": None,
                                     "profile": ""})

    def _jwt(self, iat, sid=None):
        return _jwt_token(iat, sid)

    def test_chromium_candidates_detects_flatpak_installs(self):
        """Flatpak-Browser werden als `flatpak run`-Launcher erkannt."""
        import tempfile
        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as home:
            for app_id, _command, _label, _host_label in browser_finder._FLATPAK_BROWSER_APPS:
                os.makedirs(os.path.join(home, ".var", "app", app_id))
            fake_flatpak = os.path.join(home, "bin", "flatpak")
            os.makedirs(os.path.dirname(fake_flatpak))
            with open(fake_flatpak, "w") as handle:
                handle.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake_flatpak, 0o755)
            with _patch("shutil.which",
                        side_effect=lambda name: fake_flatpak
                        if name == "flatpak" else None):
                candidates = browser_finder.chromium_candidates(home=home)
        labels = [label for _argv, label in candidates]
        candidates_by_label = dict((label, argv) for argv, label in candidates)
        for app_id, command, label, _host_label in browser_finder._FLATPAK_BROWSER_APPS:
            self.assertIn(label, labels)
            self.assertEqual(["run", "--command=" + command, app_id],
                             candidates_by_label[label][1:])
            self.assertTrue(candidates_by_label[label][0].endswith("flatpak"))

    def test_chromium_candidates_skips_flatpak_without_launcher(self):
        """Ohne `flatpak`-Binary im PATH gibt es keinen Flatpak-Kandidaten."""
        import tempfile
        from unittest.mock import patch as _patch

        with tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, ".var", "app", "com.brave.Browser"))
            with _patch("shutil.which", return_value=None), \
                 _patch.object(browser_finder, "_EXTRA_BROWSER_PATHS", ()):
                candidates = browser_finder.chromium_candidates(home=home)
        self.assertEqual([], candidates)

    def test_launch_reader_window_appends_flags_to_argv_prefix(self):
        """Der Launcher-Prefix (z. B. flatpak run) wird vor die Flags
        gesetzt, nicht damit verschmolzen."""
        import tempfile
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                spawned["args"] = list(args)

        with tempfile.TemporaryDirectory():
            prefix = ["/usr/bin/flatpak", "run", "--command=brave",
                      "com.brave.Browser"]
            with _patch.object(cdp_module, "pick_chromium",
                               return_value=(prefix, "Brave (Flatpak)")), \
                 _patch.object(cdp_module.subprocess, "Popen", FakePopen), \
                 _patch.object(cdp_module, "_http_get_json",
                               return_value={"webSocketDebuggerUrl":
                                             "ws://127.0.0.1:%d/devtools/browser/x" % 9441}), \
                 _patch.object(cdp_module.time, "sleep"):
                port = cdp_module.launch_reader_window(4)
        args = spawned["args"]
        self.assertEqual(prefix, args[:4])
        self.assertIn("--remote-debugging-port=%d" % port, args)
        self.assertTrue(any(a.startswith("--user-data-dir=") for a in args))
        self.assertIn("mytolino.com", args[-1])

    def test_filter_live_candidates_drops_stale_jwts(self):
        """JWT-Kandidaten aelter als 2 Minuten werden verworfen."""
        from ..token_candidates import _filter_live_candidates
        now = time.time()
        fresh = self._jwt(now - 10, "sess-1")
        stale = self._jwt(now - 3600, "sess-1")  # 1 h: ueber dem 30-min-Cutoff
        undated = "plainopaquevalue-not-a-jwt"
        kept = _filter_live_candidates([fresh, stale, undated])
        self.assertEqual([fresh, undated], kept)

    def test_grab_live_refresh_exchanges_freshest_token_once(self):
        """Der frischeste Live-Kandidat wird GENAU EINMAL getauscht."""
        from ..browser_login import grab_live_refresh

        now = time.time()
        fresh = self._jwt(now - 5, "sess-live")
        stale = self._jwt(now - 30, "sess-live")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        grabbed = {"refresh": [stale, fresh], "hardware": ["hw-live"]}
        with patch("calibre_plugin.browser_login.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value=grabbed), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer:
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-token", refresh)
        self.assertEqual("fake-hw", hardware)
        self.assertEqual([fresh], logins)
        # Nach dem Tausch gehört die Sitzung Calibre: das Anmeldefenster
        # wird geschlossen, damit der Reader die übernommene Kopie nicht
        # weiter rotiert.
        closer.assert_called_once_with()

    def test_grab_live_refresh_rejects_only_stale_candidates(self):
        """Nur veraltete Kandidaten scheitern klar, ohne Replay."""
        from ..browser_login import grab_live_refresh
        stale = self._jwt(time.time() - 7200, "sess-old")  # 2 h alt
        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [stale], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer, \
             patch("calibre_plugin.cdp.grabber_window_state",
                   return_value={"window": False, "reader_tab": False}), \
             patch("calibre_plugin.browser_login.time.monotonic",
                   side_effect=[0, 2]):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("B02", ctx.exception.code)
        closer.assert_called_once_with()
        self.assertIn("gültiger Zugang", str(ctx.exception))
        self.assertNotIn(stale, _full(ctx.exception))

    def test_grab_live_refresh_rejection_recommends_fresh_grab(self):
        """Wird der Live-Token abgelehnt, wird KEIN zweiter Kandidat
        replayt – der Fehler verweist auf einen erneuten Grab."""
        from ..browser_login import grab_live_refresh
        logins = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError(
                    'Tolino HTTP 400: {"error": "invalid_grant"}')

        fresh = self._jwt(time.time() - 5, "sess-live")
        other = self._jwt(time.time() - 8, "sess-other")
        with patch("calibre_plugin.browser_login.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [fresh, other],
                                 "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        message = str(ctx.exception)
        self.assertIn("bei tolino anmelden", message)
        # Genau ein Login-Versuch: der zweite Kandidat wird bewusst
        # NICHT mehr probiert (kein Replay-Feuerwerk).
        self.assertEqual([fresh], logins)

    def test_grab_live_refresh_never_retries_a_rejected_candidate(self):
        """Ein am Endpunkt abgelehnter Kandidat wird nie wieder probiert
        (Keycloak-Reuse-Schutz) – auch nicht, wenn der Seiten-Speicher
        dieselbe Kopie erneut liefert. Statt einer erzwungenen Rotation
        wartet das Plugin auf eine NEU geschriebene Kopie; kommt keine,
        endet es mit einer klaren Fehlermeldung.
        """
        from ..browser_login import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")
        logins = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.browser_login.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [spent], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual([spent], logins)
        self.assertIn("Tolino hat den Zugang", ctx.exception.detail)

    def test_ws_recv_json_parses_rfc6455_header_correctly(self):
        """Der minimale WS-Client liest den Länge+MASK-Header korrekt."""
        import socket as socket_module
        import threading
        from ..cdp import _Ws

        server_ready = threading.Event()
        bound = {}

        def server():
            listener = socket_module.socket()
            listener.setsockopt(socket_module.SOL_SOCKET,
                                socket_module.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            bound["port"] = listener.getsockname()[1]
            server_ready.set()
            conn, _addr = listener.accept()
            # Handshake minimal beantworten (Key egal, Client prueft nur 101)
            conn.recv(4096)
            conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\n"
                         b"Connection: Upgrade\r\n"
                         b"Sec-WebSocket-Accept: x\r\n\r\n")
            # Masked client frame empfangen
            header = _read_exact(conn, 2)
            assert (header[0] & 0x0F) == 0x1
            length = header[1] & 0x7F
            assert header[1] & 0x80, "client frames must be masked"
            mask = _read_exact(conn, 4)
            payload = _read_exact(conn, length)
            unmasked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            # Unmaskierte JSON-Antwort als UNMASKED Server-Frame zurueck
            # (auf > 125 Byte gepolstert, damit der 126er-Extended-
            # Length-Pfad mitgetestet wird)
            value = unmasked.decode() + "|" + "p" * 60
            reply = json.dumps({"id": 1, "result": {"value": value}}).encode()
            assert len(reply) > 125, "reply too short: %d" % len(reply)
            # Server-Frame korrekt kodieren: ab 126 Byte Extended Length.
            import struct as struct_module
            frame = bytearray([0x81, 126])
            frame += struct_module.pack(">H", len(reply))
            conn.sendall(bytes(frame) + reply)
            conn.close()
            listener.close()

        thread = threading.Thread(target=server, daemon=True)
        thread.start()
        server_ready.wait(5)

        # Der Server-Thread meldet seinen gebundenen Port zurueck.
        port = bound["port"]

        ws = _Ws.connect("127.0.0.1", port, "/devtools/page/test", timeout=5)
        try:
            ws.send_json({"id": 1, "method": "Runtime.evaluate",
                          "params": {"expression": "1+1"}})
            message = ws.recv_json(timeout=5)
        finally:
            ws.close()
        self.assertEqual(1, message.get("id"))
        # Value ist das vom Server korrekt entmaskierte Echo des Requests:
        self.assertTrue(message["result"]["value"].startswith(
            '{"id": 1, "method": "Runtime.evaluate"'))

    def test_filter_live_cutoff_accepts_tokens_from_older_signin(self):
        """Tokens bis 30 Minuten Alter ueberleben den Live-Cutoff."""
        from ..token_candidates import _filter_live_candidates
        now = time.time()
        ten_minutes_old = self._jwt(now - 600, "sess-1")
        kept = _filter_live_candidates([ten_minutes_old])
        self.assertEqual([ten_minutes_old], kept)
        # Ausdruecklich mit dem Default aufgerufen bleibt es bei 1800s:
        self.assertIn("1800",
                      _filter_live_candidates.__defaults__[0].__str__())

    def test_exchange_prefers_refresh_typ_over_newer_access_token(self):
        """Access-JWTs (kurzlebig) werden nicht vor dem Refresh-JWT
        verbraten, auch wenn ihr iat neuer ist."""
        from ..browser_login import _exchange_grabbed_token
        now = time.time()
        access = self._jwt(now - 2, "sess-live")
        # Access-Variante: gleicher Aufbau, aber typ nicht "Refresh"
        head = base64.urlsafe_b64encode(b'{"alg":"HS512","typ":"JWT"}')
        body = base64.urlsafe_b64encode(json.dumps(
            {"iat": int(now - 2), "typ": "Bearer"}).encode())
        access = "%s.%s.sig" % (head.decode().rstrip("="),
                                body.decode().rstrip("="))
        refresh = self._jwt(now - 30, "sess-live")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated"

        grabbed = {"refresh": [access, refresh], "hardware": []}
        with patch("calibre_plugin.browser_login.TolinoClient", FakeClient):
            result = _exchange_grabbed_token(4, "hw", grabbed,
                                             [access, refresh])
        self.assertEqual([refresh], logins)
        self.assertEqual("rotated", result[0])

    def test_grab_live_refresh_picks_up_token_written_during_wait(self):
        """Leerer erster Grab => das Plugin wartet, bis der Reader selbst
        einen Token schreibt, und tauscht diesen -- ohne erzwungene
        Rotation, ohne Netzwerk-Interception (beides entfernt)."""
        from ..browser_login import grab_live_refresh
        later = self._jwt(time.time() - 1, "sess-late")

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated-after-wait"

        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [], "hardware": [],
                                 "idb": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [later],
                                 "hardware": ["hw-live"], "idb": []}), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0), \
             patch("calibre_plugin.browser_login.TolinoClient", FakeClient):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-after-wait", refresh)
        self.assertEqual("hw-live", hardware)

    def test_exchange_refresh_in_browser_parses_page_reply(self):
        """In-page-fetch-Antwort (status/body) wird korrekt geparst."""
        from .. import cdp as cdp_module
        page_reply = json.dumps({
            "status": 200,
            "body": json.dumps({"access_token": "a", "refresh_token": "r"}),
        })
        with patch("calibre_plugin.cdp._verified_owner",
                   return_value={"port": 9223}), \
             patch("calibre_plugin.cdp._evaluate_raw_in_target",
                   return_value={"ok": True, "value": page_reply,
                                 "exception": None}):
            status, body = cdp_module.exchange_refresh_in_browser(
                "ws://127.0.0.1:9223/devtools/page/x",
                "https://www.orellfuessli.ch/auth/oauth2/token",
                "client_id=webreader&grant_type=refresh_token")
        self.assertEqual(200, status)
        self.assertIn("refresh_token", body)

    def test_exchange_refresh_in_browser_reports_page_failure(self):
        """Ein fehlgeschlagener Page-Evaluate gibt status=0 zurueck."""
        from .. import cdp as cdp_module
        with patch("calibre_plugin.cdp._verified_owner",
                   return_value={"port": 9223}), \
             patch("calibre_plugin.cdp._evaluate_raw_in_target",
                   return_value={"ok": False, "value": None,
                                 "exception": "page blew up"}):
            status, body = cdp_module.exchange_refresh_in_browser(
                "ws://127.0.0.1:9223/devtools/page/x", "https://x/token",
                "a=b")
        self.assertEqual(0, status)
        self.assertIn("page blew up", body)

    def test_exchange_grabbed_token_prefers_browser_exchange(self):
        """200er Browser-Tausch schlaegt den Plugin-POST – kein WAF-403."""
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")
        logins = []

        def fake_browser_exchange(ws_url, token_url, form_body, timeout=25):
            self.assertIn("grant_type=refresh_token", form_body)
            return 200, json.dumps({
                "access_token": "a", "refresh_token": "rotated-browser"})

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   side_effect=fake_browser_exchange), \
             patch("calibre_plugin.browser_login.TolinoClient") as client_cls:
            client_cls.return_value.refresh = fresh
            client_cls.return_value.hardware = "hw-grab"
            client_cls.return_value._apply_token_response.side_effect = \
                lambda data: logins.append(data) or setattr(
                    client_cls.return_value, "refresh",
                    data["refresh_token"])
            refresh, hardware = _exchange_grabbed_token(
                4, "hw-grab", {"refresh": [fresh], "hardware": ["hw-grab"]},
                [fresh])
        self.assertEqual("rotated-browser", refresh)
        self.assertEqual("hw-grab", hardware)
        # Der Plugin-Client wurde NIE _login (Plugin-POST) aufgerufen:
        self.assertEqual(1, len(logins))

    def test_exchange_grabbed_token_falls_back_to_plugin_post(self):
        """Eindeutige WAF-403 im Browser -> Plugin-POST."""
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = fresh
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated-plugin"

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   return_value=(403, "Zugriff geblockt")), \
             patch("calibre_plugin.browser_login.TolinoClient", FakeClient):
            refresh, hardware = _exchange_grabbed_token(
                4, "hw", {"refresh": [fresh], "hardware": []}, [fresh])
        self.assertEqual("rotated-plugin", refresh)

    def test_browser_exchange_ambiguous_failure_never_replays_refresh_grant(self):
        """Unklarer fetch-Fehler kann nach Token-Verbrauch auftreten;
        derselbe Single-Use-Grant darf dann nicht erneut gesendet werden."""
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")

        class MustNotLogin:
            def __init__(self, *_args):
                self.refresh = fresh
                self.hardware = "hw"

            def _login(self):
                raise AssertionError("ambiguous grant must not be replayed")

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   return_value=(0, "fetch failed: connection reset")), \
             patch("calibre_plugin.browser_login.TolinoClient", MustNotLogin):
            with self.assertRaises(TolinoAuthError) as ctx:
                _exchange_grabbed_token(
                    4, "hw", {"refresh": [fresh], "hardware": []}, [fresh])
        self.assertEqual("A06", ctx.exception.code)
        self.assertIn("nicht sicher geprüft", str(ctx.exception))

    def test_browser_exchange_non_waf_http_error_never_replays_refresh_grant(self):
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")

        class MustNotLogin:
            def __init__(self, *_args):
                raise AssertionError("non-WAF response must not retry the grant")

        for status, body in ((400, "Token expired"),
                             (500, "Internal Server Error"),
                             (403, "Authorization failed")):
            with self.subTest(status=status), \
                 patch("calibre_plugin.cdp.reader_ws_url",
                       return_value="ws://127.0.0.1:9223/devtools/page/x"), \
                 patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                       return_value=(status, body)), \
                 patch("calibre_plugin.browser_login.TolinoClient", MustNotLogin):
                with self.assertRaises(TolinoAuthError):
                    _exchange_grabbed_token(
                        4, "hw", {"refresh": [fresh], "hardware": []}, [fresh])

    def test_browser_exchange_waf_response_still_allows_transport_fallback(self):
        """Nur eine eindeutig erkannte WAF-403 darf den Fallback auslösen."""
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")

        class FallbackClient:
            def __init__(self, *_args):
                self.refresh = fresh
                self.hardware = "hw"

            def _login(self):
                self.refresh = "rotated-plugin"

        with patch("calibre_plugin.cdp.reader_ws_url",
                   return_value="ws://127.0.0.1:9223/devtools/page/x"), \
             patch("calibre_plugin.cdp.exchange_refresh_in_browser",
                   return_value=(403, "Access denied by CloudFront")), \
             patch("calibre_plugin.browser_login.TolinoClient", FallbackClient):
            refresh, _hardware = _exchange_grabbed_token(
                4, "hw", {"refresh": [fresh], "hardware": []}, [fresh])
        self.assertEqual("rotated-plugin", refresh)

    def test_grab_live_refresh_adopts_new_candidate_and_reader_hardware(self):
        """Nach einer Ablehnung uebernimmt das Plugin die NEU geschriebene
        Kopie des Readers – und die Hardware-ID der aktuellen Sitzung
        gewinnt gegen die konfigurierte (Nutzerfall: gespeichert
        da284d4b..., Reader-Sitzung lief mit eb22e4cf...)."""
        from ..browser_login import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")
        fresh = self._jwt(time.time() - 5, "sess-live")
        logins = []

        class FirstFails(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        class SecondWorks(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-after-wait"

        clients = [FirstFails, SecondWorks]

        def fake_client(partner_id, hw):
            return clients.pop(0)(partner_id, hw)

        reader_hw = "eb22e4cf-bb01-4550-bff7-334446bb20b1"
        with patch("calibre_plugin.browser_login.TolinoClient", fake_client), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value={"refresh": [fresh],
                                 "hardware": [reader_hw],
                                 "idb": []}), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual("rotated-after-wait", refresh)
        self.assertEqual(reader_hw, hardware)
        self.assertEqual([spent, fresh], logins)

    def test_exchange_uses_grabbed_hardware_over_configured(self):
        """Die aus dem Reader mitgelesene Hardware-ID wird beim Tausch
        verwendet – nicht die alte konfigurierte (Nutzerfall: Plugin
        speicherte da284d4b..., Reader-Sitzung lief mit eb22e4cf...)."""
        from ..browser_login import _exchange_grabbed_token
        fresh = self._jwt(time.time() - 5, "sess-live")
        seen_hw = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                seen_hw.append(hw)
                self.refresh = fresh
                self.hardware = hw

            def _login(self):
                self.refresh = "rotated"

        with patch("calibre_plugin.cdp.reader_ws_url", return_value=None), \
             patch("calibre_plugin.browser_login.TolinoClient", FakeClient):
            _exchange_grabbed_token(
                4, "da284d4b-6348-43c8-b0ab-17edcddfb25d",
                {"refresh": [fresh],
                 "hardware": ["eb22e4cf-bb01-4550-bff7-334446bb20b1"]},
                [fresh])
        self.assertEqual(
            ["eb22e4cf-bb01-4550-bff7-334446bb20b1"], seen_hw)

    def test_grab_live_refresh_still_fails_when_no_new_candidate_appears(self):
        """Weder Storage noch Nachschub waehrend der Wartephase => klare
        Fehlermeldung mit dem ersten Endpoint-Fehler."""
        from ..browser_login import grab_live_refresh
        spent = self._jwt(time.time() - 10, "sess-live")

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        with patch("calibre_plugin.browser_login.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [spent], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   return_value=None), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        message = _full(ctx.exception)
        self.assertIn("gültiger Zugang", message)
        self.assertIn("Tolino hat den Zugang", ctx.exception.detail)
        # Die Meldung nennt nur das ALTER des geprüften Kandidaten
        # («5 Sekunden alt» vs. «3700 Sekunden alt»), nie seinen Wert.
        self.assertIn("Sekunden alt", message)
        self.assertNotIn(spent, message)

    def test_collect_grab_waits_while_window_is_on_partner_login_page(self):
        """Feldbefund: Waehrend der Anmeldung verlaesst der Tab
        mytolino.com zugunsten der Keycloak-Seite des Partners (andere
        Domain) – das darf NIEMALS als "Fenster geschlossen" gemeldet
        werden, solange der DevTools-Endpunkt antwortet."""
        from .. import cdp as cdp_module
        pages = [
            [{"type": "page",
              "url": "https://www.orellfuessli.ch/keycloak/realms/37/"
                     "protocol/openid-connect/auth",
              "webSocketDebuggerUrl": "ws://x"}],
            [{"type": "page",
              "url": "https://webreader.mytolino.com/library/index.html",
              "webSocketDebuggerUrl":
                  "ws://127.0.0.1:9223/devtools/page/x"}],
        ]
        messages = []
        with patch("calibre_plugin.cdp._verified_owner",
                   return_value={"port": 9223}), \
             patch("calibre_plugin.cdp._http_get_json",
                   side_effect=pages), \
             patch("calibre_plugin.cdp._grab_page_tokens",
                   return_value={"refresh": ["fresh-from-reader-token"],
                                 "hardware": [], "idb": []}), \
             patch("calibre_plugin.cdp.time.sleep", lambda _s: None):
            result = cdp_module.collect_grab(9223, timeout=30,
                                             progress=messages.append)
        self.assertEqual(["fresh-from-reader-token"], result["refresh"])
        self.assertTrue(any("offen" in text for text in messages))
        self.assertTrue(all("geschlossen" not in text
                            for text in messages))

    def test_collect_grab_reports_closed_only_when_endpoint_is_gone(self):
        """Erst wenn das DevTools-Endpunkt mehrfach in Folge nicht
        antwortet, ist das Fenster wirklich zu (kein Einzel-Aussetzer)."""
        from .. import cdp as cdp_module
        with patch("calibre_plugin.cdp._verified_owner", return_value=None), \
             patch("calibre_plugin.cdp._http_get_json",
                   return_value=None), \
             patch("calibre_plugin.cdp.time.sleep", lambda _s: None):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.collect_grab(9223, timeout=30,
                                        progress=lambda _t: None)
        self.assertIn("geschlossen", str(ctx.exception))
        self.assertEqual("B03", ctx.exception.code)

    def test_collect_grab_timeout_without_reader_tab_has_login_hint(self):
        """Bleibt der Tab die ganze Zeit ohne Web-Reader (z. B. Anmeldung
        nicht abgeschlossen), gibt es einen Timeout-Hinweis – keine
        geschlossene-Fenster-Meldung."""
        from .. import cdp as cdp_module
        with self.assertRaises(TolinoAuthError) as ctx:
            cdp_module.collect_grab(9223, timeout=0)
        message = str(ctx.exception)
        self.assertIn("Zeitüberschreitung", message)
        self.assertIn("nicht abgeschlossen", message)
        self.assertNotIn("wurde geschlossen", message)

    def _ws_run_server(self, thread_state, handler, connections=1):
        """Minimaler In-Process-WS-Server (Schema der Tests):
        bindet, meldet den Port via thread_state["ready"] und beantwortet
        bis zu `connections` aufeinanderfolgende Verbindungen per
        `handler(message, send_raw)` – jeder CDP-Aufruf oeffnet eine
        eigene Verbindung.
        """
        import socket as socket_module
        import struct as struct_module
        srv = socket_module.socket()
        srv.setsockopt(socket_module.SOL_SOCKET,
                       socket_module.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        thread_state["port"] = srv.getsockname()[1]
        thread_state["ready"].set()
        try:
            for _ in range(connections):
                conn, _addr = srv.accept()
                conn.recv(4096)
                conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\n"
                             b"Upgrade: websocket\r\n"
                             b"Connection: Upgrade\r\n"
                             b"Sec-WebSocket-Accept: x\r\n\r\n")

                def send_raw(body, conn=conn):
                    frame = bytearray([0x81])
                    if len(body) < 126:
                        frame.append(len(body))
                    else:
                        frame += bytes([126]) + struct_module.pack(
                            ">H", len(body))
                    conn.sendall(bytes(frame) + body)

                try:
                    while True:
                        header = _read_exact(conn, 2)
                        if len(header) < 2:
                            break
                        length = header[1] & 0x7F
                        if length == 126:
                            length = struct_module.unpack(
                                ">H", _read_exact(conn, 2))[0]
                        elif length == 127:
                            length = struct_module.unpack(
                                ">Q", _read_exact(conn, 8))[0]
                        if header[1] & 0x80:
                            mask = _read_exact(conn, 4)
                            payload = bytes(b ^ mask[i % 4] for i, b in
                                            enumerate(_read_exact(conn, length)))
                        else:
                            payload = _read_exact(conn, length)
                        try:
                            message = json.loads(payload.decode())
                        except ValueError:
                            continue
                        handler(message, send_raw)
                finally:
                    conn.close()
        finally:
            srv.close()

    def _ws_start_server(self, handler, connections=1):
        """Server-Thread starten; Rueckgabe: State mit ready/port."""
        import threading
        state = {"ready": threading.Event(), "port": None}
        threading.Thread(target=self._ws_run_server,
                         args=(state, handler, connections),
                         daemon=True).start()
        return state

    @staticmethod
    def _send_cdp_value(send_raw, message_id, value):
        send_raw(json.dumps({
            "id": message_id,
            "result": {"result": {"value": value}}}).encode())

    def test_evaluate_in_target_parses_json_string_replies(self):
        """Regression: Das Grab-Snippet antwortet mit
        JSON.stringify(out) – einem STRING. _evaluate_in_target muss
        diese Antwort parsten, sonst bleibt jeder Grab leer ("kein
        frischer Token"); frueher antworteten nur die Test-Server mit
        dict, wodurch die Regression unsichtbar blieb.
        """
        from .. import cdp as cdp_module

        def handler(message, send_raw):
            if message.get("method") != "Runtime.evaluate":
                return None
            payload = {"refresh": ["string-reply-caught-token-xyz"],
                       "hardware": [], "idb": []}
            self._send_cdp_value(send_raw, message["id"],
                                 json.dumps(payload))

        state = self._ws_start_server(handler)
        self.assertTrue(state["ready"].wait(5))
        result = cdp_module._evaluate_in_target(
            "ws://127.0.0.1:%d/devtools/page/x" % state["port"],
            "JSON.stringify(out)", timeout=5, expected_port=state["port"])
        self.assertIsNotNone(result)
        self.assertEqual(["string-reply-caught-token-xyz"],
                         result["refresh"])

    def test_grab_page_tokens_merges_indexeddb_databases(self):
        """_grab_page_tokens liest die vom Haupt-Snippet gemeldeten
        IndexedDB-Datenbanken mit: der Reader legt sein aktuelles
        Token-Set oft genau dort ab, und ohne den Merge ist er
        fuer den Grab unsichtbar."""
        from .. import cdp as cdp_module
        reader_hw = "eb22e4cf-bb01-4550-bff7-334446bb20b1"

        def handler(message, send_raw):
            expr = str((message.get("params") or {}).get("expression", ""))
            if "objectStoreNames" in expr:
                payload = {"refresh": ["idb-token-xyz"],
                           "hardware": [reader_hw]}
                self._send_cdp_value(send_raw, message["id"],
                                     json.dumps(payload))
                return None
            payload = {"refresh": ["storage-token-abc"],
                       "hardware": [], "idb": ["reader-token-db"]}
            self._send_cdp_value(send_raw, message["id"],
                                 json.dumps(payload))

        state = self._ws_start_server(handler, connections=2)
        self.assertTrue(state["ready"].wait(5))
        grabbed = cdp_module._grab_page_tokens(
            "ws://127.0.0.1:%d/devtools/page/x" % state["port"], timeout=5,
            expected_port=state["port"])
        self.assertEqual(["storage-token-abc", "idb-token-xyz"],
                         grabbed["refresh"])
        self.assertEqual([reader_hw], grabbed["hardware"])
        self.assertEqual(["reader-token-db"], grabbed["idb"])

    def test_idb_expression_passes_database_name_as_one_javascript_string(self):
        from ..cdp_scripts import idb_expression
        expression = idb_expression("tolino-user")
        self.assertTrue(expression.rstrip().endswith('})("tolino-user")'))
        self.assertNotIn("'\"", expression)

    def test_grab_snippets_parse_json_blob_store_values(self):
        """Top-Level-Speicher-Werte sind Strings: JSON-Blobs
        (oidc.user:...) muessen geparst werden, sonst ist der aktuelle
        Token unsichtbar (Feldbefund "kein frischer Token")."""
        from ..cdp_scripts import _GRAB_SNIPPET, _IDB_SNIPPET
        for snippet in (_GRAB_SNIPPET, _IDB_SNIPPET):
            self.assertIn("tryParse(value)", snippet)

    def test_browser_login_forwards_progress_into_live_grab(self):
        """Die Dialog-Fortschrittstexte (Fenster offen, Rotation
        erzwungen) muessen bis zum Live-Grab durchreichen."""
        progress_cb = lambda _text: None
        with patch("calibre_plugin.browser_login.grab_live_refresh",
                   return_value=("rotated", "hw-live")) as grab:
            result = browser_login(4, "test_hardware",
                                   progress=progress_cb)
        self.assertEqual(("rotated", "hw-live"), result)
        grab.assert_called_once()
        self.assertIs(progress_cb, grab.call_args[1].get("progress"))

class LoginWindowWaitTests(_SidJwtTest):
    """die Wartezeit nach einer Ablehnung unterscheidet, OB das
    Anmeldefenster noch lebt und WAS es zeigt.
    """

    SID = "sess-wait"

    def test_grab_live_refresh_keeps_login_page_window_open(self):
        """Fenster offen, Tab auf der Anmeldeseite: NICHT schliessen,
        zur Neu-Anmeldung vor Ort auffordern und LÄNGER warten."""
        from ..browser_login import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        grabs = []

        class FailingClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = hw

            def _login(self):
                raise TolinoAuthError("Tolino HTTP 400: invalid_grant")

        login_state = {"window": True, "reader_tab": False}
        with patch("calibre_plugin.browser_login.TolinoClient", FailingClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [fresh], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   side_effect=lambda *a, **k: grabs.append(1) or None), \
             patch("calibre_plugin.cdp.grabber_window_state",
                   return_value=login_state), \
             patch("calibre_plugin.cdp.close_grabber_window") as closer, \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0), \
             patch("calibre_plugin.browser_login.time.monotonic",
                   side_effect=[0, 0, 0.1, 2]):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        closer.assert_not_called()
        message = str(ctx.exception)
        self.assertIn("ist offen", message)
        self.assertIn("dort anmelden", message)
        self.assertNotIn("wurde geschlossen", message)
        # Auf der Anmeldeseite wird länger gewartet (zusätzliche
        # Versuche), damit die Neu-Anmeldung vor Ort noch ankommen kann.
        self.assertGreaterEqual(len(grabs), 1)

    def test_grab_live_refresh_stops_early_when_window_vanished(self):
        """Endpunkt stumm: nach drei Fehlschlägen abbrechen statt 30
        Wartegängen – und wie bisher schliessen + Neustart-Anweisung."""
        from ..browser_login import grab_live_refresh
        stale = self._refresh_jwt(time.time() - 7200)
        grabs = []

        def no_tab(*args, **kwargs):
            grabs.append(1)
            return None

        dead = {"window": False, "reader_tab": False}
        with patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [stale], "hardware": []}), \
             patch("calibre_plugin.cdp.grab_once_from_grabber",
                   side_effect=no_tab), \
             patch("calibre_plugin.cdp.grabber_window_state",
                   return_value=dead), \
             patch("calibre_plugin.browser_login._NEW_TOKEN_INTERVAL", 0), \
             patch("calibre_plugin.browser_login.time.monotonic",
                   side_effect=[0, 1, 2, 3]):
            with self.assertRaises(TolinoAuthError) as ctx:
                grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertLessEqual(len(grabs), 4)
        self.assertIn("gültiger Zugang", str(ctx.exception))

    def test_candidate_age_note_describes_undatable_candidate(self):
        """Undatierte Kandidaten werden strukturell beschrieben (Teile,
        Zeichen, Payload) – der Wert selbst nie."""
        from ..token_candidates import _candidate_age_note
        opaque = "opaque-refresh-token-value-without-any-dots"
        note = _candidate_age_note([opaque])
        self.assertIn("Alter des gefundenen Zugangs unbekannt", note)
        self.assertIn("1 Teil", note)
        self.assertIn("Zeichen", note)
        broken = "aaa.bbb.ccc"
        note = _candidate_age_note([broken])
        self.assertIn("3 Teile", note)
        self.assertIn("unlesbar", note)
        self.assertNotIn(opaque, note)
        self.assertNotIn(broken, note)
        # Datierbare Kandidaten bleiben unverändert.
        jwt = self._refresh_jwt(time.time() - 7)
        self.assertIn("Sekunden alt", _candidate_age_note([jwt]))

class WrappedCandidateTests(_SidJwtTest):
    """Der Seiten-Speicher des Readers kann Token-Teile gekodiert
    ablegen – Feldbefund der Extrahier-Schaltfläche: genau EIN
    Refresh-Kandidat, "1 Teil, 920 Zeichen", am Endpunkt abgelehnt
    (invalid_grant) und ohne datierbares JWT-alter. Ein JWT traegt immer
    zwei Punkte; die Hülle muss vor dem Tausch abgezogen werden.
    """

    SID = "sess-wrap"

    def test_unwrap_recovers_base64_wrapped_jwt(self):
        """base64- und doppelt gehüllte JWTs werden bis zum Kern
        entpackt (Feldform: 920 Zeichen ohne Punkt)."""
        from ..token_candidates import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        wrapped = base64.urlsafe_b64encode(
            fresh.encode("utf-8")).decode().rstrip("=")
        self.assertEqual(fresh, _unwrap_refresh_candidate(wrapped))
        double = base64.b64encode(wrapped.encode("utf-8")).decode()
        self.assertEqual(fresh, _unwrap_refresh_candidate(double))

    def test_unwrap_recovers_quoted_and_percent_encoded_jwt(self):
        """JSON-Zeichenkette und Prozent-Code (inkl. kodierter Punkte)
        sind ebenfalls bekannte Hüllen."""
        from ..token_candidates import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        self.assertEqual(fresh, _unwrap_refresh_candidate(json.dumps(fresh)))
        percent = fresh.replace(".", "%2E")
        self.assertEqual(fresh, _unwrap_refresh_candidate(percent))

    def test_unwrap_leaves_opaque_and_broken_values_untouched(self):
        """Opake (echt punktlose) Tokens und unlesbare Zeichenketten
        bleiben unverändert – nur ein JWT-Treffer gewinnt."""
        from ..token_candidates import _unwrap_refresh_candidate
        opaque = "a3f9" * 230  # 920 Zeichen wie im Feldbefund
        self.assertEqual(opaque, _unwrap_refresh_candidate(opaque))
        broken = "aaa.bbb.ccc"
        self.assertEqual(broken, _unwrap_refresh_candidate(broken))
        short = "plainopaquevalue-not-a-jwt"
        self.assertEqual(short, _unwrap_refresh_candidate(short))

    def test_normalize_makes_wrapped_stale_copy_fail_age_filter(self):
        """Gekodete alte Kopien passieren den 30-Minuten-Filter NICHT
        mehr: erst nach dem Entpacken sind sie datierbar und fallen
        unter den Cutoff statt am Endpunkt verbrannt zu werden."""
        from ..token_candidates import _filter_live_candidates, _normalize_live_grab
        stale = self._refresh_jwt(time.time() - 7200)
        wrapped = base64.urlsafe_b64encode(stale.encode("utf-8")).decode()
        normalized = _normalize_live_grab({"refresh": [wrapped],
                                           "hardware": []})
        self.assertEqual([stale], normalized["refresh"])
        self.assertEqual([], _filter_live_candidates(normalized["refresh"]))
        # Unveränderte Grabs werden nicht umsonst kopiert.
        plain = {"refresh": [stale], "hardware": []}
        self.assertIs(plain, _normalize_live_grab(plain))

    def test_grab_live_refresh_exchanges_the_unwrapped_jwt(self):
        """Der Endpunkt sieht das entpackte JWT – nie die Hülle."""
        from ..browser_login import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        wrapped = base64.urlsafe_b64encode(
            fresh.encode("utf-8")).decode().rstrip("=")
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        with patch("calibre_plugin.browser_login.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [wrapped], "hardware": []}), \
             patch("calibre_plugin.cdp.reader_ws_url", return_value=None):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual(("rotated-token", "fake-hw"),
                         (refresh, hardware))
        self.assertEqual([fresh], logins)

class ReaderBlobLiveGrabTests(_SidJwtTest):
    """Der Live-Grab entpackt die CryptoJS-verschluesselten
    userToken/userInfos-Bloebe des Readers.
    """

    SID = "sess-blob"

    def _encrypt(self, plain, phrase=""):
        """Unabhaengige Referenz-Implementation des CryptoJS-Blobs."""
        import hashlib
        try:
            from Crypto.Cipher import AES
        except ImportError:
            self.skipTest("pycryptodome missing: no reference AES available")
        salt = os.urandom(8)
        derived = b""
        prev = b""
        while len(derived) < 48:
            prev = hashlib.md5(prev + phrase.encode() + salt).digest()
            derived += prev
        key, iv = derived[:32], derived[32:]
        data = plain.encode()
        pad = 16 - len(data) % 16
        data += bytes([pad]) * pad
        ct = AES.new(key, AES.MODE_CBC, iv).encrypt(data)
        return base64.b64encode(b"Salted__" + salt + ct).decode()

    def test_unwrap_decrypts_reader_user_token_blob(self):
        """Der verschluesselte Kandidat wird zum echten JWT – und ist
        damit datierbar (Altersnotiz statt Struktur-Hinweis)."""
        from ..token_candidates import (_candidate_age_note, _filter_live_candidates,
                                     _normalize_live_grab,
                                     _unwrap_refresh_candidate)
        fresh = self._refresh_jwt(time.time() - 5)
        blob = self._encrypt(fresh)
        self.assertTrue(blob.startswith("U2FsdGVk"))
        self.assertEqual(fresh, _unwrap_refresh_candidate(blob))
        normalized = _normalize_live_grab({"refresh": [blob],
                                           "hardware": []})
        self.assertEqual([fresh], normalized["refresh"])
        self.assertEqual([fresh],
                         _filter_live_candidates(normalized["refresh"]))
        self.assertIn("Sekunden alt", _candidate_age_note([fresh]))

    def test_unwrap_decrypts_nested_user_token_bundle(self):
        """Doppelte Schicht: Blob -> {"refresh": Blob} -> JWT."""
        from ..token_candidates import _unwrap_refresh_candidate
        fresh = self._refresh_jwt(time.time() - 5)
        outer = self._encrypt(json.dumps(
            {"refresh": self._encrypt(fresh), "expireTime": 1}))
        self.assertEqual(fresh, _unwrap_refresh_candidate(outer))

    def test_normalize_extracts_hardware_from_user_infos_blob(self):
        """Die im curl-Export gezeigte Hardware-Id steckt im
        verschluesselten userInfos – extrahiert, und niemals als
        Ciphertext weitergereicht."""
        from ..token_candidates import _normalize_live_grab
        blob = self._encrypt(json.dumps({
            "userId": "54389718", "devKey": "dk",
            "hardwareId": "eb22e4cf-bb01-4550-bff7-334446bb20b1"}))
        normalized = _normalize_live_grab({"refresh": [],
                                           "hardware": [blob]})
        self.assertEqual(["eb22e4cf-bb01-4550-bff7-334446bb20b1"],
                         normalized["hardware"])
        # Undurchdringbarer Ciphertext verschwindet stillschweigend,
        # statt als Hardware-Id gespeichert oder getauscht zu werden.
        opaque = self._encrypt('{"keine": "hardware"}', phrase="fremd")
        normalized = _normalize_live_grab({"refresh": [],
                                           "hardware": [opaque]})
        self.assertEqual([], normalized["hardware"])
        # Klartext-Hardware-Ids bleiben wie sie sind.
        plain_hw = "a0b2c3d4-e5f6-7788-99aa-bbccddeeff00"
        holder = {"refresh": [], "hardware": [plain_hw]}
        self.assertIs(holder, _normalize_live_grab(holder))

    def test_grab_live_refresh_exchanges_the_decrypted_blob(self):
        """End-to-end: verschluesselter Kandidat -> Entschluesselung ->
        Tausch des echten JWT (FakeClient sieht den Klartext)."""
        from ..browser_login import grab_live_refresh
        fresh = self._refresh_jwt(time.time() - 5)
        blob = self._encrypt(fresh)
        logins = []

        class FakeClient(object):
            def __init__(self, partner_id, hw):
                self.refresh = None
                self.hardware = "fake-hw"

            def _login(self):
                logins.append(self.refresh)
                self.refresh = "rotated-token"

        with patch("calibre_plugin.browser_login.TolinoClient", FakeClient), \
             patch("calibre_plugin.cdp.grab_live_tokens",
                   return_value={"refresh": [blob], "hardware": []}), \
             patch("calibre_plugin.cdp.reader_ws_url", return_value=None):
            refresh, hardware = grab_live_refresh(4, "cfg-hw", timeout=1)
        self.assertEqual(("rotated-token", "fake-hw"),
                         (refresh, hardware))
        self.assertEqual([fresh], logins)


class ChromiumOpensRegardlessOfDefaultTests(unittest.TestCase):
    """Chromium-Launcher und Temp-Profil."""

    def setUp(self):
        self._launched = dict(cdp_module._LAUNCHED)
        self.addCleanup(cdp_module._LAUNCHED.update, self._launched)
        cdp_module._LAUNCHED.update({"process": None, "port": None,
                                     "profile": ""})


    def test_chromium_candidates_finds_extra_binary_names(self):
        """Auch `chrome`, Beta/Dev-Varianten etc. werden als Befehlsname
        erkannt – nicht nur die klassischen zehn."""
        with tempfile.TemporaryDirectory() as home:
            with patch("shutil.which",
                       side_effect=lambda name: "/usr/bin/%s" % name
                       if name == "chrome" else None):
                found = browser_finder.chromium_candidates(home=home)
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/chrome"], argvs)

    def test_chromium_candidates_falls_back_to_known_paths(self):
        """Fest verdrahtete Installationsorte finden auch dann eine
        installierte Chromium-Variante, wenn Calibre mit reduziertem
        PATH startet (Desktop-Start)."""
        with tempfile.TemporaryDirectory() as home:
            path = os.path.join(home, "chromium-bin")
            with open(path, "w") as handle:
                handle.write("#!/bin/sh\n")
            os.chmod(path, 0o755)
            with patch.object(browser_finder, "_EXTRA_BROWSER_PATHS", (path,)), \
                 patch.object(browser_finder, "_host_probe_status", ""), \
                 patch("shutil.which", return_value=None):
                found = browser_finder.chromium_candidates(home=home)
        self.assertIn([path], [argv for argv, _label in found])
        self.assertIn("Chromium", [label for _argv, label in found])


    def test_host_probe_status_empty_when_host_has_no_browser(self):
        """Sonde erlaubt (rc 0), aber der Host hat keinen Chromium --
        Status "empty" fordert dann die Installation."""
        from .. import cdp as cdp_module

        class EmptyProc(object):
            returncode = 0
            stdout = ""

        with patch.object(browser_finder, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(browser_finder, "_host_probe_status", ""), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=EmptyProc()):
            browser_finder.chromium_candidates(home="/nonexistent-home")
            self.assertEqual("empty", browser_finder._host_probe_status)

    def test_launch_names_installation_when_host_probe_empty(self):
        """Ist die Sonde durchgelaufen und hat nichts gefunden, nennt die
        Meldung die Installation statt des override-Befehls (der wäre
        überflüssig)."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(browser_finder, "_host_probe_status", "empty"), \
             _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("installieren", message)
        self.assertNotIn("flatpak override", message)

    def test_launch_falls_through_to_next_candidate(self):
        """Ein defekter erster Kandidat (OSError beim Start) darf die
        Anmeldung nicht abwürgen: die nächste installierte
        Chromium-Variante wird gestartet."""
        import tempfile
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        broken = (["/usr/bin/broken"], "Broken")
        working = (["/usr/bin/working"], "Working")
        spawned = {}

        class FakePopen(object):
            def __init__(self, args, **kwargs):
                if args[0] == "/usr/bin/broken":
                    raise OSError("exec format error")
                spawned["args"] = list(args)

        with tempfile.TemporaryDirectory():
            with _patch.object(cdp_module, "pick_chromium",
                               return_value=broken), \
                 _patch.object(browser_finder, "chromium_options",
                               return_value=[broken, working]), \
                 _patch.object(browser_finder, "_host_probe_status", ""), \
                 _patch.object(cdp_module.subprocess, "Popen", FakePopen), \
                 _patch.object(cdp_module, "_http_get_json",
                               return_value={"webSocketDebuggerUrl":
                                             "ws://127.0.0.1:%d/devtools/browser/x" % 9441}), \
                 _patch.object(cdp_module.time, "sleep"):
                port = cdp_module.launch_reader_window(4)
        self.assertEqual("/usr/bin/working", spawned["args"][0])
        self.assertIn("--remote-debugging-port=%d" % port, spawned["args"])
        self.assertIn("mytolino.com", spawned["args"][-1])

    def test_a_timed_out_launch_stops_process_and_removes_profile(self):
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module
        state = {"terminated": False, "waited": False}

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.profile = next(arg.split("=", 1)[1] for arg in args
                                    if arg.startswith("--user-data-dir="))

            def poll(self):
                return 0 if state["terminated"] else None

            def terminate(self):
                state["terminated"] = True

            def wait(self, timeout=None):
                state["waited"] = True
                return 0

        with _patch.object(cdp_module, "pick_chromium",
                           return_value=(["/usr/bin/chromium"], "Chromium")), \
             _patch.object(cdp_module, "_start_port", return_value=9333), \
             _patch.object(cdp_module.subprocess, "Popen", FakePopen), \
             _patch.object(cdp_module, "_http_get_json", return_value=None), \
             _patch.object(cdp_module, "_endpoint_answers", return_value=False), \
             _patch.object(cdp_module.time, "time", side_effect=[0, 100]), \
             _patch.object(cdp_module.time, "sleep"):
            with self.assertRaises(TolinoAuthError):
                cdp_module.launch_reader_window(4)
        self.assertTrue(state["terminated"])
        self.assertTrue(state["waited"])
        self.assertIsNone(cdp_module._LAUNCHED["process"])

    def test_failed_browser_spawn_removes_only_its_temporary_profile(self):
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module
        spawned_profile = []

        class FakePopen:
            def __init__(self, args, **kwargs):
                spawned_profile.append(next(
                    arg.split("=", 1)[1] for arg in args
                    if arg.startswith("--user-data-dir=")))
                raise OSError("cannot start")

        with _patch.object(cdp_module, "pick_chromium",
                           return_value=(["/usr/bin/chromium"], "Chromium")), \
             _patch.object(browser_finder, "chromium_options", return_value=[]), \
             _patch.object(cdp_module.subprocess, "Popen", FakePopen):
            with self.assertRaises(TolinoAuthError):
                cdp_module.launch_reader_window(4)
        self.assertEqual(1, len(spawned_profile))
        self.assertFalse(os.path.exists(spawned_profile[0]))


class PlatformBrowserPathTests(unittest.TestCase):
    """Standardinstallationsorte fuer Windows und macOS."""

    def _executable(self, root, relative):
        path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("browser")
        return path

    def test_windows_roots_share_one_search_and_skip_duplicate_labels(self):
        program_files = r"C:\Program Files"
        program_files_x86 = r"C:\Program Files (x86)"
        chrome = os.path.join(
            program_files, r"Google\Chrome\Application\chrome.exe")
        duplicate_chrome = os.path.join(
            program_files_x86, r"Google\Chrome\Application\chrome.exe")
        chromium = os.path.join(
            program_files_x86, r"Chromium\Application\chrome.exe")
        existing_paths = {chrome, duplicate_chrome, chromium}
        with patch.dict(os.environ, {
                "PROGRAMFILES": program_files,
                "PROGRAMFILES(X86)": program_files_x86}), \
                patch.object(os.path, "isfile",
                             side_effect=lambda path: path in existing_paths):
            found = browser_finder._platform_browser_paths("win32")
        self.assertEqual([([chrome], "Google Chrome"),
                          ([chromium], "Chromium")], found)

    def test_macos_checks_system_and_user_application_directories(self):
        with tempfile.TemporaryDirectory() as home:
            chrome = self._executable(
                os.path.join(home, "Applications"),
                "Google Chrome.app/Contents/MacOS/Google Chrome")
            edge = self._executable(
                os.path.join(home, "Applications"),
                "Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
            real_isfile = os.path.isfile

            def isfile(path):
                if str(path).startswith("/Applications/"):
                    return False
                return real_isfile(path)

            with patch("os.path.expanduser", return_value=home), \
                    patch.object(os.path, "isfile", side_effect=isfile):
                found = browser_finder._platform_browser_paths("darwin")
        self.assertEqual([([chrome], "Google Chrome"), ([edge], "Edge")], found)


class FlatpakSandboxBrowserOpenTests(unittest.TestCase):
    """Feldbefund: Pop!_OS, Calibre über Flathub – "öffnet sich
    kein browser". In der Flatpak-Sandbox sind die Browser des Rechners
    unsichtbar (kein Browser im PATH, kein flatpak, kein flatpak run);
    die Öffnerkette und die Fehlermeldungen müssen das offenlegen."""

    URL = "https://webreader.mytolino.com/library/"

    def test_launch_reader_window_without_sandbox_has_no_permission_hint(self):
        """Ausserhalb einer Sandbox nennt dieselbe Meldung KEINEN
        Flatpak-Befehl (sonst führt sie in die Irre)."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(browser_finder, "_host_probe_status", ""), \
             _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=False):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("Kein Chromium-Browser gefunden", message)
        self.assertNotIn("flatpak override", message)

    def test_chromium_candidates_probe_host_browser_via_flatpak_spawn(self):
        """Die Sandbox-Sonde liest native Host-Pfade und Host-Flatpak-
        Apps und baut Launcher mit `flatpak-spawn --host` als Präfix."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        class FakeProc(object):
            returncode = 0
            stdout = ("NATIVE /usr/bin/chromium\n" + "".join(
                "FLATPAK %s\n" % app_id
                for app_id, _command, _label, _host_label
                in browser_finder._FLATPAK_BROWSER_APPS))

        with _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=True), \
             _patch("shutil.which",
                    side_effect=lambda name: "/usr/bin/flatpak-spawn"
                    if name == "flatpak-spawn" else None), \
             _patch.object(cdp_module.subprocess, "run",
                           return_value=FakeProc()):
            found = browser_finder.chromium_candidates(home="/nonexistent-home")
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/flatpak-spawn", "--host",
                       "/usr/bin/chromium"], argvs)
        self.assertIn(["/usr/bin/flatpak-spawn", "--host", "flatpak", "run",
                       "--command=chrome", "org.chromium.Chromium"], argvs)
        labels = [label for _argv, label in found]
        self.assertIn("chromium (Host)", labels)
        for _app_id, _command, _label, host_label in browser_finder._FLATPAK_BROWSER_APPS:
            self.assertIn(host_label, labels)


    def test_chromium_candidates_never_probe_outside_sandbox(self):
        """Ausserhalb einer Sandbox wird für die Host-Sonde nie ein
        Prozess gestartet."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        with _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=False), \
             _patch.object(browser_finder, "_EXTRA_BROWSER_PATHS", ()), \
             _patch("shutil.which", return_value=None), \
             _patch.object(cdp_module.subprocess, "run",
                           side_effect=AssertionError("probe started")):
            found = browser_finder.chromium_candidates(home="/nonexistent-home")
        self.assertEqual([], found)

class FlatpakHostProbeReportTests(unittest.TestCase):
    """Feldbefund: der override-Befehl warerteilt, die Meldung
    «kein Chromium-Browser … Freigabe erteilen» blieb trotzdem. Die
    Host-Sonde wertete den Exit-Code des sh-Skripts aus, dessen letzte
    flatpak-info-Prüfung auf fast jedem Rechner scheitert – sie
    meldete also IMMER «Freigabe fehlt» und warf die gefundenen
    Host-Browser weg.
    """

    def test_host_probe_keeps_results_when_script_exits_nonzero(self):
        """Ein Fehlercode MIT Ausgabe ist kein Freigabe-Verlust: die
        NATIVE-Zeilen werden gelesen, der Status wird «ok»."""
        from .. import cdp as cdp_module

        class Proc(object):
            returncode = 1
            stdout = "NATIVE /usr/bin/chromium\n"

        with patch.object(browser_finder, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(browser_finder, "_host_probe_status", ""), \
             patch.object(browser_finder, "_EXTRA_BROWSER_PATHS", ()), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=Proc()):
            found = browser_finder.chromium_candidates(home="/nonexistent-home")
            # Status hier pruefen: der Patch stellt den alten Wert wieder
            # her, sobald der Block endet (Verhalten wie die
            # Bestandstests in ChromiumOpensRegardlessOfDefaultTests).
            self.assertEqual("ok", browser_finder._host_probe_status)
        argvs = [argv for argv, _label in found]
        self.assertIn(["/usr/bin/flatpak-spawn", "--host",
                       "/usr/bin/chromium"], argvs)

    def test_host_probe_script_forces_exit_zero(self):
        """Das Sonden-Skript endet mit `exit 0` – sonst meldet die
        Host-Schleife selbst dann einen Fehler, wenn die Freigabe
        erteilt ist (Regressionsschutz für den Feldbefund)."""
        from .. import cdp as cdp_module

        captured = {}

        class Proc(object):
            returncode = 0
            stdout = ""

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            return Proc()

        with patch.object(browser_finder, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(browser_finder, "_host_probe_status", ""), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          side_effect=fake_run):
            browser_finder.chromium_candidates(home="/nonexistent-home")
        script = captured["argv"][-1]
        self.assertTrue(script.rstrip().endswith("exit 0"), script)
        for app_id, _command, _label, _host_label in browser_finder._FLATPAK_BROWSER_APPS:
            self.assertIn(app_id, script)

    def test_denied_still_reported_without_any_output(self):
        """Ohne Ausgabe und mit Fehlercode bleibt «denied» die einzige
        plausible Deutung: flatpak-spawn wurde abgewiesen. Ob die Sonde
        gar nichts oder nur Leerraum liefert, ist dabei egal."""
        from .. import cdp as cdp_module

        class Proc(object):
            returncode = 1
            stdout = "   "

        with patch.object(browser_finder, "in_flatpak_sandbox",
                          return_value=True), \
             patch.object(browser_finder, "_host_probe_status", ""), \
             patch.object(browser_finder, "_EXTRA_BROWSER_PATHS", ()), \
             patch("shutil.which",
                   side_effect=lambda name: "/usr/bin/flatpak-spawn"
                   if name == "flatpak-spawn" else None), \
             patch.object(cdp_module.subprocess, "run",
                          return_value=Proc()):
            found = browser_finder.chromium_candidates(home="/nonexistent-home")
            self.assertEqual("denied", browser_finder._host_probe_status)
        self.assertEqual([], found)

    def test_launch_permission_names_both_installations(self):
        """Die Meldung nennt beide Installationen (User-Befehl wie
        ausgeführt, System-Befehl mit sudo), den Nachweis über
        `flatpak info --show-permissions` und den vollständigen
        Neustart."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(browser_finder, "_host_probe_status", "denied"), \
             _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("Kein Chromium-Browser gefunden", message)
        self.assertIn("flatpak override --user", message)
        self.assertIn("sudo flatpak override", message)
        self.assertIn("--show-permissions", message)
        self.assertIn("neu starten", message)

    def test_launch_names_runtime_update_when_spawn_missing(self):
        """Ohne flatpak-spawn hilft keine Freigabe – die Meldung
        nennt dann `flatpak update` statt des override-Befehls."""
        from unittest.mock import patch as _patch
        from .. import cdp as cdp_module

        with _patch.object(cdp_module, "pick_chromium", return_value=None), \
             _patch.object(browser_finder, "_host_probe_status",
                           "unavailable"), \
             _patch.object(browser_finder, "in_flatpak_sandbox",
                           return_value=True):
            with self.assertRaises(TolinoAuthError) as ctx:
                cdp_module.launch_reader_window(4)
        message = str(ctx.exception)
        self.assertIn("flatpak update", message)
        self.assertNotIn("flatpak override", message)

class CdpOwnershipTests(unittest.TestCase):
    """CDP nutzt nur den gestarteten Prozess, das Temp-Profil und den Port."""

    def setUp(self):
        from .. import cdp
        self.cdp = cdp
        self.original = dict(cdp._LAUNCHED)
        self.addCleanup(cdp._LAUNCHED.update, self.original)
        cdp._LAUNCHED.update({"process": None, "port": None, "profile": ""})

    def _launch(self, process, port=9223, profile=None):
        self.cdp._LAUNCHED.update({
            "process": process, "port": port,
            "profile": profile or tempfile.mkdtemp(prefix="cdp-test-")})
        return self.cdp._launched_record(port, require_endpoint=False)

    def test_browser_websocket_uses_localhost_and_requires_selected_port(self):
        parse = self.cdp._devtools_ws_address
        for host in ("localhost", "127.0.0.1", "[::1]"):
            with self.subTest(host=host):
                self.assertEqual(("127.0.0.1", 9223, "/devtools/page/x"),
                                 parse("ws://%s:9223/devtools/page/x" % host,
                                       9223))
        with self.assertRaises(ValueError):
            parse("ws://attacker.invalid:9223/devtools/page/x", 9223)
        for url in ("ws://127.0.0.1:9999/devtools/page/x",
                    "wss://127.0.0.1:9223/devtools/page/x",
                    "ws://127.0.0.1:9223/json/version"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse(url, 9223)

    def test_browser_close_targets_only_loopback_and_selected_port(self):
        calls = []

        class FakeWs:
            @staticmethod
            def connect(host, port, path, timeout=10):
                calls.append((host, port, path))
                return FakeWs()

            def send_json(self, value):
                calls.append(value)

            def recv_json(self, timeout=10):
                return {}

            def close(self):
                calls.append("closed")

        with patch.object(self.cdp, "_Ws", FakeWs):
            self.assertTrue(self.cdp._send_browser_close(
                "127.0.0.1", 9223, "/devtools/browser/x", expected_port=9223))
            self.assertFalse(self.cdp._send_browser_close(
                "attacker.invalid", 9223, "/devtools/browser/x",
                expected_port=9223))
            self.assertFalse(self.cdp._send_browser_close(
                "127.0.0.1", 9999, "/devtools/browser/x",
                expected_port=9223))
        self.assertIn({"id": 1, "method": "Browser.close"}, calls)
        self.assertEqual([("127.0.0.1", 9223, "/devtools/browser/x")],
                         [call for call in calls if isinstance(call, tuple)])

    def test_owner_requires_in_memory_process_and_endpoint(self):
        class Process:
            @staticmethod
            def poll():
                return None

        self._launch(Process())
        with patch.object(self.cdp, "_endpoint_answers", return_value=True):
            self.assertEqual(9223, self.cdp._verified_owner()["port"])
            self.assertIsNotNone(self.cdp._verified_owner(9223))
            self.assertIsNone(self.cdp._verified_owner(9224))
        with patch.object(self.cdp, "_endpoint_answers", return_value=False):
            self.assertIsNone(self.cdp._verified_owner())

    def test_launch_uses_fresh_temporary_profile_and_remembers_process(self):
        spawned = {}

        class FakePopen:
            def __init__(self, args, **kwargs):
                self.args = args
                spawned["args"] = args
                spawned["process"] = self

            @staticmethod
            def poll():
                return None

        with patch.object(self.cdp, "pick_chromium",
                          return_value=(["/usr/bin/chromium"], "Chromium")), \
             patch.object(self.cdp, "_start_port", return_value=9441), \
             patch.object(self.cdp.subprocess, "Popen", FakePopen), \
             patch.object(self.cdp, "_http_get_json", return_value={"ok": True}):
            port = self.cdp.launch_reader_window(4)
        self.assertEqual(9441, port)
        profile_arg = next(arg for arg in spawned["args"]
                           if arg.startswith("--user-data-dir="))
        profile = profile_arg.split("=", 1)[1]
        self.assertTrue(os.path.isdir(profile))
        self.assertTrue(profile.startswith(tempfile.gettempdir()))
        self.assertIs(spawned["process"], self.cdp._LAUNCHED["process"])
        self.assertEqual(port, self.cdp._LAUNCHED["port"])
        self.assertEqual(profile, self.cdp._LAUNCHED["profile"])
        shutil.rmtree(profile, ignore_errors=True)

    def test_close_sends_browser_close_then_terminates_and_removes_profile(self):
        state = {"running": True, "closed": False, "terminated": False}

        class Process:
            def poll(self):
                return None if state["running"] else 0

            def terminate(self):
                state["terminated"] = True
                state["running"] = False

            def wait(self, timeout=None):
                return 0

        profile = tempfile.mkdtemp(prefix="tolino-cdp-close-")
        process = Process()
        self._launch(process, profile=profile)
        with patch.object(self.cdp, "_endpoint_answers",
                          side_effect=[True, False, False, False]), \
             patch.object(self.cdp, "_stop_launched_process",
                          side_effect=lambda child, timeout=2:
                          child.terminate() or True) as stop, \
             patch.object(self.cdp, "_browser_ws_for_record",
                          return_value=("127.0.0.1", 9223,
                                        "/devtools/browser/x")), \
             patch.object(self.cdp, "_send_browser_close",
                          side_effect=lambda *a, **k: state.update(closed=True)
                          or True), \
             patch.object(self.cdp.time, "sleep"):
            self.assertTrue(_REAL_CLOSE_WINDOW(timeout=0.2))
        stop.assert_called_once_with(process, 0.2)
        self.assertTrue(state["closed"])
        self.assertTrue(state["terminated"])
        self.assertFalse(os.path.exists(profile))
        self.assertIsNone(self.cdp._LAUNCHED["process"])

    def test_close_never_removes_profile_while_endpoint_answers(self):
        class Process:
            def poll(self):
                return None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                return 0

        profile = tempfile.mkdtemp(prefix="tolino-cdp-live-")
        self._launch(Process(), profile=profile)
        with patch.object(self.cdp, "_endpoint_answers", return_value=True), \
             patch.object(self.cdp, "_browser_ws_for_record",
                          return_value=None), \
             patch.object(self.cdp.time, "sleep"):
            self.assertFalse(self.cdp.close_grabber_window(timeout=0.2))
        self.assertTrue(os.path.isdir(profile))
        shutil.rmtree(profile, ignore_errors=True)

    def test_reader_page_requires_an_https_tolino_host(self):
        for url in ("https://webreader.mytolino.com/library/",
                    "https://mytolino.com/library/"):
            self.assertTrue(self.cdp._is_reader_url(url))
        for url in ("http://webreader.mytolino.com/",
                    "https://mytolino.com.attacker.invalid/",
                    "file:///tmp/mytolino.com"):
            self.assertFalse(self.cdp._is_reader_url(url))


class ThreadShutdownTests(unittest.TestCase):
    """closeEvent blockiert nie unbegrenzt (P2/H)."""

    def _ui(self):
        return ui_base_tests.ToolbarIconTests._load_ui_module(lambda *args, **kwargs: None)

    def test_stop_thread_detaches_after_the_timeout(self):
        ui_module = self._ui()
        try:
            calls = []

            class SlowThread:
                def quit(self):
                    calls.append("quit")

                def wait(self, timeout):
                    calls.append(("wait", timeout))
                    return False

                def setParent(self, parent):
                    calls.append(("parent", parent))

            thread = SlowThread()
            self.assertFalse(ui_module.common._stop_thread(thread, timeout_ms=1234))
            self.assertIn(("wait", 1234), calls)
            self.assertIn(("parent", None), calls)
            self.assertIn(thread, ui_module.common._ORPHANED_THREADS)
        finally:
            ui_module.common._ORPHANED_THREADS.clear()
            ui_module._restore_stubs_for_tests()

    def test_close_event_cancels_and_never_waits_forever(self):
        ui_module = self._ui()
        try:
            calls = []

            class SlowThread:
                def quit(self):
                    calls.append("quit")

                def wait(self, timeout):
                    calls.append(("wait", timeout))
                    return False

                def setParent(self, parent):
                    pass

            class Event:
                def accept(self):
                    calls.append("accepted")

            dashboard = type("Dashboard", (), {})()
            dashboard.thread = SlowThread()
            dashboard.prepare_thread = None
            dashboard.prepare_worker = None
            dashboard.login_thread = None
            dashboard.login_worker = None
            dashboard.cancel_sync = lambda: calls.append("cancel")
            dashboard._save_visible_account = lambda: calls.append("saved")
            ui_module.dashboard.SyncDashboard.closeEvent(dashboard, Event())
            self.assertIn("cancel", calls)
            self.assertIn("accepted", calls)
            self.assertIn(("wait", 3000), calls)
        finally:
            ui_module.common._ORPHANED_THREADS.clear()
            ui_module._restore_stubs_for_tests()

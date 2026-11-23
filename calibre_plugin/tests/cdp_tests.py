"""Lebenszyklus des Anmeldefensters ohne echten Browser.

Prozess, DevTools-Port und Profilordner sind Doubles bzw. echte temporaere
Verzeichnisse; die Zustandsuebergaenge (starten, warten, besitzen,
schliessen) laufen im Produktivcode.
"""
import itertools
import json
import os
import shutil
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .. import cdp, cdp_scripts
from ..errors import TolinoAuthError

PID = 4194304  # existiert nicht: os.getpgid() scheitert, keine Prozessgruppe
REAL_MKDTEMP = tempfile.mkdtemp


class FakeProcess:
    """Popen-Ersatz; ``exits_on`` bestimmt, welches Signal wirkt."""

    def __init__(self, alive=True, exits_on=("terminate", "kill"), pid=PID):
        self.pid = pid
        self.alive = alive
        self.exits_on = exits_on
        self.actions = []

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.actions.append("terminate")
        if "terminate" in self.exits_on:
            self.alive = False

    def kill(self):
        self.actions.append("kill")
        if "kill" in self.exits_on:
            self.alive = False

    def wait(self, timeout=None):
        self.actions.append("wait")
        if self.alive:
            raise subprocess.TimeoutExpired("chromium", timeout)
        return 0


class LaunchCase(unittest.TestCase):
    """Jeder Test startet ohne gemerktes Fenster und raeumt seine Ordner."""

    def setUp(self):
        self._saved = dict(cdp._LAUNCHED)
        cdp._LAUNCHED.update({"process": None, "port": None, "profile": "",
                              "pgid": None})
        self.created = []
        self.addCleanup(self._restore)

    def _restore(self):
        for path in self.created:
            shutil.rmtree(path, ignore_errors=True)
        cdp._LAUNCHED.clear()
        cdp._LAUNCHED.update(self._saved)

    def mkdtemp(self, **_kwargs):
        path = REAL_MKDTEMP(prefix="cdp-test-")
        self.created.append(path)
        return path

    def isolate(self, browsers, **popen_kwargs):
        """Browser-Liste, Prozessstart und Profilordner kontrollieren."""
        return (
            patch("calibre_plugin.cdp.browser_finder.find_browsers",
                  return_value=(browsers, "ok")),
            patch("calibre_plugin.cdp.subprocess.Popen", **popen_kwargs),
            patch("calibre_plugin.cdp.tempfile.mkdtemp",
                  side_effect=self.mkdtemp),
        )


class StartTests(LaunchCase):
    """Ein Kandidat darf die Anmeldung nicht blockieren -- aber auch nicht
    einen zweiten Start auf demselben Port vortaeuschen."""

    def test_the_next_candidate_is_tried_when_the_first_cannot_start(self):
        process = FakeProcess()
        patches = self.isolate([["/defekt"], ["/usr/bin/chromium"]],
                               side_effect=[OSError("kaputt"), process])
        with patches[0], patches[1] as popen, patches[2], \
                patch("calibre_plugin.cdp._http_get_json",
                      return_value={"ok": 1}):
            cdp.launch_reader_window()
        self.assertEqual(2, popen.call_count)
        self.assertIs(process, cdp._LAUNCHED["process"])
        self.assertTrue(os.path.isdir(cdp._LAUNCHED["profile"]))
        argv = popen.call_args[0][0]
        self.assertIn("--remote-debugging-port=%d" % cdp._LAUNCHED["port"], argv)

    def test_a_process_that_exits_during_the_wait_fails_the_candidate(self):
        dead, alive = FakeProcess(alive=False), FakeProcess()
        patches = self.isolate([["/tot"], ["/usr/bin/chromium"]],
                               side_effect=[dead, alive])
        with patches[0], patches[1], patches[2], \
                patch("calibre_plugin.cdp._http_get_json",
                      side_effect=[None, None, {"ok": 1}]):
            cdp.launch_reader_window()
        self.assertIs(alive, cdp._LAUNCHED["process"])

    def test_when_every_candidate_fails_no_owner_or_profile_remains(self):
        patches = self.isolate([["/a"], ["/b"]],
                               side_effect=OSError("nicht startbar"))
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(TolinoAuthError) as caught:
                cdp.launch_reader_window()
        self.assertEqual("B01", caught.exception.code)
        self.assertEqual({"process": None, "port": None, "profile": "",
                          "pgid": None}, cdp._LAUNCHED)
        self.assertTrue(self.created)
        self.assertFalse(any(os.path.exists(path) for path in self.created))

    def test_a_stuck_process_does_not_start_a_second_browser(self):
        """Sonst laese der naechste Kandidat die Antwort des alten Prozesses."""
        stuck = FakeProcess(exits_on=())
        patches = self.isolate([["/haengt"], ["/usr/bin/chromium"]],
                               return_value=stuck)
        with patches[0], patches[1] as popen, patches[2], \
                patch("calibre_plugin.cdp._http_get_json", return_value=None), \
                patch("calibre_plugin.cdp.time.monotonic",
                      side_effect=itertools.count(0, 100).__next__):
            with self.assertRaises(TolinoAuthError) as caught:
                cdp.launch_reader_window()
        self.assertEqual(1, popen.call_count)
        self.assertIn("nicht beenden", str(caught.exception))
        # Der laufende Prozess bleibt als eigener Besitzer vermerkt und sein
        # Profil bleibt, solange er es benutzt.
        self.assertIs(stuck, cdp._LAUNCHED["process"])
        self.assertTrue(os.path.isdir(cdp._LAUNCHED["profile"]))

    def test_a_group_that_outlives_the_wrapper_is_not_reported_as_stopped(self):
        """Ein Browserkind ohne Port lebt weiter -- Erfolg erst nach seinem Ende."""
        # Der Wrapper ist schon fertig; nur die Gruppe haelt den Lauf auf.
        wrapper = FakeProcess(alive=False, exits_on=())
        signals = []

        def killpg(group, sig):
            signals.append((group, sig))

        with patch("calibre_plugin.cdp.os.killpg", side_effect=killpg), \
                patch("calibre_plugin.cdp._group_alive", return_value=True), \
                patch("calibre_plugin.cdp._endpoint_answers",
                      return_value=False), \
                patch("calibre_plugin.cdp.time.monotonic",
                      side_effect=itertools.count(0, 100).__next__):
            self.assertFalse(cdp._stop_launched_process(
                wrapper, timeout=0.2, pgid=wrapper.pid, port=0))
        self.assertEqual([(wrapper.pid, signal.SIGTERM),
                          (wrapper.pid, signal.SIGKILL)], signals)

    def test_a_wrapper_that_exits_without_its_browser_stops_the_start(self):
        """Ein beendeter Flatpak-Wrapper sagt nichts ueber Chromium.

        Der Gruppenfuehrer ist weg, der DevTools-Port antwortet aber noch:
        die gemerkte Gruppen-ID muss den Port freigeben, sonst startete der
        naechste Kandidat auf einem belegten Port und dessen Antwort wuerde
        als eigene gelesen.
        """
        wrapper = FakeProcess(exits_on=("terminate",))
        patches = self.isolate([["/flatpak-run"], ["/usr/bin/chromium"]],
                               return_value=wrapper)
        with patches[0], patches[1] as popen, patches[2], \
                patch("calibre_plugin.cdp._wait_for_debug_port",
                      return_value=False), \
                patch("calibre_plugin.cdp._endpoint_answers",
                      return_value=True), \
                patch("calibre_plugin.cdp._group_alive", return_value=True), \
                patch("calibre_plugin.cdp.os.killpg") as killpg:
            with self.assertRaises(TolinoAuthError) as caught:
                cdp.launch_reader_window()
        self.assertIn("nicht beenden", str(caught.exception))
        self.assertEqual(1, popen.call_count)
        # Erst hoeflich an die GEMERKTE Gruppe (nicht mehr per getpgid), dann
        # hart -- obwohl der Wrapper schon beendet ist.
        self.assertEqual([(wrapper.pid, signal.SIGTERM),
                          (wrapper.pid, signal.SIGKILL)],
                         [call.args for call in killpg.call_args_list])
        self.assertIs(wrapper, cdp._LAUNCHED["process"])
        self.assertEqual(wrapper.pid, cdp._LAUNCHED["pgid"])
        self.assertTrue(os.path.isdir(cdp._LAUNCHED["profile"]))


class OwnershipTests(LaunchCase):
    """Nur der selbst gestartete Browser wird angefasst."""

    def test_without_a_launched_browser_nothing_is_closed(self):
        with patch("calibre_plugin.cdp.subprocess.Popen") as popen:
            self.assertFalse(cdp.close_grabber_window())
        popen.assert_not_called()

    def test_a_devtools_endpoint_on_another_port_is_not_adopted(self):
        with patch("calibre_plugin.cdp._http_get_json", return_value={
                "webSocketDebuggerUrl":
                "ws://127.0.0.1:9999/devtools/browser/foreign"}):
            self.assertIsNone(cdp._browser_ws_for_record({"port": 5555}))
        with patch("calibre_plugin.cdp._http_get_json", return_value={
                "webSocketDebuggerUrl":
                "ws://127.0.0.1:5555/devtools/browser/own"}):
            self.assertEqual((5555, "/devtools/browser/own"),
                             cdp._browser_ws_for_record({"port": 5555}))

    def test_closing_removes_only_the_own_profile(self):
        other = self.mkdtemp()
        profile = self.mkdtemp()
        process = FakeProcess()
        cdp._LAUNCHED.update({"process": process, "port": 5555,
                              "profile": profile})
        with patch("calibre_plugin.cdp._http_get_json", return_value=None):
            self.assertTrue(cdp.close_grabber_window())
        self.assertEqual({"process": None, "port": None, "profile": "",
                          "pgid": None}, cdp._LAUNCHED)
        self.assertFalse(os.path.exists(profile))
        self.assertTrue(os.path.isdir(other))

    def test_a_still_answering_endpoint_keeps_profile_and_owner(self):
        """Ein Wrapper kann vor seinem Browserkind enden: Profil nicht loeschen."""
        profile = self.mkdtemp()
        process = FakeProcess()
        cdp._LAUNCHED.update({"process": process, "port": 5555,
                              "profile": profile})
        version = {"webSocketDebuggerUrl":
                   "ws://127.0.0.1:5555/devtools/browser/x"}
        with patch("calibre_plugin.cdp._http_get_json", return_value=version):
            self.assertFalse(cdp.close_grabber_window(timeout=0.3))
        self.assertTrue(os.path.isdir(profile))
        self.assertIs(process, cdp._LAUNCHED["process"])


class GroupStopTests(unittest.TestCase):
    """Das Ende des Wrappers beendet nicht automatisch die Prozessgruppe."""

    def test_a_dead_wrapper_still_gets_the_saved_group_killed(self):
        wrapper = FakeProcess()
        signals, alive = [], {"orphan": False, "group": True}

        def killpg(group, sig):
            signals.append((group, sig))
            if sig == signal.SIGTERM:
                wrapper.alive = False   # der Wrapper gibt auf ...
                alive["group"] = False  # ... die Gruppe selbst ist weg ...
                alive["orphan"] = True  # ... sein Browserkind haelt den Port
            else:
                alive["orphan"] = False

        with patch("calibre_plugin.cdp.os.killpg", side_effect=killpg), \
                patch("calibre_plugin.cdp._group_alive",
                      side_effect=lambda _group: alive["group"]), \
                patch("calibre_plugin.cdp._endpoint_answers",
                      side_effect=lambda *args, **kwargs: alive["orphan"]):
            self.assertTrue(cdp._stop_launched_process(
                wrapper, timeout=1, pgid=wrapper.pid, port=5555))
        self.assertEqual([(wrapper.pid, signal.SIGTERM),
                          (wrapper.pid, signal.SIGKILL)], signals)

    def test_an_endpoint_that_keeps_answering_is_not_reported_as_stopped(self):
        wrapper = FakeProcess(exits_on=("terminate",))
        with patch("calibre_plugin.cdp.os.killpg"), \
                patch("calibre_plugin.cdp._group_alive", return_value=False), \
                patch("calibre_plugin.cdp._endpoint_answers",
                      return_value=True), \
                patch("calibre_plugin.cdp.time.monotonic",
                      side_effect=itertools.count(0, 100).__next__):
            self.assertFalse(cdp._stop_launched_process(
                wrapper, timeout=0.2, pgid=wrapper.pid, port=5555))


class FakeWs:
    """WebSocket-Doppel: liefert die vorbereiteten CDP-Antworten in Folge."""

    def __init__(self, messages=()):
        self.messages = list(messages)
        self.sent = []
        self.closed = False

    def send_json(self, obj):
        self.sent.append(obj)

    def recv_json(self, timeout=10):
        return self.messages.pop(0) if self.messages else {"id": -1}

    def close(self):
        self.closed = True


def value_message(message_id, value):
    return {"id": message_id, "result": {"result": {"value": value}}}


class EvaluateTests(unittest.TestCase):
    """Runtime.evaluate: fremde IDs, CDP-Fehler und Seitenfehler."""

    def test_a_foreign_message_is_ignored_before_the_answer(self):
        ws = FakeWs([{"id": 9, "result": {"result": {"value": "fremd"}}},
                     value_message(1, "42")])
        self.assertEqual({"ok": True, "value": "42", "exception": None},
                         cdp._evaluate_message(ws, "1+1", 1, False, 1))

    def test_cdp_errors_and_page_exceptions_are_no_value(self):
        cases = (
            ("cdp", {"id": 1, "error": {"message": "Method not found"}},
             "Method not found"),
            ("seite", {"id": 1, "result": {"exceptionDetails": {
                "exception": {"description": "ReferenceError: x"}}}},
             "ReferenceError: x"),
            ("ohne wert", {"id": 1, "result": {"result": {}}},
             "no value returned"),
        )
        for label, message, expected in cases:
            with self.subTest(label):
                result = cdp._evaluate_message(
                    FakeWs([message]), "x", 1, False, 1)
                self.assertFalse(result["ok"])
                self.assertIsNone(result["value"])
                self.assertIn(expected, result["exception"])

    def test_only_the_own_answer_counts_until_the_timeout(self):
        with patch("calibre_plugin.cdp.time.time",
                   side_effect=itertools.count(0, 100).__next__):
            result = cdp._evaluate_message(FakeWs(), "x", 1, False, 1)
        self.assertEqual("evaluate timeout", result["exception"])

    def test_the_target_connection_closes_even_on_a_page_error(self):
        ws = FakeWs([{"id": 1, "result": {"exceptionDetails": {
            "text": "boom"}}}])
        with patch("calibre_plugin.cdp._open_target", return_value=ws):
            self.assertIsNone(
                cdp._evaluate_in_target((5555, "/devtools/page/x"), "x", 1))
        self.assertTrue(ws.closed)

    def test_a_json_object_is_returned_and_junk_is_none(self):
        ws = FakeWs([value_message(1, json.dumps({"refresh": ["r"]}))])
        with patch("calibre_plugin.cdp._open_target", return_value=ws):
            self.assertEqual({"refresh": ["r"]}, cdp._evaluate_in_target(
                (5555, "/devtools/page/x"), "x", 1))
        for payload in ("kein json", "[1, 2]", None):
            with self.subTest(payload):
                ws = FakeWs([value_message(1, payload)])
                with patch("calibre_plugin.cdp._open_target", return_value=ws):
                    self.assertIsNone(cdp._evaluate_in_target(
                        (5555, "/devtools/page/x"), "x", 1))
                self.assertTrue(ws.closed)


class IdbTokenTests(unittest.TestCase):
    """IndexedDB: mehrere Datenbanken, Duplikate, eine kaputte Datenbank."""

    def test_databases_are_merged_and_duplicates_removed(self):
        ws = FakeWs([
            value_message(1, json.dumps({"refresh": ["r1", "r2"],
                                         "hardware": ["h1"]})),
            {"id": 2, "error": {"message": "DB kaputt"}},
            value_message(3, json.dumps({"refresh": ["r2"],
                                         "hardware": ["h1", "h2"]})),
        ])
        with patch("calibre_plugin.cdp._open_target", return_value=ws):
            merged = cdp.read_idb_tokens((5555, "/devtools/page/x"),
                                         ["db1", "db2", "db3"])
        self.assertEqual({"refresh": ["r1", "r2"],
                          "hardware": ["h1", "h2"]}, merged)
        self.assertTrue(ws.closed)
        self.assertEqual(3, len(ws.sent))

    def test_without_database_names_no_connection_is_opened(self):
        with patch("calibre_plugin.cdp._open_target") as opened:
            self.assertEqual({"refresh": [], "hardware": []},
                             cdp.read_idb_tokens((5555, "/x"), ["", "  "]))
        opened.assert_not_called()

    def test_the_full_grab_merges_local_storage_and_idb(self):
        with patch("calibre_plugin.cdp._evaluate_in_target",
                   return_value={"refresh": ["r1"], "idb": ["db1"]}), \
                patch("calibre_plugin.cdp.read_idb_tokens",
                      return_value={"refresh": ["r1", "r2"],
                                    "hardware": []}):
            grabbed = cdp._grab_page_tokens((5555, "/x"), 5)
        self.assertEqual({"refresh": ["r1", "r2"], "hardware": [],
                          "idb": ["db1"]}, grabbed)

    def test_a_database_name_is_embedded_as_a_json_string(self):
        name = 'db"); alert(1); ("'
        expression = cdp_scripts.idb_expression(name)
        self.assertIn(json.dumps(name), expression)
        self.assertNotIn("__DB_NAME__", expression)


class ReaderTargetTests(unittest.TestCase):
    """Der Reader-Tab braucht HTTPS, den tolino-Host und eine Target-ID."""

    def target(self, *targets):
        return cdp._reader_target(list(targets), 5555)

    def test_https_tolino_page_with_an_id_is_accepted(self):
        self.assertEqual(
            ((5555, "/devtools/page/ABC"), True),
            self.target({"type": "page",
                         "url": "https://www.mytolino.com/reader",
                         "id": "ABC"}))

    def test_foreign_hosts_plain_http_and_missing_ids_are_rejected(self):
        cases = (
            ("http", {"type": "page", "url": "http://mytolino.com/reader",
                      "id": "HTTP"}, (None, False)),
            ("fremder Host", {"type": "page",
                              "url": "https://mytolino.com.evil.test/",
                              "id": "EVIL"}, (None, False)),
            ("nur im Pfad", {"type": "page",
                             "url": "https://evil.test/?next=mytolino.com",
                             "id": "PATH"}, (None, False)),
            ("kein Tab", {"type": "other",
                          "url": "https://mytolino.com/reader",
                          "id": "WORKER"}, (None, False)),
            ("ohne ID", {"type": "page",
                         "url": "https://mytolino.com/reader"},
             (None, True)),
        )
        for label, target, expected in cases:
            with self.subTest(label):
                self.assertEqual(expected, self.target(target))

    def test_a_malformed_target_list_is_no_reader(self):
        self.assertEqual((None, False), cdp._reader_target(None, 5555))
        self.assertEqual((None, False), cdp._reader_target(["kaputt"], 5555))

"""Lebenszyklus des Anmeldefensters ohne echten Browser.

Prozess, DevTools-Port und Profilordner sind Doubles bzw. echte temporaere
Verzeichnisse; die Zustandsuebergaenge (starten, warten, besitzen,
schliessen) laufen im Produktivcode.
"""
import itertools
import os
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .. import cdp
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
        cdp._LAUNCHED.update({"process": None, "port": None, "profile": ""})
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
                      side_effect=[None, {"ok": 1}]):
            cdp.launch_reader_window()
        self.assertIs(alive, cdp._LAUNCHED["process"])

    def test_when_every_candidate_fails_no_owner_or_profile_remains(self):
        patches = self.isolate([["/a"], ["/b"]],
                               side_effect=OSError("nicht startbar"))
        with patches[0], patches[1], patches[2]:
            with self.assertRaises(TolinoAuthError) as caught:
                cdp.launch_reader_window()
        self.assertEqual("B01", caught.exception.code)
        self.assertEqual({"process": None, "port": None, "profile": ""},
                         cdp._LAUNCHED)
        self.assertTrue(self.created)
        self.assertFalse(any(os.path.exists(path) for path in self.created))

    def test_a_stuck_process_does_not_start_a_second_browser(self):
        """Sonst laese der naechste Kandidat die Antwort des alten Prozesses."""
        stuck = FakeProcess(exits_on=())
        patches = self.isolate([["/haengt"], ["/usr/bin/chromium"]],
                               return_value=stuck)
        with patches[0], patches[1] as popen, patches[2], \
                patch("calibre_plugin.cdp._http_get_json", return_value=None), \
                patch("calibre_plugin.cdp.time.time",
                      side_effect=itertools.count(0, 100).__next__):
            with self.assertRaises(TolinoAuthError) as caught:
                cdp.launch_reader_window()
        self.assertEqual(1, popen.call_count)
        self.assertIn("nicht beenden", str(caught.exception))
        # Der laufende Prozess bleibt als eigener Besitzer vermerkt und sein
        # Profil bleibt, solange er es benutzt.
        self.assertIs(stuck, cdp._LAUNCHED["process"])
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
        self.assertEqual({"process": None, "port": None, "profile": ""},
                         cdp._LAUNCHED)
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


if __name__ == "__main__":
    unittest.main()

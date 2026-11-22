"""Browser-Erkennung an kontrollierten Systemgrenzen.

Kein Test startet einen Browser: ``shutil.which``, ``subprocess.run``, die
Dateipruefungen und (fuer Windows) ein Registry-Doppel werden ersetzt. Die
Auswahl- und Deduplizierungslogik laeuft echt.
"""
import os
import types
import unittest
from unittest.mock import patch

from ..browser_finder import (missing_browser_error, _collect_candidates,
                              _macos_browser_paths, _windows_browser_paths)

EXTRA = "/opt/google/chrome/chrome"


def which_for(mapping):
    """``shutil.which`` aus einem Namens-Dict; Unbekanntes fehlt."""
    return lambda name: mapping.get(name)


def run_result(stdout="", returncode=0):
    return types.SimpleNamespace(stdout=stdout, returncode=returncode)


class LinuxCandidateTests(unittest.TestCase):
    """PATH, Zusatzpfade und Duplikate -- ohne installierten Browser."""

    def test_path_and_extra_paths_are_merged_without_duplicates(self):
        mapping = {"chrome": EXTRA, "chromium": "/usr/bin/chromium"}
        with patch("shutil.which", which_for(mapping)), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=False), \
                patch("os.path.isfile", side_effect=lambda path: path == EXTRA), \
                patch("os.access", return_value=True), \
                patch("os.path.isdir", return_value=False):
            found, status = _collect_candidates(home="/home/test")
        self.assertEqual("", status)
        self.assertEqual([[EXTRA], ["/usr/bin/chromium"]], found)

    def test_flatpak_and_snap_user_directories_add_launchers(self):
        def isdir(path):
            return (path == "/home/test/.var/app"
                    or path.startswith("/home/test/.var/app/")
                    or path.endswith("chromium"))

        with patch("shutil.which",
                   which_for({"flatpak": "/usr/bin/flatpak",
                              "snap": "/usr/bin/snap"})), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=False), \
                patch("os.path.isfile", return_value=False), \
                patch("os.access", return_value=False), \
                patch("os.path.isdir", side_effect=isdir):
            found, _status = _collect_candidates(home="/home/test")
        self.assertIn(["/usr/bin/flatpak", "run", "--command=chrome",
                       "org.chromium.Chromium"], found)
        self.assertIn(["/usr/bin/snap", "run", "chromium"], found)


class FlatpakTests(unittest.TestCase):
    """Die Host-Sonde laeuft nur in der Sandbox und nur mit flatpak-spawn."""

    def test_host_launchers_are_composed_only_inside_the_sandbox(self):
        output = "NATIVE /usr/bin/chromium\nFLATPAK com.brave.Browser\n"
        with patch("shutil.which",
                   which_for({"flatpak-spawn": "/usr/bin/flatpak-spawn"})), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=True), \
                patch("subprocess.run", return_value=run_result(output)) as run, \
                patch("os.path.isfile", return_value=False), \
                patch("os.access", return_value=False), \
                patch("os.path.isdir", return_value=False):
            found, status = _collect_candidates(home="/home/test")
        self.assertEqual("ok", status)
        self.assertIn(["/usr/bin/flatpak-spawn", "--host", "/usr/bin/chromium"],
                      found)
        self.assertIn(["/usr/bin/flatpak-spawn", "--host", "flatpak", "run",
                       "--command=brave", "com.brave.Browser"], found)
        # Der Host-Aufruf fragt die erlaubten Launcher ab, nie ein Profil.
        script = run.call_args[0][0][-1]
        self.assertIn("org.chromium.Chromium", script)
        self.assertNotIn("--user-data-dir", script)

    def test_without_the_sandbox_no_host_command_runs(self):
        with patch("shutil.which", which_for({"chromium": "/usr/bin/chromium"})), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=False), \
                patch("subprocess.run") as run, \
                patch("os.path.isfile", return_value=False), \
                patch("os.access", return_value=False), \
                patch("os.path.isdir", return_value=False):
            _found, status = _collect_candidates(home="/home/test")
        self.assertEqual("", status)
        run.assert_not_called()

    def test_a_missing_flatpak_spawn_is_reported(self):
        with patch("shutil.which", which_for({})), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=True), \
                patch("os.path.isfile", return_value=False), \
                patch("os.access", return_value=False), \
                patch("os.path.isdir", return_value=False):
            found, status = _collect_candidates(home="/home/test")
        self.assertEqual([], found)
        self.assertEqual("unavailable", status)

    def test_a_denied_probe_is_not_an_empty_library(self):
        with patch("shutil.which",
                   which_for({"flatpak-spawn": "/usr/bin/flatpak-spawn"})), \
                patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                      return_value=True), \
                patch("subprocess.run",
                      return_value=run_result("", returncode=1)), \
                patch("os.path.isfile", return_value=False), \
                patch("os.access", return_value=False), \
                patch("os.path.isdir", return_value=False):
            found, status = _collect_candidates(home="/home/test")
        self.assertEqual([], found)
        self.assertEqual("denied", status)


class FakeKey(tuple):
    """Registry-Schluessel: (Root, View, Executable) als Identitaet."""


class FakeWinreg:
    """Minimales ``winreg``: App-Paths je Root und View."""

    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2
    KEY_READ = 0x20019
    KEY_WOW64_64KEY = 0x100
    KEY_WOW64_32KEY = 0x200

    def __init__(self, entries):
        self.entries = entries
        self.opened = []

    def OpenKey(self, root, path, _reserved, access):
        executable = path.rsplit("\\", 1)[-1]
        view = access & (self.KEY_WOW64_64KEY | self.KEY_WOW64_32KEY)
        key = FakeKey((root, view, executable))
        if key not in self.entries:
            raise OSError(2, "not found")
        self.opened.append(key)
        return key

    def QueryValueEx(self, key, _name):
        value = self.entries[key]
        if value is None:
            raise OSError(2, "no default value")
        return value, 1

    def CloseKey(self, _key):
        return None


class WindowsTests(unittest.TestCase):
    """Nur registrierte, tatsaechlich vorhandene Programme zaehlen."""

    CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
    EDGE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

    def registry(self):
        hkcu, hklm = FakeWinreg.HKEY_CURRENT_USER, FakeWinreg.HKEY_LOCAL_MACHINE
        view64 = FakeWinreg.KEY_WOW64_64KEY
        view32 = FakeWinreg.KEY_WOW64_32KEY
        return FakeWinreg({
            # Dieselbe Installation in beiden Views: nur einmal melden.
            (hkcu, view64, "chrome.exe"): '"%s"' % self.CHROME,
            (hkcu, view32, "chrome.exe"): '"%s"' % self.CHROME,
            (hklm, 0, "msedge.exe"): self.EDGE,
            # Eintrag ohne Standardwert: ueberspringen, nicht abstuerzen.
            (hklm, view32, "brave.exe"): None,
            # Registriert, aber nicht mehr vorhanden: nicht melden.
            (hkcu, 0, "opera.exe"): r"C:\Gone\opera.exe",
        })

    def test_registered_browsers_are_deduplicated_and_checked(self):
        def exists(path):
            return os.path.normcase(path) in (os.path.normcase(self.CHROME),
                                              os.path.normcase(self.EDGE))

        with patch("os.path.isfile", side_effect=exists):
            found = _windows_browser_paths(self.registry())
        self.assertEqual([[self.CHROME], [self.EDGE]], found)

    def test_a_missing_registry_entry_is_simply_absent(self):
        with patch("os.path.isfile", return_value=False):
            self.assertEqual([], _windows_browser_paths(FakeWinreg({})))


class MacTests(unittest.TestCase):
    """System- und Benutzer-Programme, je relativem Pfad einmal."""

    def test_system_and_user_applications_are_deduplicated(self):
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        brave = ("/Users/test/Applications/Brave Browser.app/Contents/MacOS/"
                 "Brave Browser")

        def exists(path):
            return path in (chrome, brave)

        with patch("os.path.isfile", side_effect=exists):
            found = _macos_browser_paths(home="/Users/test")
        self.assertEqual([[chrome], [brave]], found)


class ErrorMessageTests(unittest.TestCase):
    """Die Meldung nennt den Grund, den die Sonde geliefert hat."""

    def test_messages_match_the_probe_status(self):
        cases = (
            ("no browser", False, "", "installieren"),
            ("flatpak without spawn", True, "unavailable", "flatpak update"),
            ("flatpak permission", True, "denied", "flatpak override"),
        )
        for label, sandbox, status, expected in cases:
            with self.subTest(label):
                with patch("calibre_plugin.browser_finder.in_flatpak_sandbox",
                           return_value=sandbox):
                    message = missing_browser_error(status)
                self.assertIn(expected, message)

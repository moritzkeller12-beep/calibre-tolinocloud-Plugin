"""Richtlinie fuer Test-Attrappen: patch nur an echten Systemgrenzen.

Diese Datei ist selbst ein Test: sie liest die Testmodule und schlaegt fehl,
sobald ein Test Plugin-Logik wegpatcht. Erlaubt ist nur, was ausserhalb des
Plugins liegt -- Netzwerk/HTTP, Browser/Prozess, Qt-Oberflaeche, Dateisystem,
Uhrzeit, Logging und Calibres Einstellungsspeicher.
"""

import ast
import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Ziele ausserhalb des Plugins: Modulname oder Funktionsname.
ALLOWED_PATTERNS = (
    r"^calibre_plugin\.tolino\.",          # HTTP-Client (Netz)
    r"^calibre_plugin\.cdp",               # Browser-Fenster/DevTools
    r"^(urllib|http|ssl|socket|subprocess|shutil|os|sys|time|datetime|"
    r"logging|copy|json|re|zipfile|pathlib|io)\.",
    r"^(qt|PyQt5|PyQt6|calibre)\.",
    r"\.time\.(monotonic|time|sleep)$",   # Uhr
)

# Funktionsnamen, die eine Systemgrenze darstellen (egal aus welchem Modul).
# Die Liste nennt genau die Ziele, die die Suite heute braucht: ein neuer
# Name ist eine bewusste Entscheidung und laeuft nicht still mit.
ALLOWED_NAMES = frozenset((
    "TolinoClient", "login",           # Netz/Anmeldung (TOKEN_VAULT.login)
    "check_stored_login",              # Zugangspruefung (Netz)
    "urlopen", "_http_get_json",       # HTTP
    "Popen", "which", "find_browsers", "in_flatpak_sandbox",  # Browser
    "info_dialog", "error_dialog",     # modale Qt-Dialoge
    "_aes",                            # optionale Krypto-Abhaengigkeit
    "_NEW_TOKEN_INTERVAL", "_WINDOW_CHECK_INTERVAL",  # Anmelde-Zeitfenster
))

SKIP_FILES = {"patch_policy_tests.py"}


def _targets(node):
    """Alle Patch-Ziele eines Tests."""
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = getattr(child.func, "attr", None) or getattr(child.func, "id", None)
        if name == "patch":
            for arg in child.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    found.append(arg.value)
        elif (name == "object" and isinstance(child.func, ast.Attribute)
              and getattr(child.func.value, "id", None) == "patch"):
            # ``patch.object(modul, "name")``: das gepatchte Attribut ist das
            # Ziel. Frueher wurde diese Form gar nicht geprueft.
            if (len(child.args) >= 2
                    and isinstance(child.args[1], ast.Constant)):
                found.append(str(child.args[1].value))
    return found


def _allowed(target):
    last = target.rsplit(".", 1)[-1]
    if last in ALLOWED_NAMES:
        return True
    return any(re.search(pattern, target) for pattern in ALLOWED_PATTERNS)


def offenders():
    """(Datei, Test, Ziel) fuer jeden Patch auf Plugin-Logik."""
    found = []
    for filename in sorted(os.listdir(TESTS_DIR)):
        if not filename.endswith("_tests.py") or filename in SKIP_FILES:
            continue
        with open(os.path.join(TESTS_DIR, filename)) as handle:
            tree = ast.parse(handle.read(), filename)
        functions = [n for n in tree.body if isinstance(n, ast.FunctionDef)]
        for cls in [n for n in tree.body if isinstance(n, ast.ClassDef)]:
            functions.extend(n for n in cls.body if isinstance(n, ast.FunctionDef))
        for func in functions:
            # Auch Helfer zaehlen: ein Patch im Helfer umgeht die Regel sonst.
            for target in _targets(func):
                if not _allowed(target):
                    found.append((filename, func.name, target))
    return found


class PatchPolicyTests(unittest.TestCase):
    """Kein Test darf die Logik wegpatchen, die er pruefen soll."""

    def test_only_system_boundaries_are_patched(self):
        bad = offenders()
        self.assertEqual(
            [], bad,
            "Diese Tests patchen Plugin-Logik statt sie auszufuehren:\n"
            + "\n".join("  %s::%s -> %s" % item for item in bad))

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
    r"^fcntl\.flock$",                    # Dateisperre
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
    "grab_live_refresh",               # interaktiver Browser-Login
    "info_dialog", "error_dialog",     # modale Qt-Dialoge
    "getText",                         # modaler Eingabedialog (Qt)
    "_aes",                            # optionale Krypto-Abhaengigkeit
    "_NEW_TOKEN_INTERVAL", "_WINDOW_CHECK_INTERVAL",  # Anmelde-Zeitfenster
))

SKIP_FILES = {"patch_policy_tests.py"}


def _call_name(call):
    """Qualifizierter Name eines Aufrufs, etwa ``mock.patch``."""
    parts, node = [], call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _patch_member(name):
    """``patch``, ``patch.object`` oder ``patch.multiple`` -- sonst None.

    Der Modul-Alias (``mock.patch``) ist bewusst egal: entscheidend ist das
    Glied ``patch`` und die jeweils verwendete Form.
    """
    parts = name.split(".")
    if len(parts) >= 2 and parts[-1] == "patch":
        return "patch"
    if len(parts) >= 2 and parts[-2] == "patch" and parts[-1] in (
            "object", "multiple"):
        return parts[-1]
    return None


def _patch_aliases(tree):
    """Explizite Import-Aliase fuer ``patch`` (etwa ``mock_patch``)."""
    aliases = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "patch":
                    aliases.add(alias.asname or alias.name)
    return aliases


def _argument(value):
    """Konstanter String-Wert eines Zielarguments, sonst None."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        return value.value
    return None


def _targets(node, aliases=frozenset()):
    """Zielargumente aller Patch-Aufrufe eines Tests.

    Nur das jeweilige Ziel zaehlt: ``patch("os.name", "nt")`` patcht
    ``os.name`` -- der Ersatzwert ``"nt"`` ist kein Ziel.
    """
    found = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        name = _call_name(child)
        member = _patch_member(name)
        if member is None:
            root, _, suffix = name.partition(".")
            if root in aliases:
                member = {"": "patch", "object": "object",
                          "multiple": "multiple"}.get(suffix)
        if member == "patch":
            args = child.args[:1] + [
                keyword.value for keyword in child.keywords
                if keyword.arg == "target"]
        elif member == "object":
            # ``patch.object(modul, "name")``: das Attribut ist das Ziel.
            args = child.args[1:2] + [
                keyword.value for keyword in child.keywords
                if keyword.arg == "attribute"]
        elif member == "multiple":
            args = child.args[:1] + [
                keyword.value for keyword in child.keywords
                if keyword.arg == "target"]
        else:
            continue
        found.extend(item for item in map(_argument, args) if item)
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
        aliases = _patch_aliases(tree)
        for func in functions:
            # Auch Helfer zaehlen: ein Patch im Helfer umgeht die Regel sonst.
            for target in _targets(func, aliases):
                if not _allowed(target):
                    found.append((filename, func.name, target))
    return found


class PatchPolicyTests(unittest.TestCase):
    """Kein Test darf die Logik wegpatchen, die er pruefen soll."""

    def test_detects_direct_aliased_and_qualified_patch_members(self):
        tree = ast.parse("""
from unittest.mock import patch, patch as replace
patch.object(module, "direct_object")
patch.multiple("direct_multiple", value=1)
replace.object(module, attribute="alias_object")
replace.multiple(target="alias_multiple", value=1)
mock.patch.object(module, "qualified_object")
mock.patch.multiple("qualified_multiple", value=1)
""")
        self.assertEqual(
            ["direct_object", "direct_multiple", "alias_object",
             "alias_multiple", "qualified_object", "qualified_multiple"],
            _targets(tree, _patch_aliases(tree)))

    def test_only_system_boundaries_are_patched(self):
        bad = offenders()
        self.assertEqual(
            [], bad,
            "Diese Tests patchen Plugin-Logik statt sie auszufuehren:\n"
            + "\n".join("  %s::%s -> %s" % item for item in bad))

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
    r"logging|copy|json|re|zipfile|pathlib|io)(\.|$)",
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
    "grab_live_refresh",               # Browserfenster (Anmeldung)
    "_lock_handle", "_read_frame",     # Sperrdatei (Dateisystem), Rahmen
    "_aes",                            # optionale Krypto-Abhaengigkeit
    "_NEW_TOKEN_INTERVAL", "_WINDOW_CHECK_INTERVAL",  # Anmelde-Zeitfenster
))

SKIP_FILES = {"patch_policy_tests.py"}

# Steuerargumente von ``patch.multiple`` sind keine Attribute des Moduls.
_MULTIPLE_CONTROLS = frozenset(("spec", "create", "spec_set", "autospec",
                                "new_callable"))


def _call_name(call):
    """Qualifizierter Name eines Aufrufs, etwa ``mock.patch``."""
    parts, node = [], call.func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _patch_member(name, aliases=frozenset()):
    """``patch``, ``patch.object`` oder ``patch.multiple`` -- sonst None.

    Der Modul-Alias (``mock.patch``) ist bewusst egal: entscheidend ist das
    Glied ``patch`` und die jeweils verwendete Form. Direkt importiert
    (``from unittest.mock import patch``) fehlt das Modul davor; dann traegt
    der Import-Alias die Erkennung.
    """
    parts = name.split(".")
    if parts[-1] == "patch":
        return "patch"
    if (parts[-1] in ("object", "multiple") and len(parts) >= 2
            and (parts[-2] == "patch" or parts[-2] in aliases)):
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


def _module_name(node):
    """Modulname, wenn der Zielausdruck ein einfacher Name ist -- sonst leer."""
    return node.id if isinstance(node, ast.Name) else ""


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
        member = _patch_member(name, aliases)
        if member is None and name in aliases:
            member = "patch"
        if member == "patch":
            args = child.args[:1] + [
                keyword.value for keyword in child.keywords
                if keyword.arg == "target"]
            found.extend(item for item in map(_argument, args) if item)
        elif member == "object":
            # ``patch.object(modul, "name")``: das Attribut ist das Ziel.
            # Ein einfacher Modulname qualifiziert es, damit ``os.name``
            # erlaubt bleibt; bei anderem Ausdruck zaehlt nur das Attribut.
            module = _module_name(child.args[0]) if child.args else ""
            attributes = list(map(_argument, child.args[1:2])) + [
                _argument(keyword.value) for keyword in child.keywords
                if keyword.arg == "attribute"]
            found.extend(module + "." + item if module else item
                         for item in attributes if item)
        elif member == "multiple":
            # ``patch.multiple(modul, name=...)``: das Modul und jedes
            # gepatchte Attribut sind Ziele, Steuerargumente nicht.
            module = next((_argument(item) for item in child.args[:1]
                           if _argument(item)), "")
            if not module and child.args:
                # ``patch.multiple(module, name=...)`` mit einem Modulobjekt
                # statt eines Import-Strings, wie bei ``patch.object``.
                module = _module_name(child.args[0])
            module = module or next(
                (_argument(keyword.value) for keyword in child.keywords
                 if keyword.arg == "target"), "")
            if module:
                found.append(module)
            found.extend("%s.%s" % (module, keyword.arg)
                         for keyword in child.keywords
                         if module and keyword.arg
                         and keyword.arg != "target"
                         and keyword.arg not in _MULTIPLE_CONTROLS)
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
            ["module.direct_object", "direct_multiple",
             "direct_multiple.value", "module.alias_object",
             "alias_multiple", "alias_multiple.value",
             "module.qualified_object", "qualified_multiple",
             "qualified_multiple.value"],
            _targets(tree, _patch_aliases(tree)))

    def test_module_level_patches_on_os_are_allowed(self):
        """``os.name`` bleibt erlaubt; die Steuerargumente zaehlen nicht."""
        tree = ast.parse(
            'patch.multiple("os", name="nt", spec=True)\n'
            'patch.object(os, "name", "nt")\n')
        targets = _targets(tree, _patch_aliases(tree))
        self.assertEqual(["os", "os.name", "os.name"], targets)
        self.assertEqual([], [item for item in targets if not _allowed(item)])

    def test_patch_multiple_on_a_module_object_is_still_a_verdict(self):
        """Ein Modulobjekt statt String darf die gepatchte Funktion nicht
        verstecken; Steuerargumente bleiben aussen vor."""
        tree = ast.parse(
            "patch.multiple(module, interne_funktion=ersatz, spec=True)\n")
        targets = _targets(tree, _patch_aliases(tree))
        self.assertEqual(["module", "module.interne_funktion"], targets)
        self.assertFalse(_allowed("module.interne_funktion"))

    def test_only_system_boundaries_are_patched(self):
        bad = offenders()
        self.assertEqual(
            [], bad,
            "Diese Tests patchen Plugin-Logik statt sie auszufuehren:\n"
            + "\n".join("  %s::%s -> %s" % item for item in bad))

"""Richtlinie fuer Test-Attrappen: patch nur an echten Systemgrenzen.

Diese Datei ist selbst ein Test: sie liest die Testmodule und schlaegt fehl,
sobald ein Test Plugin-Logik wegpatcht. Erlaubt ist nur, was ausserhalb des
Plugins liegt -- Netzwerk/HTTP, Browser/Prozess, Qt-Oberflaeche, Dateisystem,
Uhrzeit, Logging und Calibres Einstellungsspeicher.
"""

import ast
import os
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))

# Nur diese Namen unter ``calibre_plugin.*`` sind echte Grenzen (Netz,
# Browser, Prozess, Qt, Dateisystem, Logging). Ziele ausserhalb des Plugins
# sind immer erlaubt; alles andere im Plugin ist Logik und muss laufen.
ALLOWED_PLUGIN_TARGETS = frozenset("""
    calibre_plugin.browser_finder.in_flatpak_sandbox
    calibre_plugin.browser_login.TolinoClient
    calibre_plugin.browser_login._NEW_TOKEN_INTERVAL
    calibre_plugin.browser_login._WINDOW_CHECK_INTERVAL
    calibre_plugin.cdp._endpoint_answers calibre_plugin.cdp._evaluate_in_target
    calibre_plugin.cdp._group_alive calibre_plugin.cdp._http_get_json
    calibre_plugin.cdp._open_target calibre_plugin.cdp._wait_for_debug_port
    calibre_plugin.cdp.browser_finder.find_browsers
    calibre_plugin.cdp.close_grabber_window
    calibre_plugin.cdp.grab_once_from_grabber
    calibre_plugin.cdp.grabber_window_state
    calibre_plugin.cdp.launch_reader_window calibre_plugin.cdp.os.killpg
    calibre_plugin.cdp.read_idb_tokens calibre_plugin.cdp.subprocess.Popen
    calibre_plugin.cdp.tempfile.mkdtemp calibre_plugin.cdp.time.monotonic
    calibre_plugin.cdp.time.time calibre_plugin.cdp_transport.socket.create_connection
    calibre_plugin.sync_results.error_dialog
    calibre_plugin.sync_results.info_dialog
    calibre_plugin.tolino._DOWNLOAD_OPENER calibre_plugin.tolino.log_warning
    calibre_plugin.tolino.time.sleep calibre_plugin.ui_dashboard.error_dialog
    calibre_plugin.ui_dashboard_parts.info_dialog
    calibre_plugin.ui_dialogs.error_dialog calibre_plugin.ui_dialogs.info_dialog
    calibre_plugin.ui_workers.TOKEN_VAULT.login
""".split())

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
    """Plugin-Logik ist tabu; fremde Systemgrenzen brauchen keinen Eintrag."""
    return (not target.startswith("calibre_plugin.")
            or target in ALLOWED_PLUGIN_TARGETS)


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
        tree = ast.parse("patch.multiple(calibre_plugin, fremd=ersatz,\n"
                         "                spec=True)\n")
        targets = _targets(tree, _patch_aliases(tree))
        self.assertEqual(["calibre_plugin", "calibre_plugin.fremd"], targets)
        self.assertFalse(_allowed("calibre_plugin.fremd"))

    def test_only_system_boundaries_are_patched(self):
        bad = offenders()
        self.assertEqual(
            [], bad,
            "Diese Tests patchen Plugin-Logik statt sie auszufuehren:\n"
            + "\n".join("  %s::%s -> %s" % item for item in bad))

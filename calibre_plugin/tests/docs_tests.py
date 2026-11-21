"""Die Fehlercodeliste und der Quelltext bleiben zusammen.

Jede Meldung vergibt ihren Code im Quelltext; `docs/fehlercodes.md` ist die
Tabelle für den Nutzer. Ein neuer Code ohne Zeile dort fällt hier auf.
"""
import ast
import os
import unittest

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(os.path.dirname(SOURCE_DIR), "docs", "fehlercodes.md")


def error_codes(tree):
    """Konstante ``code=``-Werte aus Fehleraufrufen -- unabhaengig von Quotes.

    Ein Regex auf den Text wuerde einfache/doppelte Anfuehrungszeichen und
    Leerzeichen unterscheiden; der Syntaxbaum kennt nur den Wert.
    """
    codes = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = (getattr(node.func, "attr", None)
                or getattr(node.func, "id", None))
        if not (isinstance(name, str) and name.endswith("Error")):
            continue
        for keyword in node.keywords:
            value = keyword.value
            if (keyword.arg == "code" and isinstance(value, ast.Constant)
                    and isinstance(value.value, str)):
                codes.add(value.value)
    return codes


class ErrorCodeDocsTests(unittest.TestCase):
    """Kein Code im Quelltext ohne Zeile in der Liste."""

    def test_every_code_in_the_source_is_documented(self):
        codes = set()
        for name in sorted(os.listdir(SOURCE_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(SOURCE_DIR, name),
                      encoding="utf-8") as handle:
                codes.update(error_codes(ast.parse(handle.read(), name)))
        with open(DOCS, encoding="utf-8") as handle:
            docs = handle.read()
        self.assertTrue(codes)
        self.assertEqual([], sorted(code for code in codes
                                    if "| %s |" % code not in docs))

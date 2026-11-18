"""Die Fehlercodeliste und der Quelltext bleiben zusammen.

Jede Meldung vergibt ihren Code im Quelltext; `docs/fehlercodes.md` ist die
Tabelle für den Nutzer. Ein neuer Code ohne Zeile dort fällt hier auf.
"""
import os
import re
import unittest

SOURCE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(os.path.dirname(SOURCE_DIR), "docs", "fehlercodes.md")
CODE = re.compile(r'code="([A-Z][0-9]{2})"')


class ErrorCodeDocsTests(unittest.TestCase):
    """Kein Code im Quelltext ohne Zeile in der Liste."""

    def test_every_code_in_the_source_is_documented(self):
        codes = set()
        for name in sorted(os.listdir(SOURCE_DIR)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(SOURCE_DIR, name), encoding="utf-8") as handle:
                codes.update(CODE.findall(handle.read()))
        with open(DOCS, encoding="utf-8") as handle:
            docs = handle.read()
        self.assertTrue(codes)
        self.assertEqual([], sorted(code for code in codes
                                    if "| %s |" % code not in docs))

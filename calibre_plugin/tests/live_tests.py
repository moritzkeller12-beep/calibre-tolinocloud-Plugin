"""Echte tolino-Aufrufe gegen die Cloud -- nur mit Project-Secrets.

Die Zugangsdaten stehen in den Project-Secrets ``refresh_token`` und
``hardware_id`` (siehe ``secrets_support``); ohne sie wird uebersprungen.
Geprueft werden ausschliesslich lesende Aufrufe: Upload, Loeschen,
Markierungen und Metadaten wuerden das echte Konto veraendern.

Der Refresh-Token ist einmalig: Jeder Lauf loescht ihn beim Anbieter und
erhaelt einen neuen. Das Secret muss danach neu hinterlegt werden -- oder
man meldet sich im Plugin ueber das Anmeldefenster an, das den rotierten
Token selbst im Konto speichert.
"""
import os
import unittest

from ..errors import TolinoAuthError
from ..partners import PARTNERS
from ..tolino import TolinoClient
from .secrets_support import credentials_from_env

try:
    TOKEN, HARDWARE = credentials_from_env()
except TolinoAuthError as exc:
    raise unittest.SkipTest("Project-Secrets fehlen: %s" % exc)

def _partner_id():
    """Buchhaendler des Kontos (Standard Thalia.de); unbekannt faellt auf 1."""
    try:
        partner = int(os.environ.get("tolino_partner_id") or 1)
    except ValueError:
        partner = 1
    return partner if partner in PARTNERS else 1


PARTNER_ID = _partner_id()


class LiveCloudTests(unittest.TestCase):
    """Lesender Live-Lauf gegen genau ein Konto."""

    @classmethod
    def setUpClass(cls):
        cls.client = TolinoClient(PARTNER_ID, HARDWARE, refresh=TOKEN)
        cls.client.login()

    def test_login_liefert_einen_zugang(self):
        self.assertTrue(self.client.access)
        self.assertGreater(self.client.expires_at, 0)

    def test_inventar_liefert_normalisierte_datensaetze(self):
        records = self.client.inventory()
        self.assertIsInstance(records, list)
        for record in records:
            self.assertEqual({"tolino_id", "title", "authors", "isbn"},
                             set(record))

    def test_lesestatus_und_fortschritt_sind_abbildungen(self):
        self.assertIsInstance(self.client.read_ids(), set)
        self.assertIsInstance(self.client.progress_by_id(), dict)

    def test_sammlungen_sind_paare(self):
        for pair in self.client.collection_pairs():
            self.assertIsInstance(pair, tuple)
            self.assertEqual(2, len(pair))


if __name__ == "__main__":
    unittest.main()

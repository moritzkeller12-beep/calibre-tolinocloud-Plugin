"""Manueller Eingabepfad: Geräte-ID und Zugangstoken in den Einstellungen.

Der Ablauf läuft über die echten Dialogmethoden (`close_settings`,
`store_refresh`, `_save_visible_account`); nur die HTTP-Grenze des
abgelehnten Tokens ist auf einen lokalen Server umgelegt.
"""
import types
import unittest
from unittest.mock import patch

from ._support import (ComboStub, LocalHTTP, dashboard_with_widgets,
                       jwt_token, local_tolino_endpoints, ui_module_for)
from .. import config
from ..partners import PARTNERS
from ..tolino import TolinoAuthError, normalize_hardware_id, normalize_refresh_token

AT = "https://www.thalia.at/auth/oauth2/token"
HEX32 = "0123456789abcdef0123456789abcdef"
UUID = "01234567-89ab-cdef-0123-456789abcdef"


class ManualEntryTests(unittest.TestCase):
    """Geräte-ID und Token direkt in den Einstellungen eintippen."""

    def setUp(self):
        self.ui_module = ui_module_for(self)
        self.original = (config.PREFERENCES,
                         self.ui_module.dashboard.hardware_id)
        self.addCleanup(self._restore)

    def _restore(self):
        config.PREFERENCES, self.ui_module.dashboard.hardware_id = self.original

    def _dashboard(self, token="", hardware="hw-1", partner=4):
        config.PREFERENCES = config.JSONConfig("test")
        config.PREFERENCES.update({
            "accounts": [{"name": "konto", "partner_id": partner,
                          "hardware_id": hardware, "refresh_token": token,
                          "state": {}}],
            "active_account": "konto",
        })
        self.ui_module.dashboard.hardware_id = lambda: "auto-hw"
        self.accepted = []
        dashboard = dashboard_with_widgets(ComboStub)
        dashboard.partner.items = [(str(pid), pid) for pid in sorted(PARTNERS)]
        dashboard.settings_dialog = types.SimpleNamespace(
            accept=lambda: self.accepted.append(True))
        for name in ("load_values", "values", "_selected_account",
                     "update_status", "_stored_refresh", "_typed_refresh",
                     "_effective_refresh", "store_refresh",
                     "_save_visible_account", "_partner_selected",
                     "_mark_hardware_edited", "close_settings"):
            setattr(dashboard, name,
                    getattr(self.ui_module.dashboard.SyncDashboard,
                            name).__get__(dashboard))
        dashboard.load_values()
        return dashboard

    def test_normalisation_rules(self):
        """32 Hex -> UUID, leer -> leer (neue UUID), sonst unverändert."""
        self.assertEqual(UUID, normalize_hardware_id(HEX32))
        self.assertEqual("", normalize_hardware_id("   "))
        self.assertEqual("geraet-1", normalize_hardware_id("geraet-1"))
        self.assertEqual("abc", normalize_refresh_token('  "abc"  '))
        # Ein "Bearer "-Präfix wird NICHT entfernt.
        self.assertEqual("Bearer abc", normalize_refresh_token("Bearer abc"))

    def test_settings_save_hardware_first_then_token_with_it(self):
        """Reihenfolge: Geräte-ID, dann Token mit genau dieser ID."""
        dashboard = self._dashboard()
        token = jwt_token(iss=AT)
        dashboard.hardware.setText(HEX32)
        dashboard._mark_hardware_edited()
        dashboard.refresh.setText(token)
        order = []
        real_account = config.save_account
        real_credentials = config.save_credentials
        with patch.object(self.ui_module.dashboard, "save_account",
                          lambda *args, **kwargs: order.append("account")
                          or real_account(*args, **kwargs)), \
                patch.object(self.ui_module.dashboard, "save_credentials",
                             lambda *args, **kwargs:
                             order.append(("token",) + args)
                             or real_credentials(*args, **kwargs)):
            dashboard.close_settings()
        self.assertEqual("account", order[0])
        self.assertEqual(("token", "konto", token, UUID, 2), order[1])
        account = config.account_by_name("konto")
        self.assertEqual(UUID, account["hardware_id"])
        self.assertEqual(token, account["refresh_token"])
        self.assertEqual(2, account["partner_id"])
        self.assertEqual("", dashboard.refresh.text())
        self.assertEqual("Angemeldet.", dashboard.status.text_value)
        self.assertEqual([True], self.accepted)

    def test_empty_hardware_field_creates_a_new_uuid(self):
        dashboard = self._dashboard()
        dashboard.hardware.setText("")
        dashboard._mark_hardware_edited()
        dashboard.refresh.setText(jwt_token(iss=AT))
        dashboard.close_settings()
        account = config.account_by_name("konto")
        self.assertEqual("auto-hw", account["hardware_id"])
        self.assertEqual(2, account["partner_id"])

    def test_unknown_hardware_shape_is_sent_unchanged(self):
        dashboard = self._dashboard()
        dashboard.hardware.setText("geraet-1")
        dashboard._mark_hardware_edited()
        dashboard.close_settings()
        self.assertEqual("geraet-1",
                         config.account_by_name("konto")["hardware_id"])

    def test_quoted_token_is_stored_unquoted(self):
        """Umschließende Anführungszeichen und Leerraum fallen weg."""
        dashboard = self._dashboard()
        token = jwt_token(iss=AT)
        dashboard.refresh.setText('  "%s"  ' % token)
        dashboard.close_settings()
        account = config.account_by_name("konto")
        self.assertEqual(token, account["refresh_token"])
        self.assertEqual(2, account["partner_id"])

    def test_bearer_prefix_survives_the_storage_path(self):
        """Ein \"Bearer \"-Präfix wird nicht stillschweigend entfernt."""
        dashboard = self._dashboard()
        dashboard.partner.setCurrentIndex(dashboard.partner.findData(1))
        dashboard._partner_selected(0)
        dashboard.refresh.setText("Bearer " + jwt_token(iss=AT))
        dashboard.close_settings()
        self.assertEqual("Bearer " + jwt_token(iss=AT),
                         config.account_by_name("konto")["refresh_token"])

    def test_token_without_iss_is_refused_and_saves_nothing(self):
        dashboard = self._dashboard()
        boxes = []
        with patch.object(self.ui_module.dashboard, "error_dialog",
                          lambda *args, **kwargs: boxes.append(args)):
            dashboard.refresh.setText("kein-jwt")
            dashboard.close_settings()
        self.assertEqual("", config.account_by_name("konto")["refresh_token"])
        self.assertEqual(1, len(boxes))
        self.assertIn("Buchhändler", boxes[0][1])
        self.assertIn("kein Buchhändler erkennbar", boxes[0][2])

    def test_token_without_iss_uses_the_shop_chosen_in_the_settings(self):
        dashboard = self._dashboard()
        dashboard.partner.setCurrentIndex(dashboard.partner.findData(8))
        dashboard._partner_selected(0)
        dashboard.refresh.setText("kein-jwt")
        dashboard.close_settings()
        account = config.account_by_name("konto")
        self.assertEqual("kein-jwt", account["refresh_token"])
        self.assertEqual(8, account["partner_id"])

    def test_hardware_change_keeps_the_stored_token(self):
        dashboard = self._dashboard(token="alter-token")
        dashboard.hardware.setText("neue-id")
        dashboard._mark_hardware_edited()
        dashboard.close_settings()
        account = config.account_by_name("konto")
        self.assertEqual("alter-token", account["refresh_token"])
        self.assertEqual("neue-id", account["hardware_id"])
        self.assertEqual("Angemeldet.", dashboard.status.text_value)

    def test_rejected_token_is_removed_and_status_says_not_logged_in(self):
        """A01 (invalid_grant / not active): Token weg, Status abgemeldet."""
        from ..token_vault import TOKEN_VAULT
        for body in (b'{"error":"invalid_grant"}',
                     b'{"error":"invalid_grant","error_description":'
                     b'"Token is not active"}'):
            with self.subTest(body=body):
                dashboard = self._dashboard(token="abgelaufen")

                def reject(_method, _path, _headers, _payload, _body=body):
                    return (400, {"Content-Type": "application/json"}, _body)

                with LocalHTTP(reject) as server, \
                        local_tolino_endpoints(server.origin, partner_ids=(4,)):
                    with self.assertRaises(TolinoAuthError):
                        TOKEN_VAULT.login("konto")
                self.assertEqual("",
                                 config.account_by_name("konto")["refresh_token"])
                dashboard.load_values()
                self.assertEqual("Nicht angemeldet.",
                                 dashboard.status.text_value)


if __name__ == "__main__":
    unittest.main()

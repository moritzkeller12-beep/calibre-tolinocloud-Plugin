"""Calibre-Einstieg; die Oberfläche wird erst beim Öffnen geladen."""

from calibre.gui2.actions import InterfaceAction

__all__ = ["TolinoSyncAction"]


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("tolino Cloud Sync", None,
                   "tolino Cloud Sync öffnen", None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dashboard)
        _apply_toolbar_icon(self)
        # The keep-alive rotates the stored token periodically while Calibre
        # runs, so an adopted token survives between syncs.
        try:
            from .sessions import start_token_keepalive
            start_token_keepalive(self._keepalive_credentials,
                                  self._keepalive_persist)
        except Exception:
            pass  # keep-alive must never break plugin loading

    def _keepalive_credentials(self):
        try:
            from .config import settings
            cfg = settings()
        except Exception:
            return {}
        return {"accounts": [
            dict(item, account_name=item.get("name"))
            for item in cfg.get("accounts", ())
            if item.get("refresh_token")
        ]}

    def _keepalive_persist(self, refresh, account_name=None):
        try:
            from .config import save_account, settings
            cfg = settings()
            name = (account_name or cfg.get("active_account")
                    or cfg.get("name"))
            if name and refresh:
                save_account(name, {"refresh_token": refresh}, active=name)
        except Exception as exc:
            # Nicht still scheitern: eine unterbliebene Rotation faellt sonst
            # erst Stunden spaeter als abgelaufene Sitzung auf. Ohne Token.
            from .tolino import log_warning, sanitize_error
            log_warning("Tolino-Keep-alive konnte den neuen Zugang nicht "
                        "speichern: %s" % sanitize_error(exc, (refresh,)))

    def show_dashboard(self):
        from .ui_dashboard import SyncDashboard
        dialog = SyncDashboard(self.gui)
        dialog.exec()


def _apply_toolbar_icon(action):
    """Give the toolbar action its icon via the fallback chain."""
    try:
        from .icons import toolbar_icon
        icon = toolbar_icon()
    except Exception:
        return
    if icon is None or icon.isNull():
        return
    try:
        action.qaction.setIcon(icon)
    except Exception:
        action.setIcon(icon)

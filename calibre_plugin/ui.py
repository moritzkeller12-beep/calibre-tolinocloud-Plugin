"""Calibre-Einstieg; die Oberfläche wird erst beim Öffnen geladen."""

from calibre.gui2.actions import InterfaceAction

__all__ = ["TolinoSyncAction"]


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("tolino Cloud Sync", None,
                   "tolino Cloud Sync öffnen", None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dashboard)
        icon = self._toolbar_icon()
        if icon is not None and not icon.isNull():
            self.qaction.setIcon(icon)
        # The keep-alive rotates the stored token periodically while Calibre
        # runs, so an adopted token survives between syncs.
        try:
            from .token_vault import start_token_keepalive
            start_token_keepalive(self._keepalive_credentials)
        except Exception:
            pass  # keep-alive must never break plugin loading

    def shutdown(self):
        """Calibre beendet sich: Keep-alive anhalten, bevor nach Threads gesucht wird.

        Calibre prüft beim Herunterfahren auf laufende Threads (Phase
        «Actions shutdown»); ein atexit-Haken käme dafür zu spät.
        """
        try:
            from .token_vault import stop_token_keepalive
            stop_token_keepalive()
        except Exception:
            pass  # der Shutdown darf nie an der Aufräumung scheitern
        parent = getattr(super(), "shutdown", None)
        if callable(parent):
            parent()

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

    def show_dashboard(self):
        from .ui_dashboard import SyncDashboard
        dialog = SyncDashboard(self.gui)
        dialog.exec()

    def _toolbar_icon(self):
        """Load the plugin icon directly from the installed ZIP archive."""
        from qt.core import QIcon, QPixmap
        resources = self.load_resources(["images/tolino_cloud_sync.png"])
        data = resources.get("images/tolino_cloud_sync.png")
        if not data:
            return None
        pixmap = QPixmap()
        if not pixmap.loadFromData(data):
            return None
        return QIcon(pixmap)

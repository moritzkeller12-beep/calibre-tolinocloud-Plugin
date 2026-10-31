"""Calibre-Einstieg; die Oberfläche wird erst beim Öffnen geladen."""

from calibre.gui2.actions import InterfaceAction


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("tolino Cloud Sync", None,
                   "tolino Cloud Sync öffnen", None)

    def genesis(self):
        self._connect_quit_hook()
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

    def shutting_down(self):
        """Calibre beendet sich -- der dokumentierte Rückruf der Action.

        Calibre prüft beim Herunterfahren auf laufende Threads, deshalb muss
        der Keep-alive hier enden und nicht erst in einem atexit-Haken.
        """
        self._finish_session()
        parent = getattr(super(), "shutting_down", None)
        if callable(parent):
            parent()

    def _finish_session(self):
        """Keep-alive anhalten -- genau einmal."""
        if getattr(self, "_tolino_cleanup_done", False):
            return
        self._tolino_cleanup_done = True
        try:
            from .token_vault import stop_token_keepalive
            stop_token_keepalive()
        except Exception:
            pass  # der Shutdown darf nie an der Aufräumung scheitern

    def _connect_quit_hook(self):
        """Zweiter Weg: greift auch ohne den Rückruf der InterfaceAction."""
        try:
            from qt.core import QApplication
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(lambda: self._finish_session())
        except Exception:
            pass  # darf das Laden nie verhindern

    def _keepalive_credentials(self):
        try:
            from .config import settings
            cfg = settings()
        except Exception:
            return {}
        return {"accounts": [item for item in cfg.get("accounts", ())
                             if item.get("refresh_token")]}

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

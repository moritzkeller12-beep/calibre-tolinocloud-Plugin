"""Calibre-Einstieg; die Oberfläche wird erst beim Öffnen geladen."""

from calibre.gui2.actions import InterfaceAction


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
            start_token_keepalive()
        except Exception as exc:
            # Keep-alive must never break plugin loading, but a silent failure
            # makes a missing token rotation impossible to diagnose.
            from .tolino import log_warning
            log_warning("Tolino-Keep-alive konnte nicht gestartet werden: %s"
                        % exc)

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
        """Keep-alive anhalten; Calibre ruft `shutting_down` genau einmal."""
        try:
            from .token_vault import stop_token_keepalive
            stop_token_keepalive()
        except Exception as exc:
            # Der Shutdown darf nie an der Aufräumung scheitern.
            from .tolino import log_warning
            log_warning("Tolino-Keep-alive konnte nicht beendet werden: %s"
                        % exc)

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

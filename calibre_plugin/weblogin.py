"""Embedded browser login for the Tolino web reader.

Runs the partner's OAuth/Keycloak sign-in inside an embedded QtWebEngine view.
After the user signs in and the web reader loads, the tokens are read from the
page's Local Storage and persisted. No token value is ever logged.
"""
import json
import urllib.parse

try:
    from qt.core import (QDialog, QDialogButtonBox, QLabel, QTimer, QUrl,
                         QVBoxLayout)
    from qt.webengine import QWebEnginePage, QWebEngineProfile, QWebEngineView
except ImportError:  # Non-Calibre environments (tests, type checks)
    QDialog = object
    QDialogButtonBox = QLabel = QTimer = QUrl = QVBoxLayout = None
    QWebEnginePage = QWebEngineProfile = QWebEngineView = None

try:
    from .tolino import PARTNERS, TolinoAuthError, extract_login_tokens
except ImportError:
    from tolino import PARTNERS, TolinoAuthError, extract_login_tokens


READER_HOSTS = ("webreader.mytolino.com", "webreader.hugendubel.de")

_STORAGE_JS = """
(function() {
    var result = {};
    try {
        for (var i = 0; i < localStorage.length; i += 1) {
            var key = localStorage.key(i);
            result[key] = localStorage.getItem(key);
        }
    } catch (err) { result["__error__"] = String(err); }
    return JSON.stringify(result);
})()
"""


def _on_reader(url):
    host = (url.host() or "").casefold()
    return any(host == name or host.endswith("." + name) for name in READER_HOSTS)


class EmbeddedLoginDialog(QDialog):
    """Sign in to the partner OAuth page and harvest tokens from storage."""

    def __init__(self, partner_id, hardware_id_value="", parent=None):
        QDialog.__init__(self, parent)
        self.partner_id = int(partner_id)
        self.partner = PARTNERS[self.partner_id]
        self.hardware_id_value = hardware_id_value or ""
        self.refresh_token = None
        self.hardware_id = None
        self.completed = False
        self._cancel_requested = False

        self.setWindowTitle("Tolino-Anmeldung: %s" % self.partner.get("name", ""))
        self.resize(980, 760)
        layout = QVBoxLayout(self)

        self.status = QLabel(
            "Im Fenster anmelden. Nach dem Login öffnet sich der Web Reader; "
            "die Tokens werden dann automatisch übernommen.")
        layout.addWidget(self.status)

        self.profile = QWebEngineProfile(self)
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.ForcePersistentCookies)
        self.page = QWebEnginePage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll_storage)
        self.page.urlChanged.connect(self._on_url_changed)
        self._timer.start()
        self.view.load(QUrl(self._start_url()))

    def _start_url(self):
        partner = self.partner
        auth_url = partner.get("auth_url")
        params = {
            "client_id": partner.get("client_id", "webreader"),
            "response_type": "code",
            "scope": partner.get("scope", "SCOPE_BOSH"),
        }
        for key in ("x_buchde.mandant_id", "x_buchde.skin_id"):
            if partner.get(key):
                params[key] = partner[key]
        if not auth_url:
            auth_url = partner.get("reader_url") or (
                "https://%s/library/index.html" % READER_HOSTS[0])
            params = {}
        return auth_url + ("?" + urllib.parse.urlencode(params) if params else "")

    def _on_url_changed(self, url):
        if _on_reader(url):
            self.status.setText(
                "Angemeldet – Tokens werden aus dem Web Reader übernommen …")

    def _poll_storage(self):
        if self.completed or self._cancel_requested:
            return
        if not _on_reader(self.page.url()):
            return
        self.page.runJavaScript(_STORAGE_JS, 0, self._storage_ready)

    def _storage_ready(self, payload):
        if self.completed or not isinstance(payload, str):
            return
        try:
            storage = json.loads(payload)
        except ValueError:
            return
        refresh, hardware = extract_login_tokens(storage)
        if not refresh:
            return
        self.refresh_token = refresh
        self.hardware_id = hardware or self.hardware_id_value or None
        self.completed = True
        self._timer.stop()
        self.status.setText(
            "Anmeldung abgeschlossen / Sign-in complete.")
        if QTimer is not None:
            QTimer.singleShot(600, self.accept)

    def reject(self):
        self._cancel_requested = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        QDialog.reject(self)

    def closeEvent(self, event):
        self._cancel_requested = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        QDialog.closeEvent(self, event)


def embedded_login_available():
    """Whether this Calibre build can run the embedded login window."""
    return QWebEngineView is not None and QWebEngineProfile is not None


def run_embedded_login(partner_id, hardware_id_value="", parent=None):
    """Open the embedded login window and return (refresh_token, hardware_id)."""
    if not embedded_login_available():
        raise TolinoAuthError(
            "This Calibre build has no embedded browser (QtWebEngine). "
            "Use 'Token aus Browser extrahieren' instead."
        )
    dialog = EmbeddedLoginDialog(partner_id, hardware_id_value, parent)
    dialog.exec()
    if not dialog.completed or not dialog.refresh_token:
        raise TolinoAuthError(
            "Anmeldung abgebrochen, bevor Tokens übernommen werden konnten.")
    return dialog.refresh_token, dialog.hardware_id

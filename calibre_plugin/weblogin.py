"""Embedded browser login for the Tolino web reader.

Runs the partner's OAuth/Keycloak sign-in inside an embedded QtWebEngine view.
After the user signs in and the web reader loads, the tokens are read from the
page's Local Storage and persisted. No token value is ever logged.
"""
import json
import re
import urllib.parse

try:
    from qt.core import (QDialog, QDialogButtonBox, QLabel, QTimer, QUrl,
                         QVBoxLayout)
    from qt.webengine import (QWebEnginePage, QWebEngineProfile,
                              QWebEngineScript, QWebEngineView)
except ImportError:  # Non-Calibre environments (tests, type checks)
    QDialog = object
    QDialogButtonBox = QLabel = QTimer = QUrl = QVBoxLayout = None
    QWebEnginePage = QWebEngineProfile = QWebEngineScript = QWebEngineView = None

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


def _resolve_enum(owner, *paths):
    """Resolve a Qt constant across Qt5 and Qt6 enum naming schemes.

    Qt6 scopes enums (e.g. PersistentCookiesPolicy.ForcePersistentCookies)
    while Qt5 exposed flat names; PyQt6 builds vary in which shortcut exists.
    Returns None when no variant is available.
    """
    if owner is None:
        return None
    for path in paths:
        current = owner
        for part in path.split("."):
            current = getattr(current, part, None)
            if current is None:
                break
        else:
            return current
    return None


_FORCE_PERSISTENT_COOKIES = (
    "PersistentCookiesPolicy.ForcePersistentCookies",  # Qt6 scoped
    "ForcePersistentCookies",  # Qt5 flat
)
_CLOSE_BUTTON = (
    "StandardButton.Close",  # Qt6 scoped
    "Close",  # Qt5 flat
)


_QT_UA_TOKEN = re.compile(r"\s*QtWebEngine/[\w.]+", re.IGNORECASE)
LOGIN_PROFILE_STORAGE = "tolino-cloud-sync-login"
STEALTH_SCRIPT_NAME = "tolino-cloud-sync-stealth"

_STEALTH_JS = """
(function() {
    // Make the embedded Chromium look like a regular browser to bot
    // protection scripts (DataDome etc.). Every step is wrapped so a
    // failing spoof never breaks page functionality.
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: function() { return false; }
        });
    } catch (err) {}
    try {
        window.chrome = window.chrome || { runtime: {}, app: { isInstalled: false } };
    } catch (err) {}
    try {
        Object.defineProperty(navigator, 'languages', {
            get: function() { return ['de-DE', 'de', 'en-US', 'en']; }
        });
    } catch (err) {}
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: function() {
                return [{ name: 'Chrome PDF Viewer' },
                        { name: 'Chromium PDF Viewer' },
                        { name: 'Microsoft Edge PDF Viewer' },
                        { name: 'WebKit built-in PDF' }];
            }
        });
    } catch (err) {}
    try {
        if (navigator.permissions && navigator.permissions.query) {
            var originalQuery = navigator.permissions.query.bind(navigator.permissions);
            navigator.permissions.query = function(parameters) {
                if (parameters && parameters.name === 'notifications') {
                    var state = 'prompt';
                    try {
                        if (typeof Notification !== 'undefined' && Notification.permission) {
                            state = Notification.permission;
                        }
                    } catch (err) {}
                    return Promise.resolve({ state: state });
                }
                return originalQuery(parameters);
            };
        }
    } catch (err) {}
    try {
        var spoofVendor = function(getParameter) {
            return function(parameter) {
                if (parameter === 37445) { return 'Intel Inc.'; }
                if (parameter === 37446) { return 'Intel Iris OpenGL Engine'; }
                return getParameter.call(this, parameter);
            };
        };
        if (window.WebGLRenderingContext) {
            WebGLRenderingContext.prototype.getParameter =
                spoofVendor(WebGLRenderingContext.prototype.getParameter);
        }
        if (window.WebGL2RenderingContext) {
            WebGL2RenderingContext.prototype.getParameter =
                spoofVendor(WebGL2RenderingContext.prototype.getParameter);
        }
    } catch (err) {}
})();
"""


def _clean_user_agent(ua):
    """Return a Chrome-like UA without the QtWebEngine fingerprint token.

    Bot protection services (e.g. DataDome, used by several Tolino partner
    shops) flag requests whose user agent contains "QtWebEngine". Keeping the
    embedded Chromium version but dropping the Qt token makes the login window
    look like a regular Chrome on the user's platform.
    """
    if not ua or not str(ua).strip():
        return ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    cleaned = _QT_UA_TOKEN.sub("", str(ua))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _on_reader(url):
    host = (url.host() or "").casefold()
    return any(host == name or host.endswith("." + name) for name in READER_HOSTS)


if QWebEnginePage is not None:
    class _QuietWebEnginePage(QWebEnginePage):
        """Drop site console noise (Permissions-Policy, OTS fonts, gamepad, HEVC)."""

        def javaScriptConsoleMessage(self, *_args):
            return None
else:
    _QuietWebEnginePage = None


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
        self._torn_down = False

        self.setWindowTitle("Tolino-Anmeldung: %s" % self.partner.get("name", ""))
        self.resize(980, 760)
        layout = QVBoxLayout(self)

        self.status = QLabel(
            "Im Fenster anmelden. Falls ein Sicherheits-Check (Bot-Schutz) "
            "erscheint, lösen Sie ihn bitte einmalig hier im Fenster; danach "
            "werden die Tokens automatisch übernommen.")
        layout.addWidget(self.status)

        self.profile = QWebEngineProfile(LOGIN_PROFILE_STORAGE, self)
        # A named profile persists cookies across login attempts; DataDome's
        # validation cookie then survives reloads and new dialog sessions.
        try:
            self.profile.setHttpUserAgent(
                _clean_user_agent(self.profile.httpUserAgent()))
        except (AttributeError, RuntimeError):
            pass
        self._install_stealth_script()
        policy = _resolve_enum(QWebEngineProfile, *_FORCE_PERSISTENT_COOKIES)
        if policy is not None:
            self.profile.setPersistentCookiesPolicy(policy)
        self.page = _QuietWebEnginePage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view)

        close_role = _resolve_enum(QDialogButtonBox, *_CLOSE_BUTTON)
        buttons = QDialogButtonBox(close_role) if close_role is not None \
            else QDialogButtonBox()
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll_storage)
        self.page.urlChanged.connect(self._on_url_changed)
        self._timer.start()
        self.view.load(QUrl(self._start_url()))

    def _install_stealth_script(self):
        """Inject the browser-consistency spoof before any page JS runs."""
        if QWebEngineScript is None:
            return
        injection = _resolve_enum(
            QWebEngineScript,
            "InjectionPoint.DocumentCreation",  # Qt6 scoped
            "DocumentCreation",  # Qt5 flat
        )
        world = _resolve_enum(
            QWebEngineScript,
            "ScriptWorldId.MainWorld",  # Qt6 scoped
            "MainWorld",  # Qt5 flat
        )
        if injection is None or world is None:
            return
        collection = self.profile.scripts()
        try:
            for item in collection.toList():
                if item.name() == STEALTH_SCRIPT_NAME:
                    collection.remove(item)
        except Exception:
            pass
        script = QWebEngineScript()
        script.setName(STEALTH_SCRIPT_NAME)
        script.setInjectionPoint(injection)
        script.setWorldId(world)
        script.setRunsOnSubFrames(True)
        script.setSourceCode(_STEALTH_JS)
        collection.insert(script)

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
        self.page.runJavaScript(_STORAGE_JS, self._storage_ready)

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

    def _teardown_webengine(self):
        """Delete the page before the profile to avoid Qt lifetime warnings."""
        if getattr(self, "_torn_down", True):
            return
        self._torn_down = True
        view = getattr(self, "view", None)
        page = getattr(self, "page", None)
        profile = getattr(self, "profile", None)
        if view is not None:
            try:
                view.stop()
                view.setPage(None)
            except RuntimeError:
                pass
        if page is not None:
            try:
                page.deleteLater()
            except RuntimeError:
                pass
        if profile is not None:
            try:
                profile.deleteLater()
            except RuntimeError:
                pass

    def accept(self):
        self._teardown_webengine()
        QDialog.accept(self)

    def reject(self):
        self._cancel_requested = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        self._teardown_webengine()
        QDialog.reject(self)

    def closeEvent(self, event):
        self._cancel_requested = True
        if hasattr(self, "_timer"):
            self._timer.stop()
        self._teardown_webengine()
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

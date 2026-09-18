"""Embedded browser login for the Tolino web reader.

Runs the partner's OAuth/Keycloak sign-in inside an embedded QtWebEngine view.
After the user signs in and the web reader loads, the tokens are read from the
page's Local Storage and persisted. No token value is ever logged.

Bot-protection hardening history:
- 0.8.4: cleaned Chrome-like user agent (no QtWebEngine token)
- 0.8.5: named persistent profile (validation cookies survive reloads) and a
  stealth script aligning JS fingerprints
- 0.8.6: Sec-CH-UA client hint headers + navigator.userAgentData spoof, and a
  guided fallback that completes the login in the user's default browser and
  harvests the tokens from its storage when a provider still blocks the view.
"""
import json
import re
import urllib.parse
import webbrowser

try:
    from qt.core import (QDialog, QDialogButtonBox, QLabel, QMessageBox,
                         QPushButton, QTimer, QUrl, QVBoxLayout)
    from qt.webengine import (QWebEnginePage, QWebEngineProfile,
                              QWebEngineScript, QWebEngineUrlRequestInterceptor,
                              QWebEngineView)
except ImportError:  # Non-Calibre environments (tests, type checks)
    QDialog = object
    QDialogButtonBox = QLabel = QMessageBox = QPushButton = None
    QTimer = QUrl = QVBoxLayout = None
    QWebEnginePage = QWebEngineProfile = QWebEngineScript = None
    QWebEngineUrlRequestInterceptor = QWebEngineView = None

try:
    from .tolino import (PARTNERS, TolinoAuthError, extract_login_tokens,
                         scrape_browser_tokens)
except ImportError:
    from tolino import (PARTNERS, TolinoAuthError, extract_login_tokens,
                        scrape_browser_tokens)


READER_HOSTS = ("webreader.mytolino.com", "webreader.hugendubel.de")
LOGIN_PROFILE_STORAGE = "tolino-cloud-sync-login"
STEALTH_SCRIPT_NAME = "tolino-cloud-sync-stealth"

_QT_UA_TOKEN = re.compile(r"\s*QtWebEngine/[\w.]+", re.IGNORECASE)
_CHROME_MAJOR_RE = re.compile(r"Chrome/(\d+)")

_DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def _clean_user_agent(ua):
    """Return a Chrome-like UA without the QtWebEngine fingerprint token."""
    if not ua or not str(ua).strip():
        return _DEFAULT_UA
    cleaned = _QT_UA_TOKEN.sub("", str(ua))
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def _platform_from_ua(ua):
    text = str(ua or "")
    if "Windows" in text:
        return "Windows"
    if "Mac OS" in text or "Macintosh" in text:
        return "macOS"
    return "Linux"


def _client_hint_headers(user_agent):
    """Sec-CH-UA headers matching the cleaned UA (empty for non-Chrome UAs)."""
    match = _CHROME_MAJOR_RE.search(str(user_agent or ""))
    if not match:
        return {}
    major = match.group(1)
    platform = _platform_from_ua(user_agent)
    return {
        "Sec-CH-UA": ('"Not A(Brand";v="99", "Chromium";v="%s", '
                      '"Google Chrome";v="%s"' % (major, major)),
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"%s"' % platform,
    }


_STEALTH_TEMPLATE = """
(function() {
    var chromeMajor = "__CHROME_MAJOR__";
    var platformName = "__PLATFORM__";
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
    try {
        if (!navigator.userAgentData) {
            var brands = [
                { brand: 'Not A(Brand', version: '99' },
                { brand: 'Chromium', version: chromeMajor },
                { brand: 'Google Chrome', version: chromeMajor }
            ];
            var uaData = {
                brands: brands,
                mobile: false,
                platform: platformName,
                getHighEntropyValues: function() {
                    return Promise.resolve({
                        architecture: 'x86', bitness: '64', model: '',
                        platform: platformName, platformVersion: '10.0.0',
                        uaFullVersion: chromeMajor + '.0.0.0',
                        fullVersionList: brands
                    });
                },
                toJSON: function() {
                    return { brands: brands, mobile: false, platform: platformName };
                }
            };
            Object.defineProperty(navigator, 'userAgentData', {
                get: function() { return uaData; }
            });
        }
    } catch (err) {}
})();
"""


def _stealth_js(chrome_major="118", platform="Linux"):
    return (_STEALTH_TEMPLATE
            .replace("__CHROME_MAJOR__", str(chrome_major))
            .replace("__PLATFORM__", str(platform)))


def _resolve_enum(owner, *paths):
    """Resolve a Qt constant across Qt5 and Qt6 enum naming schemes."""
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
_ACTION_ROLE = (
    "ButtonRole.ActionRole",  # Qt6 scoped
    "ActionRole",  # Qt5 flat
)
_OK_BUTTON = ("StandardButton.Ok", "Ok")
_CANCEL_BUTTON = ("StandardButton.Cancel", "Cancel")


if QWebEnginePage is not None:
    class _QuietWebEnginePage(QWebEnginePage):
        """Drop site console noise (Permissions-Policy, OTS fonts, gamepad, HEVC)."""

        def javaScriptConsoleMessage(self, *_args):
            return None
else:
    _QuietWebEnginePage = None

if QWebEngineUrlRequestInterceptor is not None:
    class _ClientHintsInterceptor(QWebEngineUrlRequestInterceptor):
        """Add Sec-CH-UA headers consistent with the cleaned UA to every request."""

        def __init__(self, user_agent, parent=None):
            try:
                QWebEngineUrlRequestInterceptor.__init__(self, parent)
            except TypeError:
                QWebEngineUrlRequestInterceptor.__init__(self)
            self._headers = _client_hint_headers(user_agent)

        def interceptRequest(self, *args):
            info = args[0]
            for name, value in self._headers.items():
                try:
                    info.setHttpHeader(name, value)
                except Exception:
                    return
else:
    _ClientHintsInterceptor = None


def _on_reader(url):
    host = (url.host() or "").casefold()
    return any(host == name or host.endswith("." + name) for name in READER_HOSTS)


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
            "erscheint, lösen Sie ihn hier im Fenster – oder verwenden Sie "
            "unten 'Im Standardbrowser öffnen', der zuverlässig durchgelassen wird.")
        layout.addWidget(self.status)

        self.profile = QWebEngineProfile(LOGIN_PROFILE_STORAGE, self)
        # A named profile persists cookies across login attempts; DataDome's
        # validation cookie then survives reloads and new dialog sessions.
        user_agent = ""
        try:
            self.profile.setHttpUserAgent(
                _clean_user_agent(self.profile.httpUserAgent()))
            user_agent = self.profile.httpUserAgent()
        except (AttributeError, RuntimeError):
            pass
        self._install_client_hints(user_agent)
        self._install_stealth_script(user_agent)
        policy = _resolve_enum(QWebEngineProfile, *_FORCE_PERSISTENT_COOKIES)
        if policy is not None:
            self.profile.setPersistentCookiesPolicy(policy)
        self.page = _QuietWebEnginePage(self.profile, self)
        self.view = QWebEngineView(self)
        self.view.setPage(self.page)
        layout.addWidget(self.view)

        buttons = QDialogButtonBox()
        close_role = _resolve_enum(QDialogButtonBox, *_CLOSE_BUTTON)
        if close_role is not None:
            buttons = QDialogButtonBox(close_role)
        self.external_button = QPushButton(
            "Im Standardbrowser öffnen (bei Bot-Schutz)")
        self.external_button.clicked.connect(self._open_external_login)
        action_role = _resolve_enum(QDialogButtonBox, *_ACTION_ROLE)
        if action_role is not None:
            buttons.addButton(self.external_button, action_role)
        else:
            layout.addWidget(self.external_button)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll_storage)
        self.page.urlChanged.connect(self._on_url_changed)
        self._timer.start()
        self.view.load(QUrl(self._start_url()))

    def _install_client_hints(self, user_agent):
        """Register a request interceptor adding consistent Sec-CH-UA headers."""
        if _ClientHintsInterceptor is None:
            return
        if not _client_hint_headers(user_agent):
            return
        try:
            interceptor = _ClientHintsInterceptor(user_agent, self.profile)
        except Exception:
            return
        for setter in ("setUrlRequestInterceptor", "setRequestInterceptor"):
            try:
                getattr(self.profile, setter)(interceptor)
            except (AttributeError, RuntimeError):
                continue
            self._interceptor = interceptor
            return

    def _install_stealth_script(self, user_agent):
        """Inject the browser-consistency spoof before any page JS runs."""
        if QWebEngineScript is None:
            return
        match = _CHROME_MAJOR_RE.search(user_agent or "")
        source = _stealth_js(match.group(1) if match else "118",
                             _platform_from_ua(user_agent))
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
        try:
            collection = self.profile.scripts()
        except (AttributeError, RuntimeError):
            return
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
        script.setSourceCode(source)
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

    def _apply_external_tokens(self, refresh, hardware):
        """Adopt tokens harvested from the default browser; True on success."""
        refresh = str(refresh or "").strip()
        if not refresh:
            return False
        self.refresh_token = refresh
        self.hardware_id = hardware or self.hardware_id_value or None
        self.completed = True
        try:
            self._timer.stop()
        except Exception:
            pass
        self.status.setText(
            "Tokens aus dem Standardbrowser übernommen.")
        return True

    def _open_external_login(self):
        """Guided fallback: login in the default browser, then harvest tokens."""
        try:
            self._timer.stop()
        except Exception:
            pass
        try:
            opened = webbrowser.open(self._start_url())
        except Exception:
            opened = False
        if not opened:
            QMessageBox.warning(
                self, "Browser-Anmeldung / Browser sign-in",
                "Der Standardbrowser konnte nicht geöffnet werden.")
            return
        self.hide()
        ok = _resolve_enum(QMessageBox, *_OK_BUTTON)
        cancel = _resolve_enum(QMessageBox, *_CANCEL_BUTTON)
        box = QMessageBox(self)
        box.setWindowTitle("Anmeldung im Standardbrowser / Sign in externally")
        box.setText(
            "1. Melden Sie sich im geöffneten Browser an und lösen Sie dort "
            "ggf. den Sicherheits-Check.\n"
            "2. Öffnen Sie nach dem Login den Tolino Web Reader (wird meist "
            "automatisch geladen) und schließen Sie den Browser dann "
            "komplett.\n"
            "3. Klicken Sie hier auf OK – die Tokens werden automatisch "
            "übernommen.")
        if ok is not None and cancel is not None:
            box.setStandardButtons(ok | cancel)
        accepted = (box.exec() == ok) if ok is not None else True
        if accepted:
            refresh, hardware = scrape_browser_tokens()
            if self._apply_external_tokens(refresh, hardware):
                self.accept()
                return
            QMessageBox.warning(
                self, "Keine Tokens gefunden / No tokens found",
                "Es wurden keine Tokens gefunden. Stellen Sie sicher, dass Sie "
                "im Web Reader angemeldet waren und den Browser vollständig "
                "geschlossen haben, und versuchen Sie es erneut.")
        self.show()
        try:
            self._timer.start()
        except Exception:
            pass

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

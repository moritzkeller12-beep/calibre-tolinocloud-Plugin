"""Authentication and small asynchronous UI workers."""
from .ui_common import QObject, pyqtSignal
from .config import CredentialStore
from .browser_login import browser_login
from .token_vault import check_stored_login
from .tolino import report_error


class BrowserLoginWorker(QObject):
    """Runs browser_login off the GUI thread (it polls for minutes)."""
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, partner_id, hardware):
        QObject.__init__(self)
        self.partner_id = partner_id
        self.hardware = hardware

    def run(self):
        try:
            refresh, hardware = browser_login(
                self.partner_id, self.hardware,
                progress=self.progress.emit)
        except Exception as exc:
            self.failed.emit(exc)
            return
        self.completed.emit(refresh, hardware)


class SessionCheckWorker(QObject):
    """Check a saved login outside the GUI thread."""
    valid = pyqtSignal()
    invalid = pyqtSignal(str)
    busy = pyqtSignal(str)

    def __init__(self, settings):
        QObject.__init__(self)
        self.settings = settings

    def run(self):
        values = self.settings
        try:
            store = CredentialStore(values, values.get("account_name"))
            status, detail = check_stored_login(store)
        except Exception as exc:
            self.invalid.emit(report_error(
                exc, "Zugangsprüfung", (values["refresh_token"],)))
            return
        if status == "valid":
            self.valid.emit()
        elif status == "busy":
            self.busy.emit(detail)
        else:
            self.invalid.emit(detail)

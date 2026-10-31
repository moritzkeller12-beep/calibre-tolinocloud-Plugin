"""Authentication and small asynchronous UI workers."""
from .ui_common import QObject, pyqtSignal
from .config import CredentialStore
from .browser_login import LOGIN_TIMEOUT, grab_live_refresh
from .token_vault import check_stored_login
from .tolino import report_error


class BrowserLoginWorker(QObject):
    """Runs the browser login off the GUI thread (it polls for minutes)."""
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, partner_id, hardware):
        QObject.__init__(self)
        self.partner_id = partner_id
        self.hardware = hardware

    def run(self):
        try:
            refresh, hardware = grab_live_refresh(
                int(self.partner_id), self.hardware, timeout=LOGIN_TIMEOUT,
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

    def __init__(self, context):
        QObject.__init__(self)
        self.context = context

    def run(self):
        try:
            store = CredentialStore(self.context.credentials,
                                    self.context.account_name)
            status, detail = check_stored_login(store)
        except Exception as exc:
            self.invalid.emit(report_error(
                exc, "Zugangsprüfung", self.context.secrets()))
            return
        if status == "valid":
            self.valid.emit()
        elif status == "busy":
            self.busy.emit(detail)
        else:
            self.invalid.emit(detail)

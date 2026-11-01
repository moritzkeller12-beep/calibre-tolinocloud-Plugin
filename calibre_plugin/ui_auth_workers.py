"""Authentication and small asynchronous UI workers."""
from .ui_common import QObject, pyqtSignal
from .browser_login import LOGIN_TIMEOUT, grab_live_refresh
from .config import save_credentials
from .token_vault import check_stored_login
from .tolino import report_error


class BrowserLoginWorker(QObject):
    """Runs the browser login off the GUI thread (it polls for minutes)."""
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, account_name, partner_id, hardware):
        QObject.__init__(self)
        self.account_name = account_name
        self.partner_id = partner_id
        self.hardware = hardware

    def run(self):
        try:
            refresh, hardware = grab_live_refresh(
                int(self.partner_id), self.hardware, timeout=LOGIN_TIMEOUT,
                progress=self.progress.emit)
            # Vor dem Signal speichern: das Dashboard darf vorher schliessen,
            # ohne dass der neue Single-Use-Token verloren geht.
            save_credentials(self.account_name, refresh, hardware)
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
            status, detail = check_stored_login(
                self.context.account_name, self.context.partner_id)
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

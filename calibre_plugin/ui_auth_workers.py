"""Authentication and small asynchronous UI workers."""
from qt.core import QObject, pyqtSignal
from .browser_login import LOGIN_TIMEOUT, grab_live_refresh
from .config import save_credentials
from .errors import SETTINGS_WRITE_DETAIL, TolinoAuthError
from .token_vault import check_stored_login
from .tolino import RELOGIN, report_error


class BrowserLoginWorker(QObject):
    """Runs the browser login off the GUI thread (it polls for minutes)."""
    completed = pyqtSignal(object)
    failed = pyqtSignal(object)
    progress = pyqtSignal(str)

    def __init__(self, account_name, hardware):
        QObject.__init__(self)
        self.account_name = account_name
        self.hardware = hardware

    def run(self):
        try:
            refresh, hardware, partner_id = grab_live_refresh(
                self.hardware, timeout=LOGIN_TIMEOUT,
                progress=self.progress.emit)
            # Vor dem Signal speichern: das Dashboard darf vorher schliessen,
            # ohne dass der Single-Use-Token verloren geht; ``False`` = Fehler.
            if save_credentials(self.account_name, refresh, hardware,
                                partner_id) is False:
                raise TolinoAuthError(
                    "Die Anmeldung konnte nicht gespeichert werden. " + RELOGIN,
                    code="A01",
                    detail=SETTINGS_WRITE_DETAIL)
        except Exception as exc:
            self.failed.emit(exc)
            return
        self.completed.emit(hardware)


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
            status, detail = check_stored_login(self.context.account_name)
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

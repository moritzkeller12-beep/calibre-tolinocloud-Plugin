import os
import tempfile

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
try:
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread, QVBoxLayout,
                         QObject, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread, QVBoxLayout,
                         QObject, Signal as pyqtSignal)

try:
    from .config import PREFERENCES, save_settings
    from .sync import load_state, plan_sync, sync_summary
    from .tolino import PARTNERS, TolinoAuthError, TolinoClient, browser_login, hardware_id
except ImportError:
    from config import PREFERENCES, save_settings
    from sync import load_state, plan_sync, sync_summary
    from tolino import PARTNERS, TolinoAuthError, TolinoClient, browser_login, hardware_id


def _metadata_value(item, name, default=""):
    try:
        return getattr(item, name)
    except AttributeError:
        try:
            return item.get(name, default)
        except AttributeError:
            return default


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, settings, jobs, removals):
        QObject.__init__(self)
        self.settings = settings
        self.jobs = jobs
        self.removals = removals
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        state = load_state(self.settings["state"])
        client = None
        try:
            client = TolinoClient(self.settings["partner_id"], self.settings["hardware_id"],
                                  self.settings["refresh_token"], self.settings["username"],
                                  self.settings["password"])
            client.login()
            remote_ids = client.inventory_ids() if self.settings["enable_deletions"] else set()
            total = len(self.jobs) + len(self.removals)
            done = 0
            for job in self.jobs:
                if self.cancelled:
                    raise RuntimeError("Synchronization aborted by user.")
                book_id, book_uuid, fmt, old_id, path, cover_path = job
                self.progress.emit(done, total, "Uploading %s (%s)" % (book_uuid, fmt))
                new_id = client.upload(path)
                state[book_uuid] = {
                    "tolino_id": new_id,
                    "fingerprint": state.get(book_uuid, {}).get("fingerprint", ""),
                    "calibre_id": book_id,
                }
                if cover_path:
                    client.upload_cover(new_id, cover_path)
                if old_id and self.settings["enable_deletions"] and str(old_id) in remote_ids:
                    client.delete(old_id)
                done += 1
                self.progress.emit(done, total, "Uploaded %s" % book_uuid)
            for book_uuid, tolino_id in self.removals:
                if self.cancelled:
                    raise RuntimeError("Synchronization aborted by user.")
                if str(tolino_id) in remote_ids:
                    self.progress.emit(done, total, "Deleting %s" % book_uuid)
                    client.delete(tolino_id)
                state.pop(book_uuid, None)
                done += 1
                self.progress.emit(done, total, "Processed %s" % book_uuid)
            self.completed.emit(state, client.refresh)
        except Exception as exc:
            self.failed.emit(str(exc))


class SyncDashboard(QDialog):
    def __init__(self, gui):
        QDialog.__init__(self, gui)
        self.gui = gui
        self.thread = None
        self.worker = None
        self.temp_files = []
        self.setWindowTitle("Tolino Cloud Sync")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        account = QGroupBox("Tolino-Konto / Tolino account")
        account_form = QFormLayout(account)
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem("%s - %s" % (pid, partner["name"]), pid)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(QLineEdit.Password)
        self.status = QLabel()
        self.browser = QPushButton("Im Browser anmelden / Sign in in browser")
        self.browser.clicked.connect(self.browser_login)
        account_form.addRow("Partner", self.partner)
        account_form.addRow("Hardware ID", self.hardware)
        account_form.addRow("Refresh token", self.refresh)
        account_form.addRow("", self.browser)
        account_form.addRow("Status", self.status)
        root.addWidget(account)

        options = QGroupBox("Synchronisation / Synchronization")
        option_form = QFormLayout(options)
        self.formats = QLineEdit()
        self.covers = QCheckBox("Covers hochladen / Upload covers")
        self.deletions = QCheckBox("Löschungen erlauben / Allow deletions")
        option_form.addRow("Formate / Formats", self.formats)
        option_form.addRow("", self.covers)
        option_form.addRow("", self.deletions)
        root.addWidget(options)

        self.progress_label = QLabel("Bereit / Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.start = QPushButton("Synchronisierung starten / Start synchronization")
        self.cancel = QPushButton("Abbrechen / Abort")
        self.cancel.setEnabled(False)
        self.start.clicked.connect(self.start_sync)
        self.cancel.clicked.connect(self.cancel_sync)
        root.addWidget(self.progress_label)
        root.addWidget(self.progress)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        root.addWidget(self.start)
        root.addWidget(self.cancel)
        root.addWidget(buttons)
        self.load_values()

    def load_values(self):
        values = dict(PREFERENCES)
        self.partner.setCurrentIndex(max(0, self.partner.findData(values["partner_id"])))
        self.hardware.setText(values["hardware_id"] or hardware_id())
        self.refresh.setText(values["refresh_token"])
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.covers.setChecked(values["upload_covers"])
        self.deletions.setChecked(values["enable_deletions"])
        self.update_status()

    def update_status(self):
        self.status.setText("Angemeldet / configured" if self.refresh.text().strip()
                            else "Nicht angemeldet / not configured")

    def values(self):
        return {
            "partner_id": self.partner.currentData(),
            "hardware_id": self.hardware.text().strip() or hardware_id(),
            "refresh_token": self.refresh.text().strip(),
            "username": PREFERENCES["username"],
            "password": PREFERENCES["password"],
            "preferred_formats": [x.strip().upper() for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "state": PREFERENCES["state"],
        }

    def browser_login(self):
        try:
            refresh, hardware = browser_login(self.partner.currentData(), self.hardware.text().strip())
        except TolinoAuthError as exc:
            QMessageBox.warning(self, "Browser-Anmeldung / Browser sign-in", str(exc))
            return
        self.refresh.setText(refresh)
        self.hardware.setText(hardware)
        self.update_status()
        QMessageBox.information(self, "Anmeldung erfolgreich / Sign-in complete",
                                "Der Refresh-Token wird erst beim Speichern verwendet.")

    def start_sync(self):
        if self.thread is not None:
            return
        settings = self.values()
        if not settings["refresh_token"]:
            QMessageBox.warning(self, "Konfiguration / Configuration",
                                "Bitte zuerst einen Refresh-Token konfigurieren.")
            return
        try:
            metadata = {}
            for book_id in self.gui.current_db.all_book_ids():
                item = self.gui.current_db.get_metadata(book_id)
                metadata[book_id] = {
                    "uuid": _metadata_value(item, "uuid"),
                    "title": _metadata_value(item, "title"),
                    "formats": _metadata_value(item, "formats"),
                    "last_modified": str(_metadata_value(item, "last_modified")),
                }
            for item in metadata.values():
                if isinstance(item["formats"], str):
                    item["formats"] = item["formats"].split(",")
            state = load_state(settings["state"])
            uploads, removals, current = plan_sync(metadata, state, settings["preferred_formats"],
                                                    settings["enable_deletions"])
            jobs = []
            for book_id, book_uuid, fmt, old_id in uploads:
                path = self.gui.current_db.format_abspath(book_id, fmt)
                cover_path = None
                if settings["upload_covers"]:
                    cover = self.gui.current_db.cover(book_id, as_file=False)
                    if cover:
                        temp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                        temp.write(cover)
                        temp.close()
                        cover_path = temp.name
                        self.temp_files.append(cover_path)
                jobs.append((book_id, book_uuid, fmt, old_id, path, cover_path))
            settings["state"] = current
        except Exception as exc:
            error_dialog(self, "Vorbereitung fehlgeschlagen / Preparation failed", str(exc), show=True)
            return
        summary = sync_summary(len(jobs), len(removals))
        self.progress.setRange(0, summary["total"] or 1)
        self.progress.setValue(0)
        self.thread = QThread(self)
        self.worker = SyncWorker(settings, jobs, removals)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.sync_completed)
        self.worker.failed.connect(self.sync_failed)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.start.setEnabled(False)
        self.cancel.setEnabled(True)
        self.progress_label.setText("Synchronisierung läuft / Synchronizing")

    def progress_changed(self, done, total, text):
        self.progress.setRange(0, total or 1)
        self.progress.setValue(done)
        self.progress_label.setText(text)

    def cancel_sync(self):
        if self.worker:
            self.worker.cancel()
            self.progress_label.setText("Abbruch angefordert / Abort requested")
            self.cancel.setEnabled(False)

    def sync_completed(self, state, refresh):
        save_settings({"state": state, "refresh_token": refresh, "hardware_id": self.hardware.text().strip()})
        self.finish_thread()
        info_dialog(self, "Tolino Cloud Sync", "Synchronisierung abgeschlossen / Synchronization complete.",
                    show_copy_button=False)

    def sync_failed(self, message):
        self.finish_thread()
        error_dialog(self, "Synchronisierung fehlgeschlagen / Synchronization failed", message, show=True)

    def finish_thread(self):
        save_settings(self.values())
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        self.progress_label.setText("Bereit / Ready")
        for path in self.temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_files = []
        self.update_status()

    def closeEvent(self, event):
        if self.thread:
            self.cancel_sync()
            self.thread.quit()
            self.thread.wait()
        save_settings(self.values())
        event.accept()


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("Tolino Cloud Sync", None, "Open Tolino Cloud dashboard", None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dashboard)
        self.menu = self.qaction

    def show_dashboard(self):
        dialog = SyncDashboard(self.gui)
        dialog.exec()

import tempfile
from threading import Event

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                     QFormLayout, QLineEdit, QMessageBox, QProgressDialog,
                     QPushButton)

from .config import PREFERENCES, save_settings
from .sync import load_state, plan_sync
from .tolino import PARTNERS, TolinoAuthError, TolinoClient, browser_login, hardware_id


class ConfigDialog(QDialog):
    def __init__(self, parent=None):
        QDialog.__init__(self, parent)
        values = dict(PREFERENCES)
        self.setWindowTitle("Tolino Cloud Sync")
        form = QFormLayout(self)
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem("%s - %s" % (pid, partner["name"]), pid)
        self.partner.setCurrentIndex(max(0, self.partner.findData(values["partner_id"])))
        self.hardware = QLineEdit(values["hardware_id"])
        if not self.hardware.text():
            self.hardware.setText(hardware_id())
        self.refresh = QLineEdit(values["refresh_token"])
        self.refresh.setEchoMode(QLineEdit.Password)
        self.username = QLineEdit(values["username"])
        self.password = QLineEdit(values["password"])
        self.password.setEchoMode(QLineEdit.Password)
        self.formats = QLineEdit(", ".join(values["preferred_formats"]))
        self.covers = QCheckBox()
        self.covers.setChecked(values["upload_covers"])
        self.deletions = QCheckBox()
        self.deletions.setChecked(values["enable_deletions"])
        self.browser = QPushButton("Im Browser anmelden")
        self.browser.clicked.connect(self.browser_login)
        for label, widget in (("Partner", self.partner), ("Hardware ID", self.hardware),
                              ("Refresh token", self.refresh), ("Username", self.username),
                              ("Password", self.password), ("Preferred formats", self.formats),
                              ("Upload covers", self.covers), ("Enable deletions (caution)", self.deletions)):
            form.addRow(label, widget)
        form.addRow("", self.browser)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def values(self):
        return {"partner_id": self.partner.currentData(), "hardware_id": self.hardware.text().strip(),
                "refresh_token": self.refresh.text().strip(), "username": self.username.text().strip(),
                "password": self.password.text(), "preferred_formats":
                [x.strip().upper() for x in self.formats.text().split(",") if x.strip()],
                "upload_covers": self.covers.isChecked(), "enable_deletions": self.deletions.isChecked()}

    def browser_login(self):
        partner_id = self.partner.currentData()
        try:
            refresh, hardware = browser_login(partner_id, self.hardware.text().strip())
        except TolinoAuthError as exc:
            QMessageBox.warning(self, "Browser-Anmeldung nicht verfügbar", str(exc))
            return
        self.refresh.setText(refresh)
        self.hardware.setText(hardware)
        self.username.clear()
        self.password.clear()
        QMessageBox.information(self, "Browser-Anmeldung erfolgreich",
                                "Der Refresh-Token wurde erhalten. Mit OK werden "
                                "die Einstellungen in Calibre gespeichert.")


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("Sync with Tolino Cloud", None, "Upload Calibre books to Tolino", None)

    def genesis(self):
        self.qaction.triggered.connect(self.sync)
        self.menu = self.qaction

    def config(self):
        dialog = ConfigDialog(self.gui)
        if dialog.exec() == QDialog.Accepted:
            save_settings(dialog.values())

    def sync(self):
        if not PREFERENCES["refresh_token"] and not PREFERENCES["username"]:
            self.config()
        progress = QProgressDialog("Synchronizing with Tolino Cloud...", "Abort", 0, 0, self.gui)
        progress.setWindowTitle("Tolino Cloud Sync")
        progress.setAutoClose(False)
        progress.show()
        try:
            settings = dict(PREFERENCES)
            client = TolinoClient(settings["partner_id"], settings["hardware_id"],
                                  settings["refresh_token"], settings["username"], settings["password"])
            client.login()
            metadata = {}
            for book_id in self.gui.current_db.all_book_ids():
                item = self.gui.current_db.get_metadata(book_id)
                def value(name, default=""):
                    try:
                        return getattr(item, name)
                    except AttributeError:
                        try:
                            return item.get(name, default)
                        except AttributeError:
                            return default
                metadata[book_id] = {
                    "uuid": value("uuid"),
                    "title": value("title"),
                    "formats": value("formats"),
                    "last_modified": str(value("last_modified")),
                }
            # Calibre's metadata object exposes formats as a comma-separated string.
            for item in metadata.values():
                if isinstance(item.get("formats"), str):
                    item["formats"] = item["formats"].split(",")
            state = load_state(PREFERENCES["state"])
            uploads, removals, new_state = plan_sync(metadata, state, settings["preferred_formats"],
                                                     settings["enable_deletions"])
            remote_ids = client.inventory_ids() if settings["enable_deletions"] else set()
            for index, (book_id, book_uuid, fmt, old_id) in enumerate(uploads):
                if progress.wasCanceled():
                    raise RuntimeError("Synchronization aborted by user.")
                path = self.gui.current_db.format_abspath(book_id, fmt)
                new_id = client.upload(path)
                new_state[book_uuid]["tolino_id"] = new_id
                if settings["upload_covers"]:
                    cover = self.gui.current_db.cover(book_id, as_file=False)
                    if cover:
                        with tempfile.NamedTemporaryFile(suffix=".jpg") as temp:
                            temp.write(cover)
                            temp.flush()
                            client.upload_cover(new_id, temp.name)
                if old_id and settings["enable_deletions"] and str(old_id) in remote_ids:
                    client.delete(old_id)
                progress.setValue(index + 1)
            for book_uuid, tolino_id in removals:
                if progress.wasCanceled():
                    raise RuntimeError("Synchronization aborted by user.")
                if str(tolino_id) not in remote_ids:
                    new_state.pop(book_uuid, None)
                    continue
                client.delete(tolino_id)
                new_state.pop(book_uuid, None)
            save_settings({"state": new_state, "refresh_token": client.refresh,
                           "hardware_id": client.hardware})
            info_dialog(self.gui, "Tolino Cloud Sync", "Synchronization completed.", show_copy_button=False)
        except Exception as exc:
            error_dialog(self.gui, "Tolino Cloud Sync failed", str(exc), show=True)
        finally:
            progress.close()

import os
import tempfile
import traceback

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
try:
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread, QVBoxLayout,
                         QHBoxLayout, QTableWidget, QTableWidgetItem,
                         QTextEdit, QObject, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread, QVBoxLayout,
                         QHBoxLayout, QTableWidget, QTableWidgetItem,
                         QTextEdit, QObject, Signal as pyqtSignal)

try:
    from .config import save_settings, settings
    from .sync import (compare_inventory, iter_book_ids, load_state, plan_sync,
                       selected_book_ids, sync_summary, normalize_formats,
                       safe_format_path, cover_bytes, unpack_plan_result,
                       unpack_upload_record, diagnose_preparation,
                       format_diagnostic_report)
    from .tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                         hardware_id, normalize_refresh_token)
except ImportError:
    from config import save_settings, settings
    from sync import (compare_inventory, iter_book_ids, load_state, plan_sync,
                      selected_book_ids, sync_summary, normalize_formats,
                      safe_format_path, cover_bytes, unpack_plan_result,
                      unpack_upload_record, diagnose_preparation,
                      format_diagnostic_report)
    from tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                        hardware_id, normalize_refresh_token)


def _metadata_value(item, name, default=""):
    try:
        return getattr(item, name)
    except AttributeError:
        try:
            return item.get(name, default)
        except AttributeError:
            return default


def _display_value(value):
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return ", ".join("%s: %s" % (key, val) for key, val in sorted(value.items()))
    return str(value or "")


class SyncJob:
    __slots__ = ("book_id", "book_uuid", "format_name", "old_id", "path", "cover_path")

    def __init__(self, book_id, book_uuid, format_name, old_id, path, cover_path):
        self.book_id = book_id
        self.book_uuid = book_uuid
        self.format_name = format_name
        self.old_id = old_id
        self.path = path
        self.cover_path = cover_path


class InventoryDialog(QDialog):
    HEADERS = ("Upload", "Status", "Titel / Title", "Autor / Author",
               "ISBN", "Tolino-ID")

    def __init__(self, rows, parent=None):
        QDialog.__init__(self, parent)
        self.setWindowTitle("Bestandsvergleich / Inventory comparison")
        self.setMinimumSize(900, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Autor, Titel und ISBN dienen nur zum Vergleichen/Filtern; "
            "die Tolino-API schreibt keine Metadaten. Uploadbar sind Datei/Format "
            "und optional Cover."))
        self.table = QTableWidget(len(rows), len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.checks = []
        status_text = {
            "new_in_calibre": "Neu in Calibre",
            "only_tolino": "Nur Tolino",
            "identical": "Identisch",
            "changed": "Geändert",
        }
        for row_index, row in enumerate(rows):
            check = QCheckBox()
            check.setChecked(bool(row.get("selected")))
            check.setEnabled(row.get("book_id") is not None)
            self.checks.append(check)
            self.table.setCellWidget(row_index, 0, check)
            values = (
                status_text.get(row["status"], row["status"]),
                row.get("title", ""),
                row.get("authors", ""),
                row.get("isbn", ""),
                row.get("tolino_id", ""),
            )
            for column, value in enumerate(values, 1):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()
        layout.addWidget(self.table)
        buttons = QHBoxLayout()
        confirm = QPushButton("Auswahl übernehmen / Confirm selection")
        cancel = QPushButton("Abbrechen / Cancel")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(confirm)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def selected_ids(self, rows):
        if len(rows) != len(self.checks):
            raise ValueError("Inventory table and selection controls have different row counts.")
        return selected_book_ids([
            dict(row, selected=check.isChecked()) for row, check in zip(rows, self.checks)
        ])


class DiagnosticDialog(QDialog):
    def __init__(self, dashboard, report, parent=None):
        QDialog.__init__(self, parent)
        self.dashboard = dashboard
        self.setWindowTitle("Debug / Diagnose")
        self.setMinimumSize(760, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Die lokale Diagnose führt keinen Login und keine Tolino-Anfrage aus. "
            "Der Tolino-Test startet Netzwerkzugriff erst nach ausdrücklichem Klick."))
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlainText(report)
        layout.addWidget(self.output)
        buttons = QHBoxLayout()
        copy = QPushButton("Diagnose kopieren / Copy")
        copy.clicked.connect(self.copy_report)
        test = QPushButton("Tolino-Antwort testen")
        test.clicked.connect(self.test_tolino)
        close = QPushButton("Schließen / Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addWidget(test)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def copy_report(self):
        self.output.selectAll()
        self.output.copy()

    def test_tolino(self):
        values = self.dashboard.values()
        if not values["refresh_token"]:
            QMessageBox.warning(self, "Tolino-Antwort testen",
                                "Kein Refresh-Token konfiguriert.")
            return
        try:
            client = TolinoClient(values["partner_id"], values["hardware_id"],
                                  values["refresh_token"], values["username"],
                                  values["password"])
            client.login()
            inventory = client.inventory()
            result = {
                "step": "Tolino-Antwort testen",
                "status": "ok",
                "value": {
                    "response_type": type(inventory).__name__,
                    "item_count": len(inventory),
                    "sample": inventory[:3],
                    "auth": client.auth_diagnostics(),
                },
            }
        except Exception as exc:
            auth = locals().get("client")
            result = {
                "step": "Tolino-Antwort testen",
                "status": "error",
                "value": auth.auth_diagnostics() if auth else {
                    "partner_id": values["partner_id"],
                    "partner_name": PARTNERS[values["partner_id"]]["name"],
                    "client_id": PARTNERS[values["partner_id"]].get("client_id"),
                    "scope": PARTNERS[values["partner_id"]].get("scope"),
                    "token_url": PARTNERS[values["partner_id"]].get("token_url"),
                    **normalize_refresh_token(values["refresh_token"])[1],
                },
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            }
        self.output.append("\n" + format_diagnostic_report([result]))


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
            needs_replacement_cleanup = any(job.old_id for job in self.jobs)
            remote_ids = (client.inventory_ids()
                          if self.settings["enable_deletions"] or needs_replacement_cleanup
                          else set())
            total = len(self.jobs) + len(self.removals)
            done = 0
            for job in self.jobs:
                if self.cancelled:
                    raise RuntimeError("Synchronization aborted by user.")
                self.progress.emit(done, total, "Uploading %s (%s)" %
                                   (job.book_uuid, job.format_name))
                new_id = client.upload(job.path)
                state[job.book_uuid] = {
                    "tolino_id": new_id,
                    "fingerprint": state.get(job.book_uuid, {}).get("fingerprint", ""),
                    "calibre_id": job.book_id,
                }
                if job.cover_path:
                    client.upload_cover(new_id, job.cover_path)
                if job.old_id and str(job.old_id) in remote_ids:
                    client.delete(job.old_id)
                done += 1
                self.progress.emit(done, total, "Uploaded %s" % job.book_uuid)
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
        self.compare_authors = QCheckBox("Autor / Author (nur Vergleich/Filter)")
        self.compare_title = QCheckBox("Buchtitel / Title (nur Vergleich/Filter)")
        self.compare_isbn = QCheckBox("ISBN (nur Vergleich/Filter)")
        for field in (self.compare_authors, self.compare_title, self.compare_isbn):
            field.setChecked(True)
        self.upload_format = QLabel("Datei/Format wird hochgeladen / File/format is uploadable")
        option_form.addRow("Formate / Formats", self.formats)
        option_form.addRow("", self.upload_format)
        option_form.addRow("", self.covers)
        option_form.addRow("", self.compare_authors)
        option_form.addRow("", self.compare_title)
        option_form.addRow("", self.compare_isbn)
        option_form.addRow("", self.deletions)
        root.addWidget(options)

        self.progress_label = QLabel("Bereit / Ready")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.start = QPushButton("Synchronisierung starten / Start synchronization")
        self.debug = QPushButton("Debug / Diagnose")
        self.cancel = QPushButton("Abbrechen / Abort")
        self.cancel.setEnabled(False)
        self.start.clicked.connect(self.start_sync)
        self.debug.clicked.connect(self.debug_diagnose)
        self.cancel.clicked.connect(self.cancel_sync)
        root.addWidget(self.progress_label)
        root.addWidget(self.progress)
        action_buttons = QHBoxLayout()
        action_buttons.addWidget(self.debug)
        action_buttons.addWidget(self.start)
        root.addLayout(action_buttons)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.close)
        root.addWidget(self.cancel)
        root.addWidget(buttons)
        self.load_values()

    def debug_diagnose(self):
        values = self.values()
        results = diagnose_preparation(
            self.gui.current_db,
            values["preferred_formats"],
            values["upload_covers"],
            load_state(values["state"]),
        )
        DiagnosticDialog(self, format_diagnostic_report(results), self).exec()

    def load_values(self):
        values = settings()
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
        refresh_token, _ = normalize_refresh_token(self.refresh.text())
        return {
            "partner_id": self.partner.currentData(),
            "hardware_id": self.hardware.text().strip() or hardware_id(),
            "refresh_token": refresh_token,
            "username": settings()["username"],
            "password": settings()["password"],
            "preferred_formats": [x.strip().upper() for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "state": settings()["state"],
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
            for book_id in iter_book_ids(self.gui.current_db):
                item = self.gui.current_db.get_metadata(book_id)
                metadata[book_id] = {
                    "uuid": _metadata_value(item, "uuid"),
                    "title": _metadata_value(item, "title"),
                    "authors": _display_value(_metadata_value(item, "authors",
                                                               _metadata_value(item, "author"))),
                    "isbn": _display_value(_metadata_value(item, "isbn",
                                                            _metadata_value(item, "identifiers"))),
                    "formats": _metadata_value(item, "formats"),
                    "last_modified": str(_metadata_value(item, "last_modified")),
                }
            for item in metadata.values():
                item["formats"] = normalize_formats(item["formats"])
            state = load_state(settings["state"])
            client = TolinoClient(settings["partner_id"], settings["hardware_id"],
                                  settings["refresh_token"], settings["username"],
                                  settings["password"])
            client.login()
            comparison_fields = [
                name for name, checkbox in (
                    ("authors", self.compare_authors),
                    ("title", self.compare_title),
                    ("isbn", self.compare_isbn),
                ) if checkbox.isChecked()
            ]
            comparison = compare_inventory(
                metadata, state, client.inventory(), settings["preferred_formats"],
                comparison_fields,
            )
            dialog = InventoryDialog(comparison, self)
            if dialog.exec() != QDialog.Accepted:
                return
            selected_ids = dialog.selected_ids(comparison)
            uploads, removals, current = unpack_plan_result(plan_sync(
                metadata, state, settings["preferred_formats"],
                settings["enable_deletions"], selected_ids,
            ))
            jobs = []
            for upload in uploads:
                record = unpack_upload_record(upload)
                book_id = record["book_id"]
                book_uuid = record["book_uuid"]
                fmt = record["format_name"]
                old_id = record["old_id"]
                try:
                    path = safe_format_path(self.gui.current_db, book_id, fmt)
                except ValueError as exc:
                    title = metadata.get(book_id, {}).get("title") or "ohne Titel"
                    raise ValueError(
                        "Buch %s (%s): Format %s fehlt oder liefert keinen Pfad. %s" %
                        (book_id, title, fmt, exc)
                    ) from exc
                cover_path = None
                if settings["upload_covers"]:
                    cover = cover_bytes(self.gui.current_db, book_id)
                    if cover:
                        temp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
                        temp.write(cover)
                        temp.close()
                        cover_path = temp.name
                        self.temp_files.append(cover_path)
                jobs.append(SyncJob(book_id, book_uuid, fmt, old_id, path, cover_path))
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

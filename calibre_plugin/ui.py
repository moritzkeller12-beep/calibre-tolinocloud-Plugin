import os
import tempfile
import time
import traceback

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
try:
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QProgressDialog, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QTextEdit, QObject, QInputDialog,
                         QFileDialog, Qt, QTimer, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QProgressDialog, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QTextEdit, QObject, QInputDialog,
                         QFileDialog, Qt, QTimer, Signal as pyqtSignal)

try:
    from . import bootstrapper
    from .config import save_account, save_settings, settings
    from .sync import (compare_inventory, format_error_details, iter_book_ids,
                       load_state, metadata_by_id, plan_sync,
                       selected_book_ids, selected_table_rows,
                       sync_summary,
                       normalize_formats, safe_format_path, cover_bytes,
                       unpack_plan_result, unpack_upload_record,
                       diagnose_preparation, format_diagnostic_report,
                       custom_column_available, metadata_tolino_id,
                       update_tolino_ids, TOLINO_COLUMN, TOLINO_COLUMN_LABEL)
    from .tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                         hardware_id, normalize_refresh_token, sanitize_error,
                         scrape_browser_tokens, start_token_keepalive,
                         try_live_grab_first, validate_refresh_candidates)
except ImportError:
    bootstrapper = None
    from config import save_account, save_settings, settings
    from sync import (compare_inventory, format_error_details, iter_book_ids,
                      load_state, metadata_by_id, plan_sync,
                      selected_book_ids, selected_table_rows,
                      sync_summary,
                      normalize_formats, safe_format_path, cover_bytes,
                      unpack_plan_result, unpack_upload_record,
                      diagnose_preparation, format_diagnostic_report,
                      custom_column_available, metadata_tolino_id,
                      update_tolino_ids, TOLINO_COLUMN, TOLINO_COLUMN_LABEL)
    from tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                        hardware_id, normalize_refresh_token, sanitize_error,
                        scrape_browser_tokens, start_token_keepalive,
                        try_live_grab_first, validate_refresh_candidates)


def _plugin_version():
    """Plugin version tuple for display; safe outside Calibre."""
    try:
        from . import PLUGIN_VERSION
        return PLUGIN_VERSION
    except Exception:
        return (0, 9, 0)


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
    HEADERS = ("Upload", "Status", "Titel", "Autor",
               "ISBN", "Tolino-ID", "Hinweis")

    def __init__(self, rows, parent=None, client_factory=None):
        QDialog.__init__(self, parent)
        self.rows = list(rows)
        self.client_factory = client_factory
        self.setWindowTitle("Bestandsvergleich")
        self.setMinimumSize(900, 420)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Häkchen setzen, was hochgeladen werden soll, dann \u201eAuswahl "
            "synchronisieren\u201c klicken. Cloud-Aktionen wirken auf die "
            "markierte Zeile."))
        self.table = QTableWidget(len(rows), len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.checks = []
        status_text = {
            "new_in_calibre": "Neu in Calibre",
            "only_tolino": "Nur Tolino",
            "identical": "Identisch",
            "changed": "Geändert",
            "duplicate_tolino": "Doppelter Tolino-Titel",
            "not_matchable": "Nicht matchbar",
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
                row.get("explanation", ""),
            )
            for column, value in enumerate(values, 1):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        # Cloud actions live inside this window and use its own table, so
        # they can never outlive it (the deleted-QTableWidget crash).
        actions = QGroupBox("Aktionen für die markierte Zeile")
        action_row = QHBoxLayout()
        self.download_btn = QPushButton("Herunterladen")
        self.collection_add_btn = QPushButton("Zur Sammlung")
        self.collection_rm_btn = QPushButton("Aus Sammlung")
        self.mark_read_btn = QPushButton("Gelesen markieren")
        for button in (self.download_btn, self.collection_add_btn,
                       self.collection_rm_btn, self.mark_read_btn):
            action_row.addWidget(button)
        actions.setLayout(action_row)
        layout.addWidget(actions)
        self.download_btn.clicked.connect(self.download_selected)
        self.collection_add_btn.clicked.connect(self.add_selected_to_collection)
        self.collection_rm_btn.clicked.connect(self.remove_selected_from_collection)
        self.mark_read_btn.clicked.connect(self.mark_selected_read)

        buttons = QHBoxLayout()
        confirm = QPushButton("Auswahl synchronisieren")
        cancel = QPushButton("Abbrechen")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(confirm)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)

    def selected_ids(self, rows=None):
        rows = self.rows if rows is None else rows
        if len(rows) != len(self.checks):
            raise ValueError("Inventory table and selection controls have different row counts.")
        return selected_book_ids([
            dict(row, selected=check.isChecked()) for row, check in zip(rows, self.checks)
        ])

    # --- Cloud actions on the currently selected table row ---------------

    def _selected_row(self):
        """The comparison row for the selection, or None with a hint."""
        selected = selected_table_rows(self.table)
        if not selected:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Bitte eine Zeile markieren.")
            return None
        row_index = selected[0]
        if row_index >= len(self.rows):
            return None
        return self.rows[row_index]

    def _client(self):
        if self.client_factory is None:
            QMessageBox.warning(
                self, "Tolino Cloud Sync",
                "Keine Anmeldung verfügbar.")
            return None
        try:
            return self.client_factory()
        except Exception as exc:
            error_dialog(self, "Anmeldung fehlgeschlagen",
                         sanitize_error(exc), show=True)
            return None

    def download_selected(self):
        row = self._selected_row()
        if row is None:
            return
        tolino_id = row.get("tolino_id")
        if not tolino_id:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Diese Zeile hat keine Tolino-ID (nur in der Cloud vorhandene "
                "Bücher können heruntergeladen werden).")
            return
        try:
            client = self._client()
            if client is None:
                return
            content, _metadata = client.download(tolino_id)
        except Exception as exc:
            error_dialog(self, "Download fehlgeschlagen",
                         sanitize_error(exc), show=True)
            return
        suggested = "%s.epub" % (row.get("title") or "tolino-book")
        path, _ = QFileDialog.getSaveFileName(
            self, "EPUB speichern", suggested, "EPUB (*.epub)")
        if not path:
            return
        with open(path, "wb") as handle:
            handle.write(content)
        info_dialog(self, "Tolino Cloud Sync",
                    "Buch gespeichert: %s" % path, show_copy_button=False)

    def _collection_action(self, action, title):
        row = self._selected_row()
        if row is None:
            return
        tolino_id = row.get("tolino_id")
        if not tolino_id:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Diese Zeile hat keine Tolino-ID.")
            return
        name, ok = QInputDialog.getText(
            self, title, "Sammlungsname:")
        if not ok or not str(name).strip():
            return
        try:
            client = self._client()
            if client is None:
                return
            action(client, tolino_id, str(name).strip())
        except Exception as exc:
            error_dialog(self, "Sammlung fehlgeschlagen",
                         sanitize_error(exc), show=True)
            return
        info_dialog(self, "Tolino Cloud Sync",
                    "Sammlung aktualisiert: %s" % name, show_copy_button=False)

    def add_selected_to_collection(self):
        self._collection_action(
            lambda client, book, name: client.add_to_collection(book, name),
            "Zur Sammlung hinzufügen")

    def remove_selected_from_collection(self):
        self._collection_action(
            lambda client, book, name: client.remove_from_collection(book, name),
            "Aus Sammlung entfernen")

    def mark_selected_read(self):
        row = self._selected_row()
        if row is None:
            return
        tolino_id = row.get("tolino_id")
        if not tolino_id:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Diese Zeile hat keine Tolino-ID.")
            return
        try:
            client = self._client()
            if client is None:
                return
            client.mark_read(tolino_id, finished=True)
        except Exception as exc:
            error_dialog(self, "Markieren fehlgeschlagen",
                         sanitize_error(exc), show=True)
            return
        info_dialog(self, "Tolino Cloud Sync",
                    "Als gelesen markiert.",
                    show_copy_button=False)


class DiagnosticDialog(QDialog):
    def __init__(self, dashboard, report, parent=None):
        QDialog.__init__(self, parent)
        self.dashboard = dashboard
        self.setWindowTitle("Diagnose")
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
        copy = QPushButton("Diagnose kopieren")
        copy.clicked.connect(self.copy_report)
        test = QPushButton("Tolino-Antwort testen")
        test.clicked.connect(self.test_tolino)
        close = QPushButton("Schließen")
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
                                  values["password"],
                                  token_callback=self.dashboard.persist_refresh_token)
            client.login()
            self.dashboard.set_refresh_token(client.refresh)
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
                "error_message": sanitize_error(
                    exc, (values["refresh_token"], getattr(auth, "access", None))
                ),
                "traceback": sanitize_error(
                    traceback.format_exc(),
                    (values["refresh_token"], getattr(auth, "access", None)),
                ),
            }
        self.output.append("\n" + format_diagnostic_report([result]))


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object, object, object)
    failed = pyqtSignal(str, str)

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
        updates = []
        try:
            client = TolinoClient(self.settings["partner_id"], self.settings["hardware_id"],
                                  self.settings["refresh_token"], self.settings["username"],
                                  self.settings["password"],
                                  token_callback=self.persist_refresh_token,
                                  hardware_callback=self.persist_hardware)
            client.login()
            needs_replacement_cleanup = any(job.old_id for job in self.jobs)
            remote_ids = (client.inventory_ids()
                          if self.settings["enable_deletions"] or needs_replacement_cleanup
                          else set())
            total = len(self.jobs) + len(self.removals)
            done = 0
            for job in self.jobs:
                if self.cancelled:
                    raise RuntimeError("Synchronisierung vom Benutzer abgebrochen.")
                self.progress.emit(done, total, "Lade hoch: %s (%s)" %
                                   (job.book_uuid, job.format_name))
                new_id = client.upload(job.path)
                state[job.book_uuid] = {
                    "tolino_id": new_id,
                    "fingerprint": state.get(job.book_uuid, {}).get("fingerprint", ""),
                    "calibre_id": job.book_id,
                }
                updates.append((job.book_id, new_id))
                if job.cover_path:
                    client.upload_cover(new_id, job.cover_path)
                if job.old_id and str(job.old_id) in remote_ids:
                    client.delete(job.old_id)
                done += 1
                self.progress.emit(done, total, "Hochgeladen: %s" % job.book_uuid)
            for book_uuid, tolino_id in self.removals:
                if self.cancelled:
                    raise RuntimeError("Synchronisierung vom Benutzer abgebrochen.")
                if str(tolino_id) in remote_ids:
                    self.progress.emit(done, total, "Lösche: %s" % book_uuid)
                    client.delete(tolino_id)
                state.pop(book_uuid, None)
                done += 1
                self.progress.emit(done, total, "Erledigt: %s" % book_uuid)
            self.completed.emit(state, client.refresh, updates)
        except Exception as exc:
            self.failed.emit(sanitize_error(
                exc, (self.settings["refresh_token"], getattr(client, "access", None))
            ), client.refresh if client else self.settings["refresh_token"])

    def persist_refresh_token(self, refresh):
        self.settings["refresh_token"] = refresh
        save_account(self.settings["account_name"], {
            "refresh_token": refresh, "hardware_id": self.settings["hardware_id"],
        }, active=self.settings["account_name"])

    def persist_hardware(self, hardware):
        """Adopted device id from the BOSH recovery (0.9.35)."""
        self.settings["hardware_id"] = hardware
        save_account(self.settings["account_name"], {
            "refresh_token": self.settings["refresh_token"],
            "hardware_id": hardware,
        }, active=self.settings["account_name"])


# Browser sign-in keeps a module-level reference to its worker thread: the
# dashboard may be closed while the flow is still polling the browser
# storages, and a parented QThread destroyed while running crashes Calibre.
_LOGIN_WORKER_THREADS = []


def _reap_login_thread(thread):
    try:
        _LOGIN_WORKER_THREADS.remove(thread)
    except ValueError:
        pass


class BrowserLoginWorker(QObject):
    """Runs browser_login off the GUI thread (it polls for minutes)."""
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(str)
    # Live-Fortschrittstexte des Grabs ("Fenster offen -- anmelden",
    # "warte auf einen neu geschriebenen Token ...") direkt in die
    # Statuszeile.
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
            self.failed.emit("%s: %s" % (type(exc).__name__,
                                         sanitize_error(exc)))
            return
        self.completed.emit(refresh, hardware)


class SyncDashboard(QDialog):
    def __init__(self, gui):
        QDialog.__init__(self, gui)
        self.gui = gui
        self.thread = None
        self.worker = None
        self.login_thread = None
        self.login_worker = None
        self.sync_column_enabled = False
        self.temp_files = []
        self.setWindowTitle("Tolino Cloud Sync (Plugin-Version %s)" %
                            ".".join(str(v) for v in _plugin_version()))
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        setup = QGroupBox("1. Anmeldung")
        setup_form = QFormLayout(setup)
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem("%s - %s" % (pid, partner["name"]), pid)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(QLineEdit.Password)
        self.status = QLabel()
        self.browser = QPushButton(
            "Im Browser anmelden (frischen Token holen)")
        self.browser.clicked.connect(self.browser_login)
        self.scrape_browser = QPushButton(
            "Token aus laufendem Web Reader übernehmen")
        self.scrape_browser.clicked.connect(self.scrape_browser_tokens)
        self.install_curl_cffi = QPushButton(
            "Bot-Schutz-Komponente installieren (einmalig)")
        self.install_curl_cffi.clicked.connect(self.install_curl_cffi_clicked)
        setup_form.addRow("Buchhändler", self.partner)
        setup_form.addRow("", self.browser)
        setup_form.addRow("", self.scrape_browser)
        setup_form.addRow("", self.install_curl_cffi)
        setup_form.addRow("", self.status)
        setup_form.addRow("Hardware ID (automatisch erkannt)", self.hardware)
        setup_form.addRow("Refresh token", self.refresh)
        root.addWidget(setup)

        options = QGroupBox("2. Optionen (Standard reicht)")
        option_form = QFormLayout(options)
        self.formats = QLineEdit()
        self.covers = QCheckBox("Covers hochladen")
        self.deletions = QCheckBox("Löschungen erlauben")
        self.tolino_column = QCheckBox(
            "Tolino-ID-Spalte verwenden, wenn vorhanden")
        self.compare_authors = QCheckBox("Autor (nur Vergleich/Filter)")
        self.compare_title = QCheckBox("Buchtitel (nur Vergleich/Filter)")
        self.compare_isbn = QCheckBox("ISBN (nur Vergleich/Filter)")
        for field in (self.compare_authors, self.compare_title, self.compare_isbn):
            field.setChecked(True)
        option_form.addRow("Formate", self.formats)
        option_form.addRow("", self.covers)
        option_form.addRow("", self.compare_authors)
        option_form.addRow("", self.compare_title)
        option_form.addRow("", self.compare_isbn)
        option_form.addRow("", self.deletions)
        option_form.addRow("", self.tolino_column)
        root.addWidget(options)

        accounts = QGroupBox("Konten (optional)")
        account_form = QFormLayout(accounts)
        self.account_select = QComboBox()
        self.account_new = QPushButton("Neues Konto")
        self.account_remove = QPushButton("Konto löschen")
        account_buttons = QHBoxLayout()
        account_buttons.addWidget(self.account_new)
        account_buttons.addWidget(self.account_remove)
        self.account_select.currentIndexChanged.connect(self.account_changed)
        self.account_new.clicked.connect(self.new_account)
        self.account_remove.clicked.connect(self.remove_account)
        account_form.addRow("Konto", self.account_select)
        account_form.addRow("", account_buttons)
        root.addWidget(accounts)

        self.progress_label = QLabel("Bereit")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.start = QPushButton("3. Synchronisierung starten")
        self.debug = QPushButton("Diagnose")
        self.cancel = QPushButton("Abbrechen")
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
        root.addWidget(self.cancel)
        # Plain close button instead of QDialogButtonBox: immune to the Qt6
        # "Invalid ButtonRole, button not added" warning seen in the field.
        close = QPushButton("Schließen")
        close.clicked.connect(self.close)
        root.addWidget(close)
        self.load_values()

    def _save_visible_account(self):
        if not getattr(self, "account_name", None):
            return
        save_settings({
            "preferred_formats": [x.strip().upper()
                                  for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
        })
        save_account(self.account_name, self.values(), active=self.account_name)

    def account_changed(self, index):
        if index < 0 or not getattr(self, "account_names", None):
            return
        self._save_visible_account()
        self.account_name = self.account_select.itemData(index)
        account = next(item for item in settings()["accounts"]
                       if item["name"] == self.account_name)
        self.partner.setCurrentIndex(max(0, self.partner.findData(account["partner_id"])))
        self.hardware.setText(account["hardware_id"] or hardware_id())
        self.refresh.setText(account["refresh_token"])
        self.update_status()

    def new_account(self):
        self._save_visible_account()
        base, accepted = QInputDialog.getText(
            self, "Neues Konto", "Name:")
        base = str(base).strip()
        if not accepted or not base:
            return
        names = set(self.account_names)
        name, number = base, 2
        while name in names:
            name = "%s-%d" % (base, number)
            number += 1
        save_account(name, {"name": name}, active=name)
        self.load_values()
        self.account_select.setCurrentIndex(self.account_select.findData(name))

    def remove_account(self):
        if len(self.account_names) <= 1:
            return
        name = self.account_name
        remaining = [item for item in settings()["accounts"] if item["name"] != name]
        save_settings({"accounts": remaining,
                       "active_account": remaining[0]["name"]})
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
        self.account_names = [item["name"] for item in values["accounts"]]
        self.account_select.blockSignals(True)
        self.account_select.clear()
        for name in self.account_names:
            self.account_select.addItem(name, name)
        self.account_name = values["active_account"]
        self.account_select.setCurrentIndex(
            max(0, self.account_select.findData(self.account_name)))
        self.account_select.blockSignals(False)
        self.partner.setCurrentIndex(max(0, self.partner.findData(values["partner_id"])))
        self.hardware.setText(values["hardware_id"] or hardware_id())
        self.refresh.setText(values["refresh_token"])
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.covers.setChecked(values["upload_covers"])
        self.deletions.setChecked(values["enable_deletions"])
        self.tolino_column.setChecked(values["use_tolino_column"])
        self.update_status()

    def update_status(self):
        self.status.setText("Angemeldet" if self.refresh.text().strip()
                            else "Nicht angemeldet")

    def set_refresh_token(self, refresh):
        normalized, _ = normalize_refresh_token(refresh)
        self.refresh.setText(normalized)
        self.update_status()

    def persist_refresh_token(self, refresh):
        self.set_refresh_token(refresh)
        save_account(self.account_name, {
            "refresh_token": self.refresh.text(),
            "hardware_id": self.hardware.text().strip() or hardware_id(),
        }, active=self.account_name)

    def persist_hardware(self, hardware):
        """Adopted device id from the BOSH recovery (0.9.35)."""
        self.hardware.setText(str(hardware))
        save_account(self.account_name, {
            "refresh_token": self.refresh.text(),
            "hardware_id": str(hardware),
        }, active=self.account_name)

    def _validate_scraped_candidates(self, refreshes, hardwares):
        """Validate scraped candidates live; return the fresh pair or None.

        The token exchange itself does not involve the hardware ID (the
        Web Reader's own token POST carries none either), so ONE pass over
        the candidates is enough: the earlier loop over up to three
        hardware IDs sent two redundant replays per token, and a replayed
        token can trip Keycloak's reuse protection and kill the session.
        The adopted hardware ID comes back from the token/device flow via
        ``validate_refresh_candidates``.
        """
        return validate_refresh_candidates(
            self.partner.currentData(), "", refreshes)

    def scrape_browser_tokens(self):
        """Extract a working refresh_token and hardware_id from browsers.

        Browser storage keeps spent tokens from earlier background rotations
        and scan order is not recency order, so every candidate is validated
        against the token endpoint until one is accepted. Only the fresh,
        rotated token of the accepted grant is persisted.
        """
        try:
            # A grabber window from "Im Browser anmelden" (or an earlier
            # extract attempt) holds the CURRENT token of the signed-in
            # reader -- always better than any disk copy, whose tokens
            # are history after the reader's background rotation. Try the
            # live read first; scrape the disks only when no grabber
            # window is running.
            grabbed = try_live_grab_first(
                self.partner.currentData(), self.hardware.text().strip())
            if grabbed:
                refresh_token, hardware_id_value = grabbed
                if hardware_id_value:
                    self.hardware.setText(str(hardware_id_value))
                self.persist_refresh_token(refresh_token)
                QMessageBox.information(
                    self, "Token extrahiert",
                    "Aktueller Token live aus dem ge\u00f6ffneten Web "
                    "Reader gelesen und gespeichert (Hardware-ID: %s). "
                    "Das Anmeldefenster wurde geschlossen."
                    % (hardware_id_value or "unver\u00e4ndert"))
                return
            refreshes, hardwares, notes = scrape_browser_tokens(
                diagnose=True, all_candidates=True)
            if refreshes:
                validated = self._validate_scraped_candidates(
                    refreshes, hardwares)
                if validated:
                    refresh_token, hardware_id = validated
                    if hardware_id:
                        self.hardware.setText(hardware_id)
                    self.refresh.setText(refresh_token)
                    self.persist_refresh_token(refresh_token)
                    QMessageBox.information(
                        self, "Token extrahiert",
                        "Frischer Refresh-Token gefunden und gespeichert "
                        "(aus %d Kandidaten validiert). Hardware-ID: %s"
                        % (len(refreshes), hardware_id or "unverändert")
                    )
                    return
                self._offer_spent_token_retry(len(refreshes), notes)
                return
            result = scrape_browser_tokens(diagnose=True)
            refresh_token, hardware_id = result[0], result[1]
            notes = result[2] if len(result) > 2 else []
            detail = "\n".join("- %s" % note for note in notes) or \
                "- Kein Browserprofil gefunden"
            QMessageBox.warning(
                self, "Keine Token gefunden",
                "Keine Tolino-Web-Reader-Tokens gefunden.\n\n"
                "1. Im Web Reader (Bibliothek) anmelden – nicht nur "
                "im Shop.\n"
                "2. Die Bücherliste komplett laden.\n"
                "3. ALLE Browserfenster schließen und sofort danach "
                "hier nachdrücken – nur der neueste Token einer Sitzung "
                "ist gültig.\n\n"
                "Besser: \u201eIm Browser anmelden\u201c – der liest den "
                "Token direkt aus dem Browser-Fenster.\n\n"
                "Befund:\n%s" % detail
            )
        except TolinoAuthError as exc:
            QMessageBox.warning(
                self, "Live-\u00dcbernahme",
                "%s\n\nDas Anmeldefenster ist noch offen: dort im Web "
                "Reader anmelden (B\u00fccherliste laden) und den Knopf "
                "erneut dr\u00fccken. Oder das Fenster schlie\u00dfen und "
                "alle anderen Browserfenster schlie\u00dfen, um die "
                "Festplatten-Kopien zu pr\u00fcfen." % sanitize_error(exc))
        except Exception as exc:
            QMessageBox.critical(
                self, "Fehler",
                "Fehler beim Extrahieren der Tokens: %s" % sanitize_error(exc)
            )

    def _offer_spent_token_retry(self, count, notes):
        """Explain spent candidates and offer a 30 s background retry poll.

        The Web Reader rotates its refresh token on every background
        refresh, so a fresh token usually lands in the browser storage
        within a minute or two of the reader being used. Instead of making
        the user re-click the button, offer an automatic poll.
        """
        detail = "\n".join("- %s" % note for note in notes) or \
            "- Kein Browserprofil gefunden"
        answer = QMessageBox.question(
            self, "Token verbraucht",
            "Es wurden %d Refresh-Token gefunden, aber alle waren bereits "
            "verbraucht (invalid grant).\n\n"
            "Nur der neueste Token einer Sitzung ist gültig – "
            "Festplatten-Kopien sind deshalb fast immer alt.\n\n"
            "Besser: \u201eIm Browser anmelden\u201c benutzen. Oder ALLE "
            "Browserfenster schließen (der Reader schreibt dann seinen "
            "letzten Token auf die Festplatte) und diesen Knopf erneut "
            "drücken.\n\n"
            "Jetzt 5 Minuten alle 30 Sekunden weiterprüfen?\n\n"
            "Befund:\n%s" % (count, detail),
            QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self._stop_scrape_retry()
        self._scrape_retry_deadline = time.time() + 5 * 60
        timer = QTimer(self)
        timer.setInterval(30 * 1000)
        timer.timeout.connect(self._scrape_retry_tick)
        self._scrape_retry_timer = timer
        timer.start()
        try:
            self.status.setText(
                "Warte auf frischen Token (Prüfung alle 30 s, bis zu "
                "5 Minuten) – Web Reader geöffnet lassen ...")
        except RuntimeError:
            pass

    def _scrape_retry_tick(self):
        """Re-scrape and validate once per poll interval until fresh."""
        if time.time() > getattr(self, "_scrape_retry_deadline", 0):
            self._stop_scrape_retry()
            QMessageBox.information(
                self, "Token verbraucht",
                "Auch nach 5 Minuten kam kein frischer Token an. Melde "
                "dich im Web Reader (Bibliothek) neu an, lade ihn einmal "
                "neu (F5) und starte die Browser-Anmeldung erneut.")
            return
        try:
            refreshes, hardwares, _notes = scrape_browser_tokens(
                diagnose=True, all_candidates=True)
        except Exception:
            return  # transient scan error: try again on the next tick
        if not refreshes:
            return
        validated = self._validate_scraped_candidates(refreshes, hardwares)
        if not validated:
            return
        self._stop_scrape_retry()
        refresh_token, hw = validated
        if hw:
            self.hardware.setText(str(hw))
        self.refresh.setText(refresh_token)
        self.persist_refresh_token(refresh_token)
        self.update_status()
        QMessageBox.information(
            self, "Token extrahiert",
            "Frischer Refresh-Token gefunden und gespeichert.")

    def _stop_scrape_retry(self):
        """Stop the 30 s retry poll (new poll, success or shutdown)."""
        timer = getattr(self, "_scrape_retry_timer", None)
        if timer is not None:
            timer.stop()
        self._scrape_retry_timer = None
        try:
            self.status.setText("Bereit")
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def install_curl_cffi_clicked(self):
        """One-click install of curl_cffi wheels (pinned, checksum-verified)."""
        if bootstrapper is None:
            QMessageBox.critical(
                self, "curl_cffi",
                "Interner Fehler: Bootstrapper-Modul fehlt im Plugin-Paket.")
            return
        installed, importable, plugin_dir = bootstrapper.setup_status()
        if importable:
            QMessageBox.information(
                self, "curl_cffi",
                "curl_cffi ist bereits installiert und importierbar "
                "(Diagnose zeigt curl_cffi: true).")
            return
        confirm = QMessageBox.question(
            self, "curl_cffi installieren",
            "Es werden die offiziellen, versionierten curl_cffi-Räder "
            "(Version %s) von PyPI geladen, ihre SHA-256-Prüfsummen "
            "geprüft und in den Calibre-Plugin-Ordner entpackt:\n%s\n\n"
            "Fortfahren?" % (bootstrapper.CURL_CFFI_VERSION, plugin_dir))
        if confirm != QMessageBox.Yes:
            return
        progress = QProgressDialog(
            "curl_cffi wird installiert ...", None, 0, 0, self)
        progress.setWindowTitle("curl_cffi Installation")
        progress.setWindowModality(Qt.WindowModal)
        try:
            def step(text):
                progress.setLabelText(text)
                from qt.core import QCoreApplication
                QCoreApplication.processEvents()

            bootstrapper.install(progress=step)
        except Exception as exc:
            QMessageBox.critical(
                self, "curl_cffi Installation fehlgeschlagen",
                "Fehler bei der Installation: %s" % sanitize_error(exc))
            return
        finally:
            progress.cancel()
        session_factory = bootstrapper.import_from_plugin_dir()
        if session_factory is not None:
            QMessageBox.information(
                self, "curl_cffi installiert",
                "curl_cffi wurde installiert und ist sofort nutzbar. "
                "Testen Sie jetzt \u201eTolino-Antwort testen\u201c.")
        else:
            QMessageBox.information(
                self, "curl_cffi installiert",
                "curl_cffi wurde nach %s entpackt. Bitte Calibre neu "
                "starten, damit die Module geladen werden." % plugin_dir)

    def values(self):
        refresh_token, _ = normalize_refresh_token(self.refresh.text())
        return {
            "account_name": self.account_name,
            "partner_id": self.partner.currentData(),
            "hardware_id": self.hardware.text().strip() or hardware_id(),
            "refresh_token": refresh_token,
            "username": settings()["username"],
            "password": settings()["password"],
            "preferred_formats": [x.strip().upper() for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
            "state": settings()["state"],
        }

    def browser_login(self):
        """Open the private Chromium sign-in window, adopt the fresh token.

        Primary path: a dedicated Chromium window (DevTools protocol) on
        the partner's Web Reader, where the CURRENT token is read from the
        live page; without a Chromium binary the same flow falls back to
        the disk-scrape login in the system browser (closed windows only).

        Both paths can poll for minutes (the reader writes its fresh token
        on its own schedule), so the work runs in a worker thread: blocking
        the GUI thread froze Calibre and could crash it.
        """
        if self.login_thread is not None and self.login_thread.isRunning():
            return  # a sign-in attempt is already running
        partner_id = self.partner.currentData()
        self.browser.setEnabled(False)
        self.status.setText(
            "Browser-Anmeldung läuft: im Anmeldefenster im Web Reader "
            "(Bibliothek) anmelden und die Bücherliste laden. Fenster "
            "offen lassen – der Token wird live übernommen.")
        thread = QThread()  # no parent: dialog may close first
        worker = BrowserLoginWorker(partner_id, self.hardware.text().strip())
        worker.moveToThread(thread)
        worker.progress.connect(self._login_progress)
        thread.started.connect(worker.run)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.completed.connect(self.login_completed)
        worker.failed.connect(self.login_failed)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: _reap_login_thread(thread))
        _LOGIN_WORKER_THREADS.append(thread)
        self.login_thread = thread
        self.login_worker = worker
        thread.start()

    def login_completed(self, refresh, hardware):
        try:
            self._login_cleanup()
            self.set_refresh_token(refresh)
            if hardware:
                self.hardware.setText(str(hardware))
            self.persist_refresh_token(refresh)
            self.update_status()
            QMessageBox.information(
                self, "Anmeldung erfolgreich",
                "Der neue Refresh-Token wurde sofort gespeichert.")
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def login_failed(self, message):
        try:
            self._login_cleanup()
            QMessageBox.warning(self, "Browser-Anmeldung",
                                sanitize_error(message))
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def _login_progress(self, text):
        """Live-Hinweise des Browser-Grabs in der Statuszeile zeigen."""
        try:
            self.status.setText(str(text))
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def _login_cleanup(self):
        self.login_thread = None
        self.login_worker = None
        try:
            self.browser.setEnabled(True)
            self.status.setText("Bereit")
        except RuntimeError:
            pass  # dialog already destroyed

    def _cloud_client(self):
        """Build a logged-in client from the current dialog values.

        Raises on failure; the inventory dialog turns that into an error
        dialog. The rotated refresh token is persisted immediately.
        """
        settings = self.values()
        if not settings["refresh_token"]:
            raise TolinoAuthError(
                "Bitte zuerst einen Refresh-Token konfigurieren.")
        self._save_visible_account()
        client = TolinoClient(
            settings["partner_id"], settings["hardware_id"],
            settings["refresh_token"], settings["username"],
            settings["password"], token_callback=self.persist_refresh_token,
            hardware_callback=self.persist_hardware)
        client.login()
        self.set_refresh_token(client.refresh)
        return client

    def start_sync(self):
        if self.thread is not None:
            return
        settings = self.values()
        if not settings["refresh_token"]:
            QMessageBox.warning(self, "Konfiguration",
                                "Bitte zuerst einen Refresh-Token konfigurieren.")
            return
        self.sync_column_enabled = (
            settings["use_tolino_column"] and
            custom_column_available(self.gui.current_db)
        )
        try:
            metadata = {}
            for book_id in iter_book_ids(self.gui.current_db):
                item = metadata_by_id(self.gui.current_db, book_id)
                metadata[book_id] = {
                    "uuid": _metadata_value(item, "uuid"),
                    "title": _metadata_value(item, "title"),
                    "authors": _display_value(_metadata_value(item, "authors",
                                                               _metadata_value(item, "author"))),
                    "isbn": _display_value(_metadata_value(item, "isbn",
                                                            _metadata_value(item, "identifiers"))),
                    "formats": _metadata_value(item, "formats"),
                    "last_modified": str(_metadata_value(item, "last_modified")),
                    "tolino_id": metadata_tolino_id(item) if self.sync_column_enabled else "",
                }
            for item in metadata.values():
                item["formats"] = normalize_formats(item["formats"])
            state = load_state(settings["state"])
            client = TolinoClient(settings["partner_id"], settings["hardware_id"],
                                  settings["refresh_token"], settings["username"],
                                  settings["password"],
                                  token_callback=self.persist_refresh_token,
                                  hardware_callback=self.persist_hardware)
            client.login()
            self.set_refresh_token(client.refresh)
            # CRITICAL: Ensure the new refresh token is persisted immediately
            # This prevents "reuse exceeded" errors during sync
            if client.refresh != settings["refresh_token"]:
                self.persist_refresh_token(client.refresh)
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
                use_metadata_ids=(self.sync_column_enabled and
                                  len(settings["accounts"]) == 1),
            )
            dialog = InventoryDialog(
                comparison, self,
                client_factory=lambda: self._cloud_client())
            if dialog.exec() != QDialog.Accepted:
                return
            # The checkbox state of the confirmed dialog decides what is
            # uploaded; an empty selection simply syncs nothing.
            selected = dialog.selected_ids()
            uploads, removals, current = unpack_plan_result(plan_sync(
                metadata, state, settings["preferred_formats"],
                settings["enable_deletions"], upload_book_ids=selected,
            ))
            jobs = []
            skipped_uploads = []
            for upload in uploads:
                record = unpack_upload_record(upload)
                book_id = record["book_id"]
                book_uuid = record["book_uuid"]
                fmt = record["format_name"]
                old_id = record["old_id"]
                try:
                    path = safe_format_path(self.gui.current_db, book_id, fmt)
                    if not os.path.isfile(path):
                        raise ValueError("Pfad existiert nicht: %s" % path)
                except ValueError as exc:
                    title = metadata.get(book_id, {}).get("title") or "ohne Titel"
                    skipped_uploads.append(
                        "Buch %s (%s), Format %s: %s" % (book_id, title, fmt, exc))
                    continue
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
            error_dialog(
                self, "Vorbereitung fehlgeschlagen",
                format_error_details(exc, (settings.get("refresh_token"),)),
                show=True,
            )
            return
        if skipped_uploads:
            QMessageBox.warning(
                self, "Tolino Cloud Sync",
                "Einige Uploads wurden übersprungen:\n\n%s" %
                "\n".join(skipped_uploads))
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
        self.progress_label.setText("Synchronisierung läuft")
        # Ensure settings are up-to-date with the latest refresh token
        settings["refresh_token"] = self.refresh.text().strip()

    def progress_changed(self, done, total, text):
        self.progress.setRange(0, total or 1)
        self.progress.setValue(done)
        self.progress_label.setText(text)

    def cancel_sync(self):
        if self.worker:
            self.worker.cancel()
            self.progress_label.setText("Abbruch angefordert")
            self.cancel.setEnabled(False)

    def sync_completed(self, state, refresh, updates):
        # CRITICAL: Update UI with the latest refresh token from worker
        # This prevents "invalid token" errors on subsequent sync attempts
        if refresh and refresh != self.refresh.text().strip():
            self.set_refresh_token(refresh)
            self.persist_refresh_token(refresh)
        update_tolino_ids(self.gui.current_db, updates, enabled=self.sync_column_enabled)
        save_account(self.account_name, {
            "state": state, "refresh_token": refresh,
            "hardware_id": self.hardware.text().strip() or hardware_id(),
        }, active=self.account_name)
        self.finish_thread()
        info_dialog(self, "Tolino Cloud Sync", "Synchronisierung abgeschlossen.",
                    show_copy_button=False)

    def sync_failed(self, message, refresh):
        self.set_refresh_token(refresh)
        self.finish_thread()
        error_dialog(
            self, "Synchronisierung fehlgeschlagen",
            sanitize_error(message, (self.values()["refresh_token"],)),
            show=True,
        )

    def finish_thread(self):
        self._save_visible_account()
        if self.thread:
            self.thread.quit()
            self.thread.wait()
        self.thread = None
        self.worker = None
        self.start.setEnabled(True)
        self.cancel.setEnabled(False)
        self.progress_label.setText("Bereit")
        for path in self.temp_files:
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_files = []
        self.update_status()

    def closeEvent(self, event):
        self._stop_scrape_retry()
        if self.thread:
            self.cancel_sync()
            self.thread.quit()
            self.thread.wait()
        # A running browser sign-in is left alone on purpose: its thread is
        # unparented and self-terminates after its timeout, and joining it
        # here could freeze the close for minutes.
        self.login_thread = None
        self.login_worker = None
        self._save_visible_account()
        event.accept()


class TolinoSyncAction(InterfaceAction):
    name = "Tolino Cloud Sync"
    action_spec = ("Tolino Cloud Sync", None,
                   "Tolino-Cloud-Dashboard öffnen", None)

    def genesis(self):
        self.qaction.triggered.connect(self.show_dashboard)
        self.menu = self.qaction
        _apply_toolbar_icon(self)
        # Keycloak refresh tokens expire after ~1h idle (refresh_expires_in
        # ~3598); the keep-alive rotates the stored token every 45 minutes
        # while Calibre runs, so an adopted token survives between syncs.
        try:
            start_token_keepalive(self._keepalive_credentials,
                                  self._keepalive_persist)
        except Exception:
            pass  # keep-alive must never break plugin loading

    def _keepalive_credentials(self):
        try:
            cfg = settings()
        except Exception:
            return {}
        return {
            "partner_id": cfg.get("partner_id"),
            "hardware_id": cfg.get("hardware_id") or "",
            "refresh_token": cfg.get("refresh_token") or "",
            "username": cfg.get("username"),
            "password": cfg.get("password"),
        }

    def _keepalive_persist(self, refresh):
        try:
            cfg = settings()
            name = cfg.get("account_name")
            if name and refresh:
                save_account(name, {"refresh_token": refresh}, active=name)
        except Exception:
            pass  # best-effort persistence

    def show_dashboard(self):
        dialog = SyncDashboard(self.gui)
        dialog.exec()


def _apply_toolbar_icon(action):
    """Give the toolbar action its icon via the robust fallback chain."""
    try:
        from .icons import toolbar_icon
    except ImportError:
        from icons import toolbar_icon
    except Exception:
        return
    try:
        icon = toolbar_icon()
    except Exception:
        icon = None
    if icon is None or icon.isNull():
        return
    try:
        action.qaction.setIcon(icon)
    except Exception:
        action.setIcon(icon)

import os
import tempfile
import traceback

from calibre.gui2 import error_dialog, info_dialog
from calibre.gui2.actions import InterfaceAction
try:
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QTextEdit, QObject, QInputDialog,
                         QFileDialog, Qt, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QTextEdit, QObject, QInputDialog,
                         QFileDialog, Qt, Signal as pyqtSignal)

try:
    from .config import (account_by_name, save_account, save_settings,
                         set_active_account, settings)
    from .sync import (compare_inventory, format_error_details, iter_book_ids,
                       load_state, metadata_by_id, plan_sync, plan_metadata_updates,
                       plan_collection_updates, selected_book_ids,
                       selected_table_rows, series_map_from_metadata,
                       sort_comparison_rows, sync_summary,
                       normalize_formats, safe_format_path, cover_bytes,
                       unpack_plan_result, unpack_upload_record,
                       unpack_meta_update_record,
                       diagnose_preparation, format_diagnostic_report,
                       metadata_tolino_id,
                       tolino_column_active, update_tolino_ids)
    from .tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                         chromium_available, hardware_id,
                         normalize_refresh_token, sanitize_error,
                         start_token_keepalive, try_live_grab_first)
except ImportError:
    from config import (account_by_name, save_account, save_settings,
                        set_active_account, settings)
    from sync import (compare_inventory, format_error_details, iter_book_ids,
                      load_state, metadata_by_id, plan_sync, plan_metadata_updates,
                      plan_collection_updates, selected_book_ids,
                      selected_table_rows, series_map_from_metadata,
                      sort_comparison_rows, sync_summary,
                      normalize_formats, safe_format_path, cover_bytes,
                      unpack_plan_result, unpack_upload_record,
                      unpack_meta_update_record,
                      diagnose_preparation, format_diagnostic_report,
                      metadata_tolino_id,
                      tolino_column_active, update_tolino_ids)
    from tolino import (PARTNERS, TolinoAuthError, TolinoClient, browser_login,
                        chromium_available, hardware_id,
                        normalize_refresh_token, sanitize_error,
                        start_token_keepalive, try_live_grab_first)


def _plugin_version():
    """Plugin version string for display; safe outside Calibre."""
    try:
        from . import PLUGIN_VERSION
    except Exception:
        return "unbekannt"
    return ".".join(str(part) for part in PLUGIN_VERSION)


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


def _format_signatures(database, book_id, formats):
    """size:mtime je Format-Datei -- das billige Content-Signal (0.9.45).

    Ersetzt eine Datei, ändern sich Größe und mtime; eine reine
    Metadatenänderung in Calibre berührt die Datei nicht.
    """
    signatures = {}
    for fmt in formats:
        try:
            stat = os.stat(safe_format_path(database, book_id, fmt))
        except (ValueError, OSError):
            continue
        signatures[fmt] = "%d:%d" % (stat.st_size, int(stat.st_mtime * 1000))
    return signatures


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
    """Bestandsvergleich: bewusst in Alltagssprache und ohne Ballast.

    Der Normalfall steht vorn -- Titel/Autor/Status sehen, anhaken,
    hochladen. Technische Spalten (ISBN, Tolino-ID) und die Cloud-
    Einzelaktionen sind eingeklappt, und über der Tabelle steht im
    Klartext, was die Synchronisierung zusätzlich automatisch tut.
    """

    # Sichtbare Spalten zuerst; die technischen Spalten stehen ganz rechts
    # und sind standardmäßig ausgeblendet.
    HEADERS = ("Hochladen", "Titel", "Autor(en)", "Status", "Hinweis",
               "ISBN", "Tolino-ID")
    TECHNICAL_COLUMNS = (5, 6)

    STATUS_TEXT = {
        "new_in_calibre": "Nur in Calibre",
        "only_tolino": "Nur in der Cloud",
        "identical": "In beiden, gleich",
        "changed": "In beiden, geändert",
        "duplicate_tolino": "Mehrfach in der Cloud",
        "not_matchable": "Nicht zuordenbar",
    }

    _SORT_KEYS = (
        # 0 Hochladen: angehakte zuerst, danach Titel/Autoren
        lambda row: (0 if row.get("selected") else 1,
                     str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        # 1 Titel
        lambda row: (str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        # 2 Autor(en)
        lambda row: (str(row.get("authors") or "").casefold(),
                     str(row.get("title") or "").casefold()),
        # 3 Status
        lambda row: (str(row.get("status") or ""),
                     str(row.get("title") or "").casefold()),
        # 4 Hinweis
        lambda row: (str(row.get("explanation") or ""),
                     str(row.get("title") or "").casefold()),
        # 5 ISBN
        lambda row: (str(row.get("isbn") or ""),
                     str(row.get("title") or "").casefold()),
        # 6 Tolino-ID
        lambda row: (str(row.get("tolino_id") or ""),
                     str(row.get("title") or "").casefold()),
    )

    def __init__(self, rows, parent=None, client_factory=None, options=None):
        QDialog.__init__(self, parent)
        # Sortierung: erst die Upload-Auswahl, dann Titel, dann Autoren --
        # Zeilen und Häkchen bleiben identisch, nur die Reihenfolge.
        self.rows = sort_comparison_rows(list(rows))
        self.client_factory = client_factory
        self.options = dict(options or {})
        self._technical_visible = False
        self.setWindowTitle("Bestandsvergleich")
        self.setMinimumSize(980, 520)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Ihre Calibre-Bibliothek im Vergleich zur Tolino Cloud. "
            "Angehakte Bücher werden mit <b>„Auswahl hochladen“</b> in die "
            "Cloud geladen; ein Klick auf einen Spaltenkopf sortiert die "
            "Liste.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.note = QLabel(self._automatic_note())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        helpers = QHBoxLayout()
        self.select_differences = QPushButton("Alle Unterschiede anhaken")
        self.select_differences.setToolTip(
            "Hakt alle Bücher an, die neu in Calibre oder von der Cloud "
            "abweichend sind.")
        self.clear_selection = QPushButton("Auswahl aufheben")
        self.clear_selection.setToolTip("Nimmt alle Häkchen weg.")
        helpers.addWidget(self.select_differences)
        helpers.addWidget(self.clear_selection)
        helpers.addStretch(1)
        layout.addLayout(helpers)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        header_tooltips = (
            "Häkchen = mit „Auswahl hochladen“ in die Tolino Cloud laden",
            "Buchtitel", "Autor(en)",
            "Was sich zwischen Calibre und Cloud unterscheidet",
            "Erklärung dieser Zeile",
            "ISBN (technisch)",
            "Tolino-Kennung der Cloud-Kopie (technisch)",
        )
        for index, text in enumerate(header_tooltips):
            item = self.table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(text)
        # Spaltenkopf-Klick sortiert (QTableWidget-eigenes Sortieren würde
        # die Zell-Widgets/Häkchen nicht mit den Zeilen mitziehen -- alles
        # wird deshalb über _rebuild_table neu aufgebaut).
        self._sort_column = 0
        self._sort_descending = False
        table_header = self.table.horizontalHeader()
        try:
            table_header.setSortIndicatorShown(True)
            table_header.setSortIndicator(0, Qt.AscendingOrder)
        except Exception:
            pass  # Sortieranzeige ist nur Kosmetik
        table_header.sectionClicked.connect(self._sort_by_column)
        self._rebuild_table(self.rows)
        layout.addWidget(self.table)

        self.technical_toggle = QCheckBox(
            "Technische Spalten anzeigen (ISBN, Tolino-ID)")
        self.technical_toggle.setToolTip(
            "Für den normalen Gebrauch nicht nötig – nur zum Nachschauen.")
        self.technical_toggle.toggled.connect(self._toggle_technical_columns)
        layout.addWidget(self.technical_toggle)
        self._apply_technical_visibility()

        # Weitere Cloud-Aktionen liegen eingeklappt: der Normalfall
        # (anhaken, hochladen) soll nicht von Nebenschauplätzen verdeckt
        # werden. Die Aktionen nutzen die Tabelle DIESES Fensters und
        # können es deshalb nicht überleben (der gelöschte-QTableWidget-
        # Absturz).
        self.more_toggle = QCheckBox(
            "Weitere Aktionen anzeigen (herunterladen, Sammlung, gelesen)")
        self.more_toggle.setToolTip(
            "Wirkt nur auf das markierte Buch in der Tolino Cloud, nicht "
            "auf Ihre Calibre-Bibliothek.")
        self.more_toggle.toggled.connect(self._toggle_more_actions)
        layout.addWidget(self.more_toggle)

        self.actions = QGroupBox(
            "Wirkt nur in der Tolino Cloud, nicht in Calibre:")
        action_row = QHBoxLayout()
        self.download_btn = QPushButton("Herunterladen")
        self.download_btn.setToolTip(
            "Lädt das markierte Buch aus der Tolino Cloud als EPUB auf "
            "diesen Rechner.")
        self.collection_add_btn = QPushButton("Zur Sammlung")
        self.collection_add_btn.setToolTip(
            "Ordnet das markierte Cloud-Buch einer Sammlung („Ordner“ in "
            "der Tolino Cloud) zu -- der Name wird abgefragt.")
        self.collection_rm_btn = QPushButton("Aus Sammlung")
        self.collection_rm_btn.setToolTip(
            "Nimmt das markierte Cloud-Buch aus einer Sammlung der "
            "Tolino Cloud heraus -- der Name wird abgefragt.")
        self.mark_read_btn = QPushButton("Gelesen markieren")
        self.mark_read_btn.setToolTip(
            "Setzt im Tolino-Konto die Lese-Marke „gelesen“ für das "
            "markierte Cloud-Buch.")
        for button in (self.download_btn, self.collection_add_btn,
                       self.collection_rm_btn, self.mark_read_btn):
            action_row.addWidget(button)
        self.actions.setLayout(action_row)
        self.actions.setVisible(False)
        layout.addWidget(self.actions)
        self.download_btn.clicked.connect(self.download_selected)
        self.collection_add_btn.clicked.connect(self.add_selected_to_collection)
        self.collection_rm_btn.clicked.connect(self.remove_selected_from_collection)
        self.mark_read_btn.clicked.connect(self.mark_selected_read)

        buttons = QHBoxLayout()
        confirm = QPushButton("Auswahl hochladen")
        confirm.setToolTip(
            "Lädt genau die angehakten Bücher in die Tolino Cloud hoch.")
        cancel = QPushButton("Abbrechen")
        cancel.setToolTip(
            "Schließt den Vergleich, ohne etwas hochzuladen.")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.confirm = confirm
        buttons.addWidget(confirm)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.select_differences.clicked.connect(self.select_all_differences)
        self.clear_selection.clicked.connect(self.clear_all_selection)
        # Erst jetzt gibt es den Knopf: die Zusammenfassung zeigt so von
        # Anfang an die Zahl der angehakten Bücher auf dem Knopf.
        self._update_summary()

    def _automatic_note(self):
        """Klartext-Zeile: was die Synchronisierung zusätzlich tut."""
        parts = []
        if self.options.get("collections"):
            parts.append("Serien werden in der Tolino Cloud angelegt")
        if self.options.get("metadata_updates"):
            parts.append("geänderte Titel/Autoren/ISBN werden ohne neuen "
                         "Upload in der Cloud aktualisiert")
        if self.options.get("deletions"):
            parts.append("Bücher, die nicht mehr in Calibre sind, werden "
                         "aus der Cloud gelöscht")
        if not parts:
            return ("Nur die angehakten Bücher werden hochgeladen – sonst "
                    "passiert nichts.")
        return ("Zusätzlich automatisch bei „Auswahl hochladen“: %s. "
                "Abschaltbar unter „Erweiterte Einstellungen“."
                % "; ".join(parts))

    def _toggle_technical_columns(self, visible):
        self._technical_visible = bool(visible)
        self._apply_technical_visibility()

    def _apply_technical_visibility(self):
        for column in self.TECHNICAL_COLUMNS:
            try:
                self.table.setColumnHidden(column, not self._technical_visible)
            except Exception:
                pass  # Qt-Stub ohne Spaltenverstecken

    def _toggle_more_actions(self, visible):
        try:
            self.actions.setVisible(bool(visible))
        except Exception:
            pass  # Qt-Stub oder bereits zerstörtes Fenster

    def _set_selection(self, predicate):
        """Häufige Auswahlmuster in einem Schritt (Häkchen bleiben synchron)."""
        for row, check in zip(self.rows, self.checks):
            row["selected"] = bool(predicate(row))
            try:
                check.setChecked(row["selected"])
            except Exception:
                pass  # Qt-Stub ohne echtes Signal
        self._update_summary()

    def select_all_differences(self):
        """Alle Bücher anhaken, die neu sind oder abweichen."""
        differences = {"new_in_calibre", "changed"}
        self._set_selection(
            lambda row: row.get("book_id") is not None
            and row.get("status") in differences)

    def clear_all_selection(self):
        self._set_selection(lambda _row: False)

    def selected_ids(self, rows=None):
        rows = self.rows if rows is None else rows
        if len(rows) != len(self.checks):
            raise ValueError("Inventory table and selection controls have different row counts.")
        return selected_book_ids([
            dict(row, selected=check.isChecked()) for row, check in zip(rows, self.checks)
        ])

    def _sort_by_column(self, column):
        """Spaltenkopf anklicken: aufsteigend <-> absteigend umschalten."""
        if not 0 <= column < len(self._SORT_KEYS):
            return
        # Hächenzustand zuerst in die Zeilen retten, sonst geht nach dem
        # Umsortieren der GetUser-Auswahl verloren.
        for row, check in zip(self.rows, self.checks):
            row["selected"] = bool(check.isChecked())
        if self._sort_column == column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        ordered = sorted(self.rows, key=self._SORT_KEYS[column],
                         reverse=self._sort_descending)
        try:
            self.table.horizontalHeader().setSortIndicator(
                column,
                Qt.DescendingOrder if self._sort_descending
                else Qt.AscendingOrder)
        except Exception:
            pass  # Sortieranzeige ist nur Kosmetik
        self._rebuild_table(ordered)

    def _rebuild_table(self, rows):
        """Tabelle aus den (ggf. sortierten) Zeilen neu aufbauen."""
        self.rows = list(rows)
        self.checks = []
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            check = QCheckBox()
            check.setChecked(bool(row.get("selected")))
            check.setEnabled(row.get("book_id") is not None)
            check.setToolTip(
                "Buch aus der Calibre-Bibliothek in die Cloud laden."
                if row.get("book_id") is not None
                else "Nur in der Cloud – nicht hochladbar, aber "
                     "herunterladbar.")
            state_signal = getattr(check, "stateChanged", None)
            if state_signal is not None:
                try:
                    state_signal.connect(self._update_summary)
                except Exception:
                    pass  # Qt-Stub ohne echtes Signal
            self.checks.append(check)
            self.table.setCellWidget(row_index, 0, check)
            values = (
                row.get("title", ""),
                row.get("authors", ""),
                self.STATUS_TEXT.get(row["status"], row["status"]),
                row.get("explanation", ""),
                row.get("isbn", ""),
                row.get("tolino_id", ""),
            )
            for column, value in enumerate(values, 1):
                self.table.setItem(
                    row_index, column, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()
        self._apply_technical_visibility()
        self._update_summary()

    def _update_summary(self, *_args):
        """Kurzzeile: wie viele Bücher gerade ausgewählt sind."""
        try:
            total = len(self.rows)
            selected = len(self.selected_ids())
            summary = getattr(self, "summary", None)
            if summary is not None:
                summary.setText(
                    "<b>%d von %d Büchern angehakt</b> – ohne Häkchen wird "
                    "nichts hochgeladen." % (selected, total))
            confirm = getattr(self, "confirm", None)
            if confirm is not None:
                confirm.setText("Auswahl hochladen (%d)" % selected
                                if selected else "Auswahl hochladen")
        except Exception:
            return  # Qt-Stub oder noch nicht aufgebaute Oberfläche

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
            # Derselbe Weg wie „Synchronisieren starten“: Konto speichern,
            # Token- und Hardware-Callback, Login -- der Test darf sich
            # nicht vom Lauf unterscheiden, der ihn scheinbar heilt.
            client = self.dashboard._cloud_client()
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

    def __init__(self, settings, jobs, removals, meta_updates=None, series_map=None):
        QObject.__init__(self)
        self.settings = settings
        self.jobs = jobs
        self.removals = removals
        self.meta_updates = list(meta_updates or ())
        self.series_map = dict(series_map or {})
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
            total = (len(self.jobs) + len(self.removals)
                     + len(self.meta_updates) + len(self.series_map))
            done = 0
            # Erst die in-place Metadaten-Updates, dann die Uploads (0.9.45).
            for update in self.meta_updates:
                if self.cancelled:
                    raise RuntimeError("Synchronisierung vom Benutzer abgebrochen.")
                record = unpack_meta_update_record(update)
                self.progress.emit(done, total, "Metadaten: %s" % record["book_uuid"])
                client.update_metadata(record["tolino_id"], record["fields"])
                done += 1
            for job in self.jobs:
                if self.cancelled:
                    raise RuntimeError("Synchronisierung vom Benutzer abgebrochen.")
                self.progress.emit(done, total, "Lade hoch: %s (%s)" %
                                   (job.book_uuid, job.format_name))
                new_id = client.upload(job.path)
                # Plan-Eintrag kopieren: fingerprint/content_fp/meta_fp bleiben
                # erhalten, nur die Tolino-ID wird neu (0.9.45).
                entry = dict(state.get(job.book_uuid) or {})
                entry.update({"tolino_id": new_id, "calibre_id": job.book_id})
                state[job.book_uuid] = entry
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
            # Serien → Cloud-Sammlungen: Bestand einmal lesen und nur fehlende
            # Zuordnungen nachziehen (idempotent). Ohne eingeschaltete
            # Sammlungen ist series_map leer und hier passiert nichts.
            series_items = plan_collection_updates(self.series_map, state)
            if series_items:
                known = client.collection_pairs()
                for tolino_id, series in series_items:
                    if self.cancelled:
                        raise RuntimeError("Synchronisierung vom Benutzer abgebrochen.")
                    if (tolino_id, series) in known:
                        done += 1
                        continue
                    self.progress.emit(done, total,
                                       "Serie in der Cloud: %s" % series)
                    client.add_to_collection(tolino_id, series)
                    known.add((tolino_id, series))
                    done += 1
                self.progress.emit(done, total, "Serien geprüft")
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
        self.setWindowTitle("Tolino Cloud Sync (Plugin-Version %s)"
                            % _plugin_version())
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)

        intro = QLabel(
            "So geht’s: <b>1)</b> anmelden &nbsp;→&nbsp; <b>2)</b> "
            "vergleichen und Bücher hochladen. Alles Weitere ist optional "
            "und standardmäßig richtig eingestellt.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # --- Schritt 1: Anmelden -------------------------------------------
        setup = QGroupBox("1 · Anmelden")
        setup_form = QFormLayout(setup)
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem("%s – %s" % (pid, partner["name"]), pid)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(QLineEdit.Password)
        self.status = QLabel()
        self.browser = QPushButton("Jetzt bei Tolino anmelden")
        self.browser.setToolTip(
            "Öffnet ein Anmeldefenster. Dort im Web Reader einloggen und "
            "die Bücherliste öffnen – den Zugang holt das Plugin "
            "automatisch und schließt das Fenster wieder.")
        self.browser.clicked.connect(self.browser_login)
        self.scrape_browser = QPushButton("Zugang aus offenem Browser holen")
        self.scrape_browser.setToolTip(
            "Holt den Zugang aus einem bereits geöffneten oder neu "
            "geöffneten Anmeldefenster (benötigt Chrome, Chromium, Brave "
            "oder Edge).")
        self.scrape_browser.clicked.connect(self.scrape_browser_tokens)
        setup_form.addRow("Buchhändler", self.partner)
        setup_form.addRow("", self.browser)
        setup_form.addRow("", self.status)
        root.addWidget(setup)

        # --- Schritt 2: Synchronisieren ------------------------------------
        action = QGroupBox("2 · Synchronisieren")
        action_form = QFormLayout(action)
        self.progress_label = QLabel("Bereit")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.start = QPushButton("Vergleichen und hochladen …")
        self.start.setToolTip(
            "Öffnet den Bestandsvergleich: Bücher anhaken, dann die "
            "Auswahl in die Tolino Cloud hochladen. Die automatischen "
            "Cloud-Aktionen aus den erweiterten Einstellungen laufen mit.")
        self.start.clicked.connect(self.start_sync)
        self.cancel = QPushButton("Abbrechen")
        self.cancel.setEnabled(False)
        self.cancel.clicked.connect(self.cancel_sync)
        self.debug = QPushButton("Diagnose")
        self.debug.setToolTip(
            "Prüfbericht zum Nachschauen bei Problemen. Führt ohne "
            "Anmeldung keine Tolino-Anfrage aus.")
        self.debug.clicked.connect(self.debug_diagnose)
        action_form.addRow("", self.start)
        action_form.addRow("", self.progress_label)
        action_form.addRow("", self.progress)
        action_form.addRow("", self.cancel)
        root.addWidget(action)

        # --- Erweiterte Einstellungen (standardmäßig ausgeblendet) ---------
        self.advanced_toggle = QCheckBox("Erweiterte Einstellungen anzeigen")
        self.advanced_toggle.setToolTip(
            "Zugangsdaten, Formate, Vergleichsfelder, automatische "
            "Cloud-Aktionen und mehrere Konten – für den normalen "
            "Gebrauch nicht nötig.")
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        root.addWidget(self.advanced_toggle)

        self.advanced = QGroupBox("Erweiterte Einstellungen")
        advanced_form = QFormLayout(self.advanced)
        self.formats = QLineEdit()
        self.covers = QCheckBox("Titelbild mit hochladen")
        self.covers.setToolTip(
            "Das Titelbild des Buches (Calibre: „Titelbild“) zusätzlich in "
            "die Cloud laden.")
        self.deletions = QCheckBox("Löschungen aus der Cloud übernehmen")
        self.sync_collections = QCheckBox("Serien in der Cloud anlegen")
        self.sync_collections.setToolTip(
            "Jedes Buch einer Calibre-Serie wird in der Tolino Cloud "
            "zugeordnet; der Tolino Web Reader zeigt diese Zuordnung als "
            "„Sammlung“ an. Abschalten, wenn die Cloud unverändert "
            "bleiben soll.")
        self.metadata_updates = QCheckBox(
            "Geänderte Titel/Autoren/ISBN in der Cloud aktualisieren")
        self.metadata_updates.setToolTip(
            "Ändert sich in Calibre nur der Text (die Buchdatei bleibt "
            "gleich), wird der Cloud-Eintrag ohne neuen Upload "
            "aktualisiert. Abschalten, wenn die Cloud unverändert bleiben "
            "soll.")
        self.tolino_column = QCheckBox(
            "Tolino-ID in eigene Spalte schreiben (nur ein Konto)")
        self.tolino_column.setToolTip(
            "Schreibt die Tolino-IDs in die Calibre-Spalte #tolino_id. "
            "Nur sinnvoll bei genau einem Konto – bei mehreren Konten "
            "merkt sich jedes Konto seine IDs selbst.")
        self.compare_authors = QCheckBox(
            "Autor(en) beim Vergleich berücksichtigen")
        self.compare_title = QCheckBox("Titel beim Vergleich berücksichtigen")
        self.compare_isbn = QCheckBox("ISBN beim Vergleich berücksichtigen")
        for field in (self.compare_authors, self.compare_title, self.compare_isbn):
            field.setChecked(True)
        advanced_form.addRow("", self.scrape_browser)
        advanced_form.addRow("Hardware-ID (automatisch erkannt)", self.hardware)
        advanced_form.addRow("Zugangsschlüssel (Refresh-Token)", self.refresh)
        advanced_form.addRow("Bevorzugte Formate", self.formats)
        advanced_form.addRow("", self.covers)
        advanced_form.addRow("", self.compare_authors)
        advanced_form.addRow("", self.compare_title)
        advanced_form.addRow("", self.compare_isbn)
        automatic_hint = QLabel("Bei jedem Synchronisieren zusätzlich "
                                "(abwählbar):")
        automatic_hint.setWordWrap(True)
        advanced_form.addRow("", automatic_hint)
        advanced_form.addRow("", self.deletions)
        advanced_form.addRow("", self.sync_collections)
        advanced_form.addRow("", self.metadata_updates)
        advanced_form.addRow("", self.tolino_column)
        root.addWidget(self.advanced)

        self.accounts_group = QGroupBox("Konten")
        account_form = QFormLayout(self.accounts_group)
        self.account_select = QComboBox()
        self.account_new = QPushButton("Neues Konto")
        self.account_remove = QPushButton("Konto löschen")
        account_buttons = QHBoxLayout()
        account_buttons.addWidget(self.account_new)
        account_buttons.addWidget(self.account_remove)
        self.account_select.currentIndexChanged.connect(self.account_changed)
        self.account_new.clicked.connect(self.new_account)
        self.account_remove.clicked.connect(self.remove_account)
        account_form.addRow("Aktives Konto", self.account_select)
        account_form.addRow("", account_buttons)
        root.addWidget(self.accounts_group)

        self.advanced.setVisible(False)
        self.accounts_group.setVisible(False)

        # Plain close button instead of QDialogButtonBox: immune to the Qt6
        # "Invalid ButtonRole, button not added" warning seen in the field.
        close = QPushButton("Schließen")
        close.clicked.connect(self.close)
        root.addWidget(close)
        self.load_values()

    def _toggle_advanced(self, visible):
        """Erweiterte Einstellungen ein-/ausblenden (Standard: aus)."""
        visible = bool(visible)
        self.advanced.setVisible(visible)
        self.accounts_group.setVisible(visible)

    def _save_visible_account(self):
        if not getattr(self, "account_name", None):
            return
        save_settings({
            "preferred_formats": [x.strip().upper()
                                  for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "sync_collections": self.sync_collections.isChecked(),
            "sync_metadata_updates": self.metadata_updates.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
        })
        save_account(self.account_name, self.values(), active=self.account_name)

    def account_changed(self, index):
        if index < 0 or not getattr(self, "account_names", None):
            return
        self._save_visible_account()
        self.account_name = self.account_select.itemData(index)
        # Angezeigtes und aktives Konto sind ab jetzt IMMER dasselbe:
        # settings(), Konten-State und Keep-alive lesen sonst weiter das
        # vorherige Konto, und die Tolino-ID-Zuordnung landete beim
        # falschen Konto.
        set_active_account(self.account_name)
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
        self.sync_collections.setChecked(values["sync_collections"])
        self.metadata_updates.setChecked(values["sync_metadata_updates"])
        self.tolino_column.setChecked(values["use_tolino_column"])
        self.update_status()

    def update_status(self):
        logged_in = bool(self.refresh.text().strip())
        self.status.setText(
            "✓ Angemeldet – Sie können synchronisieren." if logged_in
            else "● Nicht angemeldet – bitte zuerst oben anmelden.")
        # Schritt 2 ist ohne Anmeldung nicht möglich; während eines Laufs
        # bleibt der Knopf gesperrt (start_sync/finish_thread steuern ihn).
        if self.thread is None:
            self.start.setEnabled(logged_in)

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

    def scrape_browser_tokens(self):
        """Read the current token from the plugin's own sign-in window."""
        try:
            # Ein laufendes Anmeldefenster haelt den AKTUELLEN Token.
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
            # Kein Fenster: selbst das eigene Chromium-Fenster oeffnen und
            # den Token live uebernehmen.
            if chromium_available():
                if (self.login_thread is not None
                        and self.login_thread.isRunning()):
                    return  # eine Anmeldung läuft bereits
                self.status.setText(
                    "Öffne ein eigenes Chromium-Fenster auf dem Web "
                    "Reader – dort anmelden, Bücherliste laden. Nur "
                    "dieses Fenster schließt sich danach.")
                self.browser_login()
                return
            QMessageBox.information(
                self, "Kein Chromium-Browser gefunden",
                "Dieser Knopf liest den Zugang aus dem eigenen "
                "Anmeldefenster und benötigt dafür Chrome, Chromium, "
                "Brave oder Edge.\n\n"
                "Entweder einen dieser Browser installieren und danach "
                "„Jetzt bei Tolino anmelden“ erneut drücken.\n\n"
                "Oder den Zugang von Hand eintragen: im Web Reader "
                "anmelden, F12 → Netzwerk → Aufnahme starten, im "
                "Netzwerk-Protokoll refresh_token und hardware_id "
                "kopieren und unter „Erweiterte Einstellungen“ in die "
                "Felder „Refresh-Token“ und „Hardware-ID“ einfügen.")
        except TolinoAuthError as exc:
            QMessageBox.warning(
                self, "Live-\u00dcbernahme",
                "%s\n\nDas Anmeldefenster ist noch offen: dort im Web "
                "Reader anmelden (Bücherliste laden) und den Knopf "
                "erneut drücken." % sanitize_error(exc))
        except Exception as exc:
            QMessageBox.critical(
                self, "Fehler",
                "Fehler beim Extrahieren der Tokens: %s" % sanitize_error(exc)
            )

    def _selected_account(self):
        """The account this dialog shows -- never a different 'active' one."""
        return account_by_name(getattr(self, "account_name", None))

    def values(self):
        refresh_token, _ = normalize_refresh_token(self.refresh.text())
        account = self._selected_account() or settings()
        return {
            "account_name": self.account_name,
            "partner_id": self.partner.currentData(),
            "hardware_id": self.hardware.text().strip() or hardware_id(),
            "refresh_token": refresh_token,
            "username": account.get("username", ""),
            "password": account.get("password", ""),
            "preferred_formats": [x.strip().upper()
                                  for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "sync_collections": self.sync_collections.isChecked(),
            "sync_metadata_updates": self.metadata_updates.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
            "state": (account.get("state")
                      if isinstance(account.get("state"), dict) else {}),
        }

    def browser_login(self):
        """Open the private Chromium sign-in window, adopt the fresh token."""
        if self.login_thread is not None and self.login_thread.isRunning():
            return  # a sign-in attempt is already running
        partner_id = self.partner.currentData()
        self.browser.setEnabled(False)
        self.scrape_browser.setEnabled(False)
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
            self.scrape_browser.setEnabled(True)
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
                "Bitte zuerst anmelden: „Jetzt bei Tolino anmelden“.")
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
            QMessageBox.warning(self, "Noch nicht angemeldet",
                                "Bitte zuerst oben auf „Jetzt bei Tolino "
                                "anmelden“ drücken.")
            return
        # EINE Regel fuer Lesen UND Schreiben der Tolino-ID-Spalte
        # (tolino_column_active): nur bei genau einem Konto.
        self.sync_column_enabled = tolino_column_active(
            settings, self.gui.current_db)
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
                    "series": str(_metadata_value(item, "series") or "").strip(),
                }
            for book_id, item in metadata.items():
                item["formats"] = normalize_formats(item["formats"])
                item["format_signatures"] = _format_signatures(
                    self.gui.current_db, book_id, item["formats"])
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
                use_metadata_ids=self.sync_column_enabled,
            )
            dialog = InventoryDialog(
                comparison, self,
                client_factory=lambda: self._cloud_client(),
                options={
                    "collections": bool(settings["sync_collections"]),
                    "metadata_updates": bool(settings["sync_metadata_updates"]),
                    "deletions": bool(settings["enable_deletions"]),
                })
            if dialog.exec() != QDialog.Accepted:
                return
            # The checkbox state of the confirmed dialog decides what is
            # uploaded; an empty selection simply syncs nothing.
            selected = dialog.selected_ids()
            uploads, removals, current = unpack_plan_result(plan_sync(
                metadata, state, settings["preferred_formats"],
                settings["enable_deletions"], upload_book_ids=selected,
            ))
            # Reine Metadatenänderungen laufen in-place gegen /meta und
            # sind abschaltbar (0.12.0).
            uploads, meta_updates = plan_metadata_updates(
                metadata, state, uploads, settings["preferred_formats"],
                enabled=settings["sync_metadata_updates"])
            # Serien → Sammlungen: ohne eingeschaltete Sammlungen bleibt
            # die Zuordnung leer und die Cloud wird nicht angefasst.
            series_map = (series_map_from_metadata(metadata)
                          if settings["sync_collections"] else {})
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
        self.worker = SyncWorker(settings, jobs, removals, meta_updates, series_map)
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
            # Keep-alive-Konto = aktives Konto (settings()); das
            # Dialog-Feld "account_name" existiert in settings() nicht --
            # der rotierte Token landete dadurch nie auf der Platte.
            name = cfg.get("active_account") or cfg.get("name")
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

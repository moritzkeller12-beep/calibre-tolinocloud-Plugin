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

from .config import (account_by_name, save_account, save_credentials,
                     save_settings, set_active_account, settings)
from .sync import (compare_inventory, iter_book_ids,
                   load_state, metadata_by_id, plan_sync, plan_metadata_updates,
                   plan_collection_updates, revert_unsynced_fingerprints,
                   remember_collection,
                   selected_book_ids,
                   selected_table_rows, series_map_from_metadata,
                   sort_comparison_rows, sync_summary,
                   normalize_formats, safe_format_path, cover_bytes,
                   unpack_plan_result, unpack_upload_record,
                   unpack_meta_update_record,
                   diagnose_preparation, format_diagnostic_report,
                   metadata_tolino_id,
                   tolino_column_active, update_tolino_ids)
from .tolino import (PARTNERS, RELOGIN, TolinoAuthError, TolinoClient, browser_login,
                     check_stored_login, chromium_available, hardware_id,
                     log_warning,
                     mark_sync_active,
                     normalize_refresh_token, recent_errors, report_error,
                     sanitize_error,
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
    """size:mtime je Format-Datei – das billige Content-Signal (0.9.45).

    Ersetzt eine Datei, ändern sich Grösse und mtime; eine reine
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


CANCEL_MESSAGE = "Die Synchronisierung wurde abgebrochen."


class InventoryDialog(QDialog):
    """Bestandsvergleich: pro Buch drei Aktionen zum Auswählen.

    Links steht, was mit dem Buch passieren soll – **Hochladen** (die
    Buchdatei in die Cloud), **Metadaten** (nur Titel/Autor(en)/ISBN in der
    Cloud abgleichen, ohne Upload) und **Gelesen** (die Lese-Marke im
    Tolino-Konto setzen oder wieder wegnehmen). **In Calibre** und **In der
    Cloud** sagen mit einem Häkchen, wo das Buch liegt, **Hinweis** erklärt
    die Zeile. Alles Technische ist eingeklappt, und alle Aktionen für
    markierte Zeilen liegen hinter EINEM Knopf.
    """

    HEADERS = ("Hochladen", "Metadaten", "Gelesen", "In Calibre", "In der Cloud",
               "Titel", "Autor(en)", "Hinweis", "ISBN", "Tolino-ID")
    # Technische Spalten stehen ganz rechts und sind standardmässig aus.
    TECHNICAL_COLUMNS = (8, 9)

    # Text der entsperrten Lese-Zelle -- einmal, damit er nicht driftet.
    READ_UNLOCKED_TOOLTIP = (
        "In der Cloud gelesen. Erneut anklicken setzt es wieder auf "
        "ungelesen.")

    # Alle Aktionen, die auf die MARKIERTEN Tabellenzeilen wirken -- eine
    # Stelle statt vier Knöpfe in zwei verschiedenen Ecken (0.15.2). Die
    # Spalten Hochladen/Metadaten/Gelesen lassen sich hier genauso setzen
    # und zurücknehmen wie einzeln im Vergleich (0.15.11).
    ROW_ACTIONS = (
        ("Zum Hochladen auswählen", "upload_on"),
        ("Hochladen abwählen", "upload_off"),
        ("Für Metadaten auswählen", "metadata_on"),
        ("Metadaten abwählen", "metadata_off"),
        ("Als gelesen markieren", "read"),
        ("Als ungelesen markieren", "read_off"),
        ("Aus der Cloud herunterladen", "download"),
        ("Zu Sammlung hinzufügen …", "collection_add"),
        ("Aus Sammlung entfernen …", "collection_remove"),
    )

    _SORT_KEYS = (
        # 0 Hochladen / 1 Metadaten: Ausgewähltes zuerst, dann Titel/Autor(en)
        lambda row: (0 if row.get("upload") else 1,
                     0 if row.get("metadata") else 1,
                     str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        lambda row: (0 if row.get("metadata") else 1,
                     0 if row.get("upload") else 1,
                     str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        # 2 Gelesen: markierte Bücher zuerst
        lambda row: (0 if row.get("read") else 1,
                     str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        # 3 In Calibre: vorhandene Bücher zuerst
        lambda row: (0 if row.get("book_id") is not None else 1,
                     str(row.get("title") or "").casefold()),
        # 4 In der Cloud: Cloud-Kopien zuerst
        lambda row: (0 if row.get("tolino_id") else 1,
                     str(row.get("title") or "").casefold()),
        # 5 Titel / 6 Autor(en) (mit dem jeweils anderen als Zweitschlüssel)
        lambda row: (str(row.get("title") or "").casefold(),
                     str(row.get("authors") or "").casefold()),
        lambda row: (str(row.get("authors") or "").casefold(),
                     str(row.get("title") or "").casefold()),
        # 7 Hinweis
        lambda row: (str(row.get("explanation") or ""),
                     str(row.get("title") or "").casefold()),
        # 8 ISBN / 9 Tolino-ID
        lambda row: (str(row.get("isbn") or ""),
                     str(row.get("title") or "").casefold()),
        lambda row: (str(row.get("tolino_id") or ""),
                     str(row.get("title") or "").casefold()),
    )

    @staticmethod
    def default_actions(row):
        """Was der Vergleich für diese Zeile vorschlägt (Hochladen, Metadaten).

        Neue Bücher werden hochgeladen. Bei geänderten Büchern entscheidet
        der Unterschied: neue Buchdatei -> Hochladen, sonst reicht der
        Metadaten-Abgleich. Ohne Cloud-Kopie gibt es nichts abzugleichen.
        Die Lese-Marke wird nie vorgeschlagen: der Lese-Status in der Cloud
        bleibt, wie er ist, bis der Nutzer ihn ausdrücklich wählt.
        """
        if row.get("book_id") is None or not row.get("tolino_id"):
            new_book = row.get("book_id") is not None
            return (new_book, False)
        if row.get("status") == "new_in_calibre":
            return (True, False)
        if row.get("status") == "changed":
            return (False, True) if not row.get("file_changed") else (True, False)
        return (False, False)

    def __init__(self, rows, parent=None, client_factory=None, options=None):
        QDialog.__init__(self, parent)
        self.client_factory = client_factory
        self.options = dict(options or {})
        self._technical_visible = False
        self.rows = []
        for row in rows:
            row = dict(row)
            if "upload" not in row or "metadata" not in row:
                row["upload"], row["metadata"] = self.default_actions(row)
            # „Gelesen“ kommt aus der Cloud: was dort schon gelesen ist, ist
            # angehakt (und gesperrt) -- nichts muss doppelt geschrieben
            # werden (0.15.0).
            row.setdefault("read", bool(row.get("read_in_cloud")))
            self.rows.append(row)
        # Ausgewählte Aktionen zuerst, dann Titel/Autor(en).
        self.rows = sort_comparison_rows(self.rows)
        self.setWindowTitle("Bestandsvergleich")
        self.setMinimumSize(1180, 540)
        layout = QVBoxLayout(self)

        intro = QLabel(
            "Pro Buch auswählen, was in die Cloud soll: <b>Hochladen</b> "
            "(Buchdatei), <b>Metadaten</b> (Titel, Autor, ISBN) oder "
            "<b>Gelesen</b>. Ohne Auswahl passiert nichts.")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.note = QLabel(self._automatic_note())
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        helpers = QHBoxLayout()
        self.recommend = QPushButton("Empfehlung auswählen")
        self.recommend.setToolTip(
            "Wählt für neue Bücher Hochladen, für geänderte je nach "
            "Unterschied Hochladen oder Metadaten aus.")
        self.clear_selection = QPushButton("Auswahl aufheben")
        self.clear_selection.setToolTip("Setzt alle Auswahlen zurück.")
        helpers.addWidget(self.recommend)
        helpers.addWidget(self.clear_selection)
        helpers.addStretch(1)
        self.row_actions = QPushButton("Für markierte Zeilen …")
        self.row_actions.setToolTip(
            "Für die markierten Zeilen (mehrere: Strg-/Shift-Klick, alle: "
            "Strg+A): Hochladen, Metadaten oder «Gelesen» auswählen bzw. "
            "zurücknehmen, herunterladen, Sammlung.")
        helpers.addWidget(self.row_actions)
        layout.addLayout(helpers)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        header_tooltips = (
            "Ausgewählt: Buchdatei in die Cloud hochladen",
            "Ausgewählt: Titel, Autor und ISBN aktualisieren",
            "Ausgewählt: als gelesen oder ungelesen markieren",
            "Buch ist in Calibre",
            "Buch ist in der Cloud",
            "Buchtitel", "Autor(en)",
            "Was sich unterscheidet",
            "ISBN (technisch)",
            "Tolino-ID der Cloud-Kopie (technisch)",
        )
        for index, text in enumerate(header_tooltips):
            item = self.table.horizontalHeaderItem(index)
            if item is not None:
                item.setToolTip(text)
        # Spaltenkopf-Klick sortiert (QTableWidget-eigenes Sortieren würde
        # die Zell-Widgets/Auswahl nicht mit den Zeilen mitziehen -- alles
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
            "Nur zum Nachschauen.")
        self.technical_toggle.toggled.connect(self._toggle_technical_columns)
        layout.addWidget(self.technical_toggle)
        self._apply_technical_visibility()

        buttons = QHBoxLayout()
        confirm = QPushButton("Auswahl übernehmen")
        confirm.setToolTip(
            "Führt die ausgewählten Aktionen aus.")
        cancel = QPushButton("Abbrechen")
        cancel.setToolTip(
            "Schliesst den Vergleich, ohne etwas zu ändern.")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.confirm = confirm
        buttons.addWidget(confirm)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        layout.addLayout(buttons)
        self.recommend.clicked.connect(self.apply_recommended)
        self.clear_selection.clicked.connect(self.clear_all_selection)
        self.row_actions.clicked.connect(self.choose_row_action)
        # Erst jetzt gibt es den Knopf: die Zusammenfassung zeigt so von
        # Anfang an die Zahl der ausgewählten Aktionen darauf.
        self._update_summary()

    def _automatic_note(self):
        """Klartext-Zeile: was über die Auswahl hinaus passiert."""
        parts = []
        if self.options.get("collections"):
            parts.append("Serien werden in der Cloud angelegt")
        if self.options.get("deletions"):
            parts.append("Bücher, die nicht mehr in Calibre sind, werden "
                         "aus der Cloud gelöscht")
        if parts:
            head = ("Zusätzlich automatisch: %s. Abschaltbar unter "
                    "«Einstellungen …»." % "; ".join(parts))
        else:
            head = "Geändert wird nur die Auswahl."
        return (head + " Den Gelesen-Status ändert nur die Spalte "
                "«Gelesen».")

    def _toggle_technical_columns(self, visible):
        self._technical_visible = bool(visible)
        self._apply_technical_visibility()

    def _apply_technical_visibility(self):
        for column in self.TECHNICAL_COLUMNS:
            try:
                self.table.setColumnHidden(column, not self._technical_visible)
            except Exception:
                pass  # Qt-Stub ohne Spaltenverstecken

    def _wire_read_check(self, index, check):
        """«Gelesen» verkabeln: der erste Klick löst nur die Sperre (0.15.2).

        Feldbefund: «Ein Buch, das in der Cloud als gelesen markiert ist,
        kann ich nicht mehr auf ungelesen setzen.» Deshalb ist die Zelle
        jetzt anklickbar – aber in zwei Schritten, damit ein versehentlicher
        Klick keine Lese-Marke in der Cloud löscht.
        """
        clicked = getattr(check, "clicked", None)
        if clicked is not None:
            try:
                clicked.connect(
                    lambda _checked=False, i=index: self._read_clicked(i))
            except Exception:
                pass  # Qt-Stub ohne echtes Signal
        row = self.rows[index]
        if not row.get("read_in_cloud") or not row.get("tolino_id"):
            return
        if row.get("read_unlocked"):
            check.setToolTip(self.READ_UNLOCKED_TOOLTIP)
        else:
            check.setToolTip(
                "In der Cloud gelesen. Ein Klick entsperrt, der zweite setzt es "
                "auf ungelesen.")

    def _read_clicked(self, index):
        """Klick auf eine Lese-Marke: erst entsperren, dann umschalten."""
        if not 0 <= index < len(self.rows):
            return
        row = self.rows[index]
        check = self.read_checks[index]
        if row.get("read_unlocked"):
            row["read"] = bool(check.isChecked())
            self._update_summary()
            return
        # Erster Klick: Qt hat das Häkchen schon weggenommen -- zurückgeben
        # und nur die Sperre fallen lassen.
        row["read_unlocked"] = True
        try:
            check.setChecked(True)
        except Exception:
            pass
        check.setToolTip(self.READ_UNLOCKED_TOOLTIP)
        self._update_summary()

    def _sync_checks_to_rows(self):
        for row, uploads, metadata, read in zip(self.rows, self.checks,
                                                self.meta_checks,
                                                self.read_checks):
            row["upload"] = bool(uploads.isChecked())
            row["metadata"] = bool(metadata.isChecked())
            row["read"] = bool(read.isChecked())

    def _set_selection(self, chooser):
        """Auswahl in einem Schritt setzen (Zeilen und Widgets synchron).

        `chooser` liefert (Hochladen, Metadaten). Die Lese-Marke ist nie Teil
        der Empfehlung: sie fällt auf den Cloud-Stand zurück – angehakt
        bleibt, was im Konto schon gelesen ist.
        """
        for row, uploads, metadata, read in zip(self.rows, self.checks,
                                                self.meta_checks,
                                                self.read_checks):
            wanted = tuple(chooser(row))
            upload, meta = bool(wanted[0]), bool(wanted[1])
            mark_read = bool(row.get("read_in_cloud"))
            row["upload"], row["metadata"], row["read"] = upload, meta, mark_read
            for check, value in ((uploads, upload), (metadata, meta),
                                 (read, mark_read)):
                try:
                    check.setChecked(value)
                except Exception:
                    pass  # Qt-Stub ohne echtes Signal
        self._update_summary()

    def apply_recommended(self):
        """Die vorgeschlagenen Aktionen für alle Zeilen auswählen."""
        self._set_selection(self.default_actions)

    def clear_all_selection(self):
        self._set_selection(lambda _row: (False, False))

    def selected_upload_ids(self):
        """Bücher, deren Datei hochgeladen werden soll."""
        return selected_book_ids([
            dict(row, selected=check.isChecked())
            for row, check in zip(self.rows, self.checks)
        ])

    def selected_metadata_ids(self):
        """Bücher, deren Cloud-Metadaten aktualisiert werden sollen."""
        return selected_book_ids([
            dict(row, selected=check.isChecked())
            for row, check in zip(self.rows, self.meta_checks)
        ])

    def selected_read_targets(self):
        """Tolino-IDs, die JETZT als gelesen markiert werden (0.15.0).

        Schon gelesene Bücher fallen heraus – die Cloud bekommt nur, was
        sich wirklich ändert.
        """
        return [str(row["tolino_id"]) for row, check
                in zip(self.rows, self.read_checks)
                if check.isChecked() and row.get("tolino_id")
                and not row.get("read_in_cloud")]

    def selected_read_unmarks(self):
        """Tolino-IDs, die JETZT wieder auf ungelesen gesetzt werden (0.15.2).

        Nur Bücher, die in der Cloud als gelesen stehen und deren Häkchen
        der Nutzer entfernt hat – alles andere lässt den Lese-Status in Ruhe.
        """
        return [str(row["tolino_id"]) for row, check
                in zip(self.rows, self.read_checks)
                if not check.isChecked() and row.get("tolino_id")
                and row.get("read_in_cloud")]

    def choose_row_action(self):
        """Eine Stelle für alle Aktionen auf markierte Zeilen (0.15.2).

        Vorher lagen dieselben Aktionen auf vier Knöpfen in zwei
        verschiedenen Ecken – jetzt ein Knopf, eine Rückfrage. Seit 0.15.11
        sind auch die Spalten Hochladen, Metadaten und «Gelesen» dabei.
        """
        if not selected_table_rows(self.table):
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Bitte zuerst Zeilen markieren (Strg-/Shift-Klick, alle: "
                "Strg+A).")
            return
        labels = [label for label, _key in self.ROW_ACTIONS]
        label, accepted = QInputDialog.getItem(
            self, "Für markierte Zeilen", "Aktion:", labels, 0, False)
        if not accepted or not str(label):
            return
        key = dict(self.ROW_ACTIONS).get(str(label))
        if key == "read":
            self.mark_read_for_marked_rows()
        elif key == "read_off":
            self.clear_read_for_marked_rows()
        elif key == "upload_on":
            self.set_column_for_marked_rows(
                "upload", True, "haben schon diese Auswahl oder keine Datei")
        elif key == "upload_off":
            self.set_column_for_marked_rows(
                "upload", False, "waren schon abgewählt oder haben keine Datei")
        elif key == "metadata_on":
            self.set_column_for_marked_rows(
                "metadata", True,
                "haben schon diese Auswahl oder liegen nicht in der Cloud")
        elif key == "metadata_off":
            self.set_column_for_marked_rows(
                "metadata", False,
                "waren schon abgewählt oder liegen nicht in der Cloud")
        elif key == "download":
            self.download_selected()
        elif key == "collection_add":
            self.add_selected_to_collection()
        elif key == "collection_remove":
            self.remove_selected_from_collection()

    def mark_read_for_marked_rows(self):
        """Spalte «Gelesen» für alle markierten Zeilen setzen (0.15.0).

        Feldbefund 0.15.0: Hunderte Bücher einzeln auswählen ist keine
        Zumutung – deshalb Mehrfachauswahl in der Tabelle plus diese Aktion.
        """
        self.set_column_for_marked_rows(
            "read", True, "sind schon gelesen oder liegen nicht in der Cloud")

    def clear_read_for_marked_rows(self):
        """«Gelesen» für markierte Cloud-Bücher wieder wegnehmen (0.15.11)."""
        self.set_column_for_marked_rows(
            "read", False,
            "lassen sich nicht auf ungelesen setzen (nur Gelesenes)")

    def set_column_for_marked_rows(self, key, value, summary):
        """Eine Spalte für alle markierten Zeilen setzen (0.15.11).

        Dieselbe Wirkung wie die Auswahl je Zeile, nur für viele auf einmal:
        passt die Spalte nicht zur Zeile, wird sie gezählt und ausgelassen.
        Geschrieben wird weiterhin erst mit «Auswahl übernehmen».
        """
        marked = selected_table_rows(self.table)
        if not marked:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Bitte zuerst Zeilen markieren (Strg-/Shift-Klick, alle: "
                "Strg+A).")
            return
        changed = 0
        skipped = 0
        for index in marked:
            if not 0 <= index < len(self.rows):
                continue
            if not self._column_applies(key, self.rows[index]):
                skipped += 1
                continue
            if self._set_row_choice(index, key, bool(value)):
                changed += 1
            else:
                skipped += 1
        self._update_summary()
        if not changed:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Nichts zu tun: %d markierte Zeile(n) %s."
                % (skipped or len(marked), summary))

    @staticmethod
    def _column_applies(key, row):
        """Geht diese Spalte bei dieser Zeile? (wie beim einzelnen Klick)"""
        if key == "upload":
            return row.get("book_id") is not None
        if key == "metadata":
            return (row.get("book_id") is not None
                    and bool(row.get("tolino_id")))
        if key == "read":
            return bool(row.get("tolino_id"))
        return False

    def _set_row_choice(self, index, key, value):
        """Zeile und Auswahlkästchen setzen; False = war schon so (0.15.11).

        «Gelesen» läuft in denselben zwei Schritten wie der Klick: eine in
        der Cloud gelesene Zeile wird beim Wegnehmen zuerst entsperrt,
        sonst bliebe der Cloud-Stand stehen und nichts würde übertragen.
        """
        row = self.rows[index]
        if key == "read":
            check = self.read_checks[index]
            if value and row.get("read_in_cloud"):
                return False  # steht in der Cloud schon auf gelesen
            if not value and not row.get("read_in_cloud"):
                return False  # nur Gelesenes lässt sich zurücksetzen
            if bool(row.get("read")) == value and bool(check.isChecked()) == value:
                return False
            row["read"] = value
            if not value:
                row["read_unlocked"] = True
            if row.get("read_unlocked"):
                check.setToolTip(self.READ_UNLOCKED_TOOLTIP)
        else:
            check = (self.checks if key == "upload" else self.meta_checks)[index]
            if bool(row.get(key)) == value and bool(check.isChecked()) == value:
                return False
            row[key] = value
        try:
            check.setChecked(value)
        except Exception:
            pass  # Qt-Stub ohne echtes Signal
        return True

    def _sort_by_column(self, column):
        """Spaltenkopf anklicken: aufsteigend <-> absteigend umschalten."""
        if not 0 <= column < len(self._SORT_KEYS):
            return
        # Auswahlzustand zuerst in die Zeilen retten, sonst geht nach dem
        # Umsortieren die Auswahl verloren.
        self._sync_checks_to_rows()
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
        self.meta_checks = []
        self.read_checks = []
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            has_book = row.get("book_id") is not None
            in_cloud = bool(row.get("tolino_id"))
            checks = (
                ("upload", has_book,
                 "Buchdatei in die Cloud hochladen (ersetzt dort die "
                 "vorhandene Kopie).",
                 "Nur in der Cloud – es gibt keine Calibre-Datei zum "
                 "Hochladen."),
                ("metadata", has_book and in_cloud,
                 "Titel, Autor und ISBN in der Cloud aktualisieren, ohne "
                 "Upload.",
                 "Nur in Calibre – es gibt in der Cloud nichts zu "
                 "aktualisieren." if has_book else
                 "Kein Calibre-Buch – nichts zu aktualisieren."),
                # In der Cloud liegende Bücher sind in BEIDE Richtungen
                # anklickbar: gelesen markieren und wieder ungelesen setzen.
                # Schon gelesene Bücher sind zusätzlich gesperrt -- der erste
                # Klick löst nur die Sperre (0.15.2).
                ("read", in_cloud,
                 "In der Cloud als gelesen markieren (ohne Upload).",
                 "Geht erst, wenn das Buch in der Cloud liegt."),
            )
            for column, (key, enabled, tooltip, disabled_tooltip) in enumerate(checks):
                check = QCheckBox()
                check.setChecked(bool(row.get(key)))
                check.setEnabled(enabled)
                check.setToolTip(tooltip if enabled else disabled_tooltip)
                state_signal = getattr(check, "stateChanged", None)
                if state_signal is not None:
                    try:
                        state_signal.connect(self._update_summary)
                    except Exception:
                        pass  # Qt-Stub ohne echtes Signal
                if column == 2:
                    self._wire_read_check(row_index, check)
                (self.checks, self.meta_checks,
                 self.read_checks)[column].append(check)
                self.table.setCellWidget(row_index, column, check)
            values = (
                "✓" if has_book else "",
                "✓" if in_cloud else "",
                row.get("title", ""),
                row.get("authors", ""),
                row.get("explanation", ""),
                row.get("isbn", ""),
                row.get("tolino_id", ""),
            )
            for column, value in enumerate(values, 3):
                self.table.setItem(
                    row_index, column, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()
        self._apply_technical_visibility()
        self._update_summary()

    def _update_summary(self, *_args):
        """Kurzzeile: wie viele Aktionen gerade ausgewählt sind."""
        try:
            uploads = len(self.selected_upload_ids())
            metadata = len(self.selected_metadata_ids())
            marked_read = len(self.selected_read_targets())
            marked_unread = len(self.selected_read_unmarks())
            read_text = "%d als gelesen" % marked_read
            if marked_unread:
                read_text += ", %d auf ungelesen" % marked_unread
            summary = getattr(self, "summary", None)
            if summary is not None:
                summary.setText(
                    "<b>%d zum Hochladen, %d für Metadaten, %s</b> ausgewählt."
                    % (uploads, metadata, read_text))
            confirm = getattr(self, "confirm", None)
            if confirm is not None:
                total = uploads + metadata + marked_read + marked_unread
                confirm.setText("Auswahl übernehmen (%d)" % total
                                if total else "Auswahl übernehmen")
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
                         report_error(exc, "Anmeldung"), show=True)
            return None

    def download_selected(self):
        row = self._selected_row()
        if row is None:
            return
        tolino_id = row.get("tolino_id")
        if not tolino_id:
            QMessageBox.information(
                self, "Tolino Cloud Sync",
                "Nur Bücher aus der Cloud lassen sich herunterladen.")
            return
        try:
            client = self._client()
            if client is None:
                return
            content, _metadata = client.download(tolino_id)
        except Exception as exc:
            error_dialog(self, "Download fehlgeschlagen",
                         report_error(exc, "Download"), show=True)
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
                "Das Buch liegt nicht in der Cloud.")
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
                         report_error(exc, "Sammlung"), show=True)
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


class DiagnosticDialog(QDialog):
    def __init__(self, dashboard, report, parent=None):
        QDialog.__init__(self, parent)
        self.dashboard = dashboard
        self.setWindowTitle("Diagnose")
        self.setMinimumSize(760, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Die Diagnose läuft lokal, ohne Anmeldung und ohne Netz. Der "
            "Tolino-Test greift erst nach Klick auf das Netz zu."))
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        errors = recent_errors()
        if errors:
            report = "%s\n\nLetzte Fehler:\n%s" % (report, "\n".join(errors))
        self.output.setPlainText(report)
        layout.addWidget(self.output)
        buttons = QHBoxLayout()
        copy = QPushButton("Diagnose kopieren")
        copy.clicked.connect(self.copy_report)
        test = QPushButton("Tolino-Antwort testen")
        test.clicked.connect(self.test_tolino)
        close = QPushButton("Schliessen")
        close.clicked.connect(self.accept)
        buttons.addWidget(copy)
        buttons.addWidget(test)
        buttons.addWidget(close)
        layout.addLayout(buttons)

    def copy_report(self):
        self.output.selectAll()
        self.output.copy()

    @staticmethod
    def _sync_data_summary(client, secrets=()):
        """Kurzer Ueberblick ueber die sync-data-Antwort (0.15.1).

        Die Spalte «Gelesen» und die gemerkten Serien stammen aus dieser
        Antwort. Der Bericht zeigt deshalb, OB der Server Marken liefert und
        wie viele davon Sammlungen bzw. Gelesen-Marken sind – sonst ist
        spaeter nicht zu erkennen, ob eine leere Spalte an der Cloud oder am
        Plugin lag. Ein Fehler hier bricht den Test nicht ab.
        """
        try:
            revision, patches = client.sync_snapshot()
            categories = sorted({
                str((item.get("value") or {}).get("category") or "")
                for item in patches if isinstance(item, dict)
            })
            return {
                "revision_present": revision not in (None, "", 0),
                "patch_count": len(patches),
                "collections": len(client.collection_pairs()),
                "read_marks": len(client.read_ids()),
                "tag_categories": categories,
                "sample_paths": [
                    str(item.get("path") or "") for item in patches[:3]
                    if isinstance(item, dict)
                ],
            }
        except Exception as exc:
            return {"error_type": type(exc).__name__,
                    "error_message": sanitize_error(
                        exc, secrets + (getattr(client, "access", None),))}

    def test_tolino(self):
        values = self.dashboard.values()
        if not values["refresh_token"]:
            QMessageBox.warning(self, "Tolino-Antwort testen",
                                "Es ist kein Zugang gespeichert. Bitte zuerst anmelden.")
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
                    "sync_data": DiagnosticDialog._sync_data_summary(
                        client, (values["refresh_token"],)),
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

    def __init__(self, settings, jobs, removals, meta_updates=None,
                 series_map=None, read_marks=None, read_unmarks=None):
        QObject.__init__(self)
        self.settings = settings
        self.jobs = jobs
        self.removals = removals
        self.meta_updates = list(meta_updates or ())
        self.series_map = dict(series_map or {})
        # Tolino-IDs, die in der Cloud als gelesen markiert werden sollen
        # (Spalte "Gelesen"). Leer heisst: Lese-Status bleibt unangetastet.
        self.read_marks = [str(item) for item in (read_marks or ())
                           if str(item or "").strip()]
        # Tolino-IDs, deren Lese-Marke wieder ENTFERNT werden soll (0.15.2:
        # Häkchen in der Spalte „Gelesen“ weggeklickt). Ebenfalls leer =
        # Lese-Status bleibt unangetastet.
        self.read_unmarks = [str(item) for item in (read_unmarks or ())
                             if str(item or "").strip()]
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        # Ein laufender Sync sperrt den Keep-alive aus: sonst loesen beide
        # denselben Refresh-Token ein (Keycloak-Reuse-Schutz) und die
        # Sitzung stirbt (0.15.5).
        mark_sync_active(True)
        try:
            self._run()
        finally:
            mark_sync_active(False)

    def _run(self):
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
            # Serien → Sammlungen: nur noch OFFENE Zuordnungen. Bereits
            # geschriebene stehen als "collections" am Buch (0.14.0).
            series_items = plan_collection_updates(self.series_map, state)
            total = (len(self.jobs) + len(self.removals)
                     + len(self.meta_updates) + len(series_items)
                     + (1 if self.read_marks else 0)
                     + (1 if self.read_unmarks else 0))
            done = 0
            # Erst die in-place Metadaten-Updates, dann die Uploads (0.9.45).
            for update in self.meta_updates:
                if self.cancelled:
                    raise RuntimeError(CANCEL_MESSAGE)
                record = unpack_meta_update_record(update)
                self.progress.emit(done, total, "Metadaten: %s" % record["book_uuid"])
                client.update_metadata(record["tolino_id"], record["fields"])
                done += 1
            for job in self.jobs:
                if self.cancelled:
                    raise RuntimeError(CANCEL_MESSAGE)
                self.progress.emit(done, total, "Lade hoch: %s (%s)" %
                                   (job.book_uuid, job.format_name))
                new_id = client.upload(job.path)
                # Plan-Eintrag kopieren: fingerprint/content_fp/meta_fp bleiben
                # erhalten, nur die Tolino-ID wird neu (0.9.45).
                entry = dict(state.get(job.book_uuid) or {})
                entry.update({"tolino_id": new_id, "calibre_id": job.book_id})
                # Neue Cloud-Kopie: alte Serien-Zuordnungen gelten nicht mehr.
                entry.pop("collections", None)
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
                    raise RuntimeError(CANCEL_MESSAGE)
                if str(tolino_id) in remote_ids:
                    self.progress.emit(done, total, "Lösche: %s" % book_uuid)
                    client.delete(tolino_id)
                state.pop(book_uuid, None)
                done += 1
                self.progress.emit(done, total, "Erledigt: %s" % book_uuid)
            # Lese-Marke: nur die ausdrücklich ausgewählten Bücher, und nur,
            # wenn sie in der Cloud noch nicht gesetzt ist. Sammlungen und
            # andere Marken bleiben dabei unverändert (0.14.0).
            if self.read_marks:
                if self.cancelled:
                    raise RuntimeError(CANCEL_MESSAGE)
                self.progress.emit(done, total, "Als gelesen markieren")
                client.mark_read_many(self.read_marks)
                done += 1
                self.progress.emit(done, total, "Als gelesen markiert")
            # Umgekehrt: Bücher, die wieder ungelesen sein sollen. Nur die
            # vorhandene Marke wird entfernt, alles andere bleibt (0.15.2).
            if self.read_unmarks:
                if self.cancelled:
                    raise RuntimeError(CANCEL_MESSAGE)
                self.progress.emit(done, total, "Als ungelesen markieren")
                client.mark_read_many(self.read_unmarks, finished=False)
                done += 1
                self.progress.emit(done, total, "Als ungelesen markiert")
            # Serien → Cloud-Sammlungen: nur die offenen Zuordnungen. Der
            # Cloud-Bestand wird einmal gelesen, jede geschriebene oder
            # bereits vorhandene Zuordnung aber im Zustand gemerkt -- beim
            # nächsten Sync ist hier deshalb nichts mehr zu tun (0.14.0).
            if series_items:
                known = client.collection_pairs()
                revision = None
                for book_uuid, tolino_id, series in series_items:
                    if self.cancelled:
                        raise RuntimeError(CANCEL_MESSAGE)
                    if (tolino_id, series) not in known:
                        self.progress.emit(done, total,
                                           "Serie in der Cloud: %s" % series)
                        revision = client.add_to_collection(
                            tolino_id, series, revision=revision)
                        known.add((tolino_id, series))
                    remember_collection(state, book_uuid, series)
                    done += 1
                self.progress.emit(done, total, "Serien geprüft")
            self.completed.emit(state, client.refresh, updates)
        except Exception as exc:
            message = (CANCEL_MESSAGE if str(exc) == CANCEL_MESSAGE
                       else report_error(exc, "Sync", (
                           self.settings["refresh_token"],
                           getattr(client, "access", None))))
            self.failed.emit(
                message,
                client.refresh if client else self.settings["refresh_token"])

    def persist_refresh_token(self, refresh):
        """Rotierten Token des laufenden Syncs speichern (0.15.5: an einer Stelle)."""
        self.settings["refresh_token"] = refresh
        save_credentials(self.settings["account_name"], refresh,
                         self.settings["hardware_id"])

    def persist_hardware(self, hardware):
        """Adopted device id from the BOSH recovery (0.9.35)."""
        self.settings["hardware_id"] = hardware
        save_credentials(self.settings["account_name"],
                         self.settings["refresh_token"], hardware)


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
            self.failed.emit(report_error(exc, "Anmeldefenster"))
            return
        self.completed.emit(refresh, hardware)


class SessionCheckWorker(QObject):
    """Prueft den gespeicherten Zugang, bevor ein Anmeldefenster aufgeht.

    Der Test ist ein Netz-Roundtrip und rotiert den Refresh-Token -- im
    GUI-Thread wuerde Calibre dabei stehen (0.15.7).
    """
    valid = pyqtSignal(str)
    invalid = pyqtSignal(str)
    busy = pyqtSignal(str)

    def __init__(self, settings):
        QObject.__init__(self)
        self.settings = settings

    def run(self):
        values = self.settings
        try:
            status, detail = check_stored_login(
                values["partner_id"], values["hardware_id"],
                values["refresh_token"], values.get("username"),
                values.get("password"), persist=self.persist_refresh_token)
        except Exception as exc:
            self.invalid.emit(report_error(
                exc, "Zugangsprüfung", (values["refresh_token"],)))
            return
        if status == "valid":
            self.valid.emit(detail)
        elif status == "busy":
            self.busy.emit(detail)
        else:
            self.invalid.emit(detail)

    def persist_refresh_token(self, refresh):
        """Rotierten Token sofort sichern -- ohne GUI-Zugriff (Arbeitsthread)."""
        self.settings["refresh_token"] = refresh
        save_credentials(self.settings["account_name"], refresh,
                         self.settings["hardware_id"])


class SettingsDialog(QDialog):
    """Alle seltenen Einstellungen an einem Ort (0.15.2).

    Übertragung, Konten und Diagnose lagen vorher hinter zwei verschiedenen
    „… anzeigen"-Häkchen im Hauptfenster. Die Fenster dort bleiben kurz:
    anmelden, vergleichen, fertig.
    """

    def __init__(self, dashboard):
        QDialog.__init__(self, dashboard)
        self.dashboard = dashboard
        self.setWindowTitle("Einstellungen")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)

        self.transfer = QGroupBox("Übertragung")
        self.transfer_form = QFormLayout(self.transfer)
        layout.addWidget(self.transfer)

        self.accounts = QGroupBox("Konten")
        self.accounts_form = QFormLayout(self.accounts)
        layout.addWidget(self.accounts)

        self.diagnostics = QGroupBox("Diagnose")
        self.diagnostics_form = QFormLayout(self.diagnostics)
        layout.addWidget(self.diagnostics)

        self.close_button = QPushButton("Fertig")
        self.close_button.setToolTip("Einstellungen speichern und schliessen.")
        self.close_button.clicked.connect(self.dashboard.close_settings)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)


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
            "<b>1)</b> Anmelden &nbsp;→&nbsp; <b>2)</b> vergleichen und pro "
            "Buch auswählen, was in die Cloud soll. Alles Weitere ist "
            "optional.")
        intro.setWordWrap(True)
        root.addWidget(intro)

        # --- Ein Ablauf: anmelden, dann vergleichen ------------------------
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem("%s – %s" % (pid, partner["name"]), pid)
        self.partner.setToolTip(
            "Ihr Buchhändler (Tolino-Partner). Hier meldet sich das "
            "Plugin an.")
        self.status = QLabel()
        self.status.setWordWrap(True)
        # Kontoauswahl direkt auf der ersten Seite -- aber nur, wenn es
        # wirklich mehrere Konten gibt. Bei einem einzigen Konto wäre die
        # Zeile nur Ballast (0.15.4).
        self.account_quick = QComboBox()
        self.account_quick.setToolTip(
            "Aktives Konto. Jedes Konto hat eigenen Zugang und Stand.")
        self.account_quick.currentIndexChanged.connect(
            self.quick_account_changed)
        self.account_quick_label = QLabel("Konto")
        # Der Anmeldeknopf steht wieder sichtbar auf der ersten Seite --
        # auch wenn schon ein Zugang gespeichert ist, denn genau dann
        # braucht man ihn, wenn der Zugang abgelaufen ist (0.15.3).
        self.login_button = QPushButton("Jetzt bei Tolino anmelden")
        self.login_button.setToolTip(
            "Öffnet das Anmeldefenster: im Web Reader anmelden, bis die "
            "Bücherliste lädt. Auch nötig, wenn der Zugang abgelaufen ist.")
        self.login_button.clicked.connect(self.browser_login)
        self.primary = QPushButton("Vergleichen und übernehmen …")
        self.primary.setToolTip(
            "Öffnet den Vergleich: pro Buch Hochladen, Metadaten oder "
            "«Gelesen» auswählen. Läuft ein Sync, bricht diese Schaltfläche "
            "ab.")
        self.primary.clicked.connect(self.primary_action)
        self.progress_label = QLabel("Bereit")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.settings_button = QPushButton("Einstellungen …")
        self.settings_button.setToolTip(
            "Formate, Titelbilder, Serien, Löschungen, Konten und "
            "Diagnose. Normalerweise nicht nötig.")
        self.settings_button.clicked.connect(self.open_settings)

        action_form = QFormLayout()
        action_form.addRow("Buchhändler", self.partner)
        action_form.addRow(self.account_quick_label, self.account_quick)
        action_form.addRow("", self.login_button)
        action_form.addRow("", self.primary)
        action_form.addRow("", self.status)
        action_form.addRow("", self.progress_label)
        action_form.addRow("", self.progress)
        action_form.addRow("", self.settings_button)
        root.addLayout(action_form)

        # --- Alles Seltene hinter EINEM Fenster ----------------------------
        self.settings_dialog = SettingsDialog(self)

        transfer = self.settings_dialog.transfer_form
        self.formats = QLineEdit()
        self.formats.setToolTip(
            "Reihenfolge beim Hochladen (komma-getrennt). Standard: EPUB.")
        self.covers = QCheckBox("Titelbild mit hochladen")
        self.covers.setToolTip(
            "Lädt das Titelbild mit hoch.")
        self.compare_authors = QCheckBox(
            "Autor(en) beim Vergleich berücksichtigen")
        self.compare_title = QCheckBox("Titel beim Vergleich berücksichtigen")
        self.compare_isbn = QCheckBox("ISBN beim Vergleich berücksichtigen")
        for field in (self.compare_authors, self.compare_title, self.compare_isbn):
            field.setChecked(True)
        self.sync_collections = QCheckBox("Serien in der Cloud anlegen")
        self.sync_collections.setToolTip(
            "Bücher einer Calibre-Serie werden in der Cloud als "
            "«Sammlung» zugeordnet. Standard: aus.")
        self.deletions = QCheckBox("Löschungen aus der Cloud übernehmen")
        self.tolino_column = QCheckBox(
            "Tolino-ID in eigene Spalte schreiben (nur ein Konto)")
        self.tolino_column.setToolTip(
            "Schreibt die Tolino-ID in die Calibre-Spalte #tolino_id. "
            "Nur bei genau einem Konto sinnvoll.")
        automatic_hint = QLabel("Zusätzlich zu Ihrer Auswahl im Vergleich:")
        automatic_hint.setWordWrap(True)
        transfer.addRow("Bevorzugte Formate", self.formats)
        transfer.addRow("", self.covers)
        transfer.addRow("", self.compare_authors)
        transfer.addRow("", self.compare_title)
        transfer.addRow("", self.compare_isbn)
        transfer.addRow("", automatic_hint)
        transfer.addRow("", self.sync_collections)
        transfer.addRow("", self.deletions)
        transfer.addRow("", self.tolino_column)

        accounts = self.settings_dialog.accounts_form
        self.account_select = QComboBox()
        self.account_new = QPushButton("Neues Konto")
        self.account_remove = QPushButton("Konto löschen")
        account_buttons = QHBoxLayout()
        account_buttons.addWidget(self.account_new)
        account_buttons.addWidget(self.account_remove)
        self.account_select.currentIndexChanged.connect(self.account_changed)
        self.account_new.clicked.connect(self.new_account)
        self.account_remove.clicked.connect(self.remove_account)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(QLineEdit.Password)
        self.grab_button = QPushButton("Zugang aus offenem Browser holen")
        self.grab_button.setToolTip(
            "Holt den Zugang aus dem Anmeldefenster (benötigt Chrome, "
            "Chromium, Brave oder Edge).")
        self.grab_button.clicked.connect(self.grab_browser_tokens)
        accounts.addRow("Aktives Konto", self.account_select)
        accounts.addRow("", account_buttons)
        accounts.addRow("", self.grab_button)
        accounts.addRow("Hardware-ID (automatisch erkannt)", self.hardware)
        accounts.addRow("Zugangsschlüssel (Refresh-Token)", self.refresh)

        # Feldbefund 0.10.0: ein gebauter, aber nie eingehängter Knopf ist
        # unsichtbar -- der Regressionstest prüft das für die ganze Datei.
        self.debug = QPushButton("Diagnose")
        self.debug.setToolTip(
            "Prüft Datenbank, Formate und Titelbilder lokal – ohne Anmeldung "
            "und ohne Netz.")
        self.debug.clicked.connect(self.debug_diagnose)
        self.settings_dialog.diagnostics_form.addRow("", self.debug)

        # Plain close button instead of QDialogButtonBox: immune to the Qt6
        # "Invalid ButtonRole, button not added" warning seen in the field.
        close = QPushButton("Schliessen")
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
            "sync_collections": self.sync_collections.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
        })
        save_account(self.account_name, self.values(), active=self.account_name)

    def account_changed(self, index):
        """Konto in den Einstellungen gewechselt."""
        if index < 0 or not getattr(self, "account_names", None):
            return
        self.switch_account(self.account_select.itemData(index))

    def quick_account_changed(self, index):
        """Konto auf der ersten Seite gewechselt (0.15.4)."""
        if index < 0 or not getattr(self, "account_names", None):
            return
        self.switch_account(self.account_quick.itemData(index))

    def switch_account(self, name):
        """Konto wechseln – erst sichern, dann das neue laden.

        Ein Weg für beide Stellen (erste Seite und Einstellungen): das
        sichtbare Konto wird gespeichert, das gewählte wird das aktive, und
        `load_values` holt Buchhändler, Hardware-ID und Zugang dazu.
        Angezeigtes und aktives Konto sind danach IMMER dasselbe: sonst
        lesen settings() und die Tolino-ID-Zuordnung das vorherige Konto.
        """
        name = str(name or "")
        if not name or name == self.account_name:
            return False
        if name not in getattr(self, "account_names", ()):
            return False
        self._save_visible_account()
        set_active_account(name)
        self.load_values()
        return True

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
        self.account_name = values["active_account"]
        for combo in (self.account_select, self.account_quick):
            combo.blockSignals(True)
            combo.clear()
            for name in self.account_names:
                combo.addItem(name, name)
            combo.setCurrentIndex(max(0, combo.findData(self.account_name)))
            combo.blockSignals(False)
        # Ein einziges Konto braucht keine Auswahl auf der ersten Seite.
        multiple = len(self.account_names) > 1
        self.account_quick.setVisible(multiple)
        self.account_quick_label.setVisible(multiple)
        self.partner.setCurrentIndex(max(0, self.partner.findData(values["partner_id"])))
        self.hardware.setText(values["hardware_id"] or hardware_id())
        self.refresh.setText(values["refresh_token"])
        # Vergleichsstand fuer values(): nur was der Nutzer WIRKLICH aendert,
        # darf den gespeicherten Zugang ersetzen. Ein im Hintergrund
        # rotierter Token liegt nicht im Feld und wuerde sonst von der alten
        # Kopie ueberschrieben (0.15.5).
        self._loaded_refresh = normalize_refresh_token(self.refresh.text())[0]
        self._loaded_hardware = self.hardware.text().strip()
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.covers.setChecked(values["upload_covers"])
        self.deletions.setChecked(values["enable_deletions"])
        self.sync_collections.setChecked(values["sync_collections"])
        self.tolino_column.setChecked(values["use_tolino_column"])
        self.update_status()

    def open_settings(self):
        """Einstellungen öffnen – speichert beim Schliessen (0.15.2)."""
        self.settings_dialog.exec()

    def close_settings(self):
        self._save_visible_account()
        self.settings_dialog.accept()

    def primary_action(self):
        """Hauptknopf: vergleichen – oder einen laufenden Sync abbrechen.

        Das Anmelden hat seit 0.15.3 wieder seinen eigenen Knopf darüber,
        damit es auch bei abgelaufenem Zugang sofort erreichbar ist.
        """
        if self.thread is not None:
            self.cancel_sync()
        else:
            self.start_sync()

    def update_status(self):
        logged_in = bool(self.refresh.text().strip())
        running = self.thread is not None
        if running:
            self.status.setText(
                "Synchronisierung läuft – «Abbrechen» stoppt sie.")
        elif logged_in:
            self.status.setText(
                "✓ Angemeldet – bereit zum Vergleichen.")
        else:
            self.status.setText(
                "● Noch nicht angemeldet. " + RELOGIN)
        self.progress.setVisible(running)
        self.progress_label.setVisible(running)
        if running:
            self.primary.setText("Abbrechen")
            self.primary.setEnabled(True)
        else:
            self.primary.setText("Vergleichen und übernehmen …")
            # Ohne Zugang gibt es nichts zu vergleichen -- der Grund steht
            # im Tooltip und in der Statuszeile, der Anmeldeknopf ist direkt
            # darüber sichtbar.
            self.primary.setEnabled(logged_in)
            self.primary.setToolTip(
                "Öffnet den Vergleich: pro Buch Hochladen, Metadaten oder "
                "«Gelesen» auswählen." if logged_in else
                "Noch nicht angemeldet. " + RELOGIN)

    def set_refresh_token(self, refresh):
        normalized, _ = normalize_refresh_token(refresh)
        self.refresh.setText(normalized)
        # Frisch gelesen heisst gewollt: der Wert ist jetzt der Vergleichsstand
        # fuer values() (0.15.5).
        self._loaded_refresh = normalized
        self.update_status()

    def persist_refresh_token(self, refresh):
        """Rotierten Token speichern (0.15.5: derselbe Weg wie im Worker)."""
        self.set_refresh_token(refresh)
        save_credentials(self.account_name, self.refresh.text(),
                         self.hardware.text().strip() or hardware_id())

    def persist_hardware(self, hardware):
        """Adopted device id from the BOSH recovery (0.9.35)."""
        self.hardware.setText(str(hardware))
        self._loaded_hardware = self.hardware.text().strip()
        save_credentials(self.account_name, self.refresh.text(), str(hardware))

    def grab_browser_tokens(self):
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
                    self, "Zugang übernommen",
                    "Zugang übernommen und gespeichert. Das Anmeldefenster "
                    "wurde geschlossen.")
                return
            # Kein Fenster: selbst das eigene Chromium-Fenster oeffnen und
            # den Token live uebernehmen.
            if chromium_available():
                if (self.login_thread is not None
                        and self.login_thread.isRunning()):
                    return  # eine Anmeldung läuft bereits
                self.status.setText(
                    "Anmeldefenster wird geöffnet: dort anmelden, bis die "
                    "Bücherliste lädt.")
                # Bewusst OHNE Vorab-Prüfung: diese Schaltfläche holt den
                # Zugang ausdrücklich aus dem Browser (0.15.7).
                self._start_login_window(self.values())
                return
            QMessageBox.information(
                self, "Kein Chromium-Browser gefunden",
                "Bitte Chrome, Chromium, Brave oder Edge installieren und "
                "danach auf «Jetzt bei Tolino anmelden» klicken.\n\n"
                "Oder den Zugang von Hand eintragen: im Web Reader "
                "anmelden, F12 → Netzwerk, refresh_token und hardware_id "
                "kopieren und unter «Einstellungen …» einfügen.")
        except TolinoAuthError as exc:
            QMessageBox.warning(
                self, "Anmeldung nicht abgeschlossen",
                report_error(exc, "Zugang übernehmen"))
        except Exception as exc:
            QMessageBox.critical(
                self, "Fehler", report_error(exc, "Zugang übernehmen"))

    def _selected_account(self):
        """The account this dialog shows – never a different 'active' one."""
        return account_by_name(getattr(self, "account_name", None))

    def values(self):
        refresh_token, _ = normalize_refresh_token(self.refresh.text())
        account = self._selected_account() or settings()
        if refresh_token == getattr(self, "_loaded_refresh", None):
            # Unveraendertes Feld: es gilt der GESPEICHERTE Zugang. Hat der
            # Keep-alive im Hintergrund rotiert, wuerde die alte Kopie sonst
            # beim Speichern oder beim Sync die Rotation verbrennen (0.15.5).
            stored_token, _ = normalize_refresh_token(
                account.get("refresh_token") or "")
            if stored_token:
                refresh_token = stored_token
        hardware = self.hardware.text().strip() or hardware_id()
        if hardware == getattr(self, "_loaded_hardware", None):
            stored_hardware = str(account.get("hardware_id") or "").strip()
            if stored_hardware:
                hardware = stored_hardware
        return {
            "account_name": self.account_name,
            "partner_id": self.partner.currentData(),
            "hardware_id": hardware,
            "refresh_token": refresh_token,
            "username": account.get("username", ""),
            "password": account.get("password", ""),
            "preferred_formats": [x.strip().upper()
                                  for x in self.formats.text().split(",") if x.strip()],
            "upload_covers": self.covers.isChecked(),
            "enable_deletions": self.deletions.isChecked(),
            "sync_collections": self.sync_collections.isChecked(),
            "use_tolino_column": self.tolino_column.isChecked(),
            "state": (account.get("state")
                      if isinstance(account.get("state"), dict) else {}),
        }

    def _login_running(self):
        """Läuft gerade eine Zugangs-Prüfung oder ein Anmeldefenster?"""
        thread = self.login_thread
        return thread is not None and thread.isRunning()

    def browser_login(self):
        """Anmeldeknopf: erst den gespeicherten Zugang prüfen, dann anmelden.

        Ein neues Anmeldefenster ist nur nötig, wenn der vorhandene Zugang
        nicht mehr funktioniert. Deshalb läuft zuerst eine stille Prüfung im
        Hintergrund; das Fenster geht erst auf, wenn Tolino den Zugang
        ablehnt (0.15.7). Ohne gespeicherten Zugang gibt es nichts zu prüfen.
        """
        if self._login_running():
            return  # eine Prüfung oder Anmeldung läuft bereits
        settings = self.values()
        if settings["refresh_token"]:
            self._check_stored_login(settings)
            return
        self._start_login_window(settings)

    def _check_stored_login(self, settings):
        """Gespeicherten Zugang im Hintergrund prüfen (ein Netz-Roundtrip)."""
        self.primary.setEnabled(False)
        self.grab_button.setEnabled(False)
        self.status.setText("Gespeicherter Zugang wird geprüft …")
        thread = QThread()  # no parent: dialog may close first
        worker = SessionCheckWorker(settings)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for signal in (worker.valid, worker.invalid, worker.busy):
            signal.connect(thread.quit)
        worker.valid.connect(self.login_still_valid)
        worker.invalid.connect(self.login_rejected)
        worker.busy.connect(self.login_check_busy)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(lambda: _reap_login_thread(thread))
        _LOGIN_WORKER_THREADS.append(thread)
        self.login_thread = thread
        self.login_worker = worker
        thread.start()

    def login_still_valid(self, refresh):
        """Prüfung bestanden: kein Anmeldefenster, Zugang bleibt gültig."""
        try:
            self._login_cleanup()
            self.set_refresh_token(refresh)
            self.persist_refresh_token(refresh)
            self.status.setText("✓ Angemeldet – Zugang gilt weiterhin.")
            QMessageBox.information(
                self, "Zugang geprüft",
                "Der gespeicherte Zugang gilt weiterhin – kein Anmelden "
                "nötig.")
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def login_rejected(self, message=""):
        """Prüfung fehlgeschlagen: jetzt das Anmeldefenster öffnen.

        Der technische Grund landet im Log statt im Fenster: die Box bleibt
        eine Zeile, und für die Diagnose steht er weiter bereit (0.15.8).
        """
        try:
            self._login_cleanup()
            settings = self.values()
        except RuntimeError:
            return  # dialog already destroyed (Calibre shutdown)
        log_warning("Zugang abgelehnt: " + str(message))
        QMessageBox.warning(
            self, "Zugang abgelehnt",
            "Der gespeicherte Zugang gilt nicht mehr. Bitte im "
            "Anmeldefenster neu anmelden.")
        self._start_login_window(settings)

    def login_check_busy(self, _reason=""):
        """Der Zugang wird gerade benutzt – jetzt nicht dazwischenfunken."""
        try:
            self._login_cleanup()
            self.status.setText("Zugang wird gerade verwendet – bitte später erneut.")
            QMessageBox.information(
                self, "Zugang in Benutzung",
                "Sync oder Erneuerung läuft. Bitte danach erneut klicken.")
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def _start_login_window(self, settings):
        """Das eigene Anmeldefenster öffnen und den frischen Zugang übernehmen."""
        if self._login_running():
            return  # a sign-in attempt is already running
        partner_id = settings["partner_id"]
        self.primary.setEnabled(False)
        self.grab_button.setEnabled(False)
        self.status.setText(
            "Anmeldung läuft: im Anmeldefenster anmelden, bis die "
            "Bücherliste lädt.")
        thread = QThread()  # no parent: dialog may close first
        worker = BrowserLoginWorker(partner_id, settings["hardware_id"])
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
                "Der neue Zugang wurde gespeichert.")
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
            self.primary.setEnabled(True)
            self.grab_button.setEnabled(True)
            self.update_status()
        except RuntimeError:
            pass  # dialog already destroyed

    def _cloud_client(self):
        """Build a logged-in client from the current dialog values.

        Raises on failure; the inventory dialog turns that into an error
        dialog. The rotated refresh token is persisted immediately.
        """
        settings = self.values()
        if not settings["refresh_token"]:
            raise TolinoAuthError("Noch nicht angemeldet. " + RELOGIN,
                                  code="A03")
        self._save_visible_account()
        client = TolinoClient(
            settings["partner_id"], settings["hardware_id"],
            settings["refresh_token"], settings["username"],
            settings["password"], token_callback=self.persist_refresh_token,
            hardware_callback=self.persist_hardware)
        client.login()
        self.set_refresh_token(client.refresh)
        return client

    def _show_prepare_error(self, exc, settings):
        """Nur Kurztext plus Code im Dialog; alles Technische steht im Log."""
        secrets = (settings.get("refresh_token"),)
        error_dialog(self, "Vorbereitung fehlgeschlagen",
                     report_error(exc, "Vorbereitung", secrets), show=True)

    def start_sync(self):
        """Vergleichen und den Hintergrundlauf starten.

        Hier steht nur die Reihenfolge und die Fehlerbehandlung der
        Vorbereitung; die Schritte selbst liegen in `_prepare_sync`
        (Metadaten lesen, anmelden, vergleichen, Auswahl einholen, planen)
        und `_start_sync_worker` (übersprungene Uploads melden, Thread
        starten).
        """
        if self.thread is not None:
            return
        settings = self.values()
        if not settings["refresh_token"]:
            QMessageBox.warning(self, "Noch nicht angemeldet",
                                "Bitte zuerst oben auf «Jetzt bei Tolino "
                                "anmelden» klicken.")
            return
        # EINE Regel fuer Lesen UND Schreiben der Tolino-ID-Spalte
        # (tolino_column_active): nur bei genau einem Konto.
        self.sync_column_enabled = tolino_column_active(
            settings, self.gui.current_db)
        try:
            preparation = self._prepare_sync(settings)
        except Exception as exc:
            self._show_prepare_error(exc, settings)
            return
        if preparation is None:
            # Der Nutzer hat das Bestätigungsfenster abgebrochen.
            return
        self._start_sync_worker(settings, preparation)

    def _prepare_sync(self, settings):
        """Alles vor dem Hintergrundlauf; None bei Abbruch im Fenster."""
        metadata = self._local_metadata()
        state = load_state(settings["state"])
        client = self._logged_in_client(settings)
        comparison = self._cloud_comparison(settings, metadata, state, client)
        selection = self._confirm_comparison(comparison, settings)
        if selection is None:
            return None
        return self._plan_actions(settings, metadata, state, selection)

    def _local_metadata(self):
        """Calibre-Metadaten aller Bücher inkl. Format-Signaturen lesen."""
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
        return metadata

    def _logged_in_client(self, settings):
        """Anmelden und einen rotierten Refresh-Token sofort persistieren."""
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
        return client

    def _cloud_comparison(self, settings, metadata, state, client):
        """Cloud-Inventar und Lese-Status holen, Vergleichszeilen bilden."""
        comparison_fields = [
            name for name, checkbox in (
                ("authors", self.compare_authors),
                ("title", self.compare_title),
                ("isbn", self.compare_isbn),
            ) if checkbox.isChecked()
        ]
        # Lese-Status aus der Cloud holen, damit die Spalte „Gelesen“
        # vorbelegt ist. Scheitert das (z. B. Netzfehler), läuft der
        # Vergleich ohne Vorbelegung weiter (0.15.0).
        inventory = client.inventory()
        try:
            read_ids = client.read_ids()
        except Exception:
            read_ids = set()
        return compare_inventory(
            metadata, state, inventory, settings["preferred_formats"],
            comparison_fields,
            use_metadata_ids=self.sync_column_enabled,
            read_ids=read_ids,
        )

    def _confirm_comparison(self, comparison, settings):
        """Bestätigungsfenster zeigen; None, wenn es abgebrochen wurde."""
        dialog = InventoryDialog(
            comparison, self,
            client_factory=lambda: self._cloud_client(),
            options={
                "collections": bool(settings["sync_collections"]),
                "deletions": bool(settings["enable_deletions"]),
            })
        if dialog.exec() != QDialog.Accepted:
            return None
        # Die Auswahl des bestätigten Fensters entscheidet, was
        # passiert -- jede Aktion für sich; eine leere Auswahl ändert
        # nichts.
        return {
            "uploads": dialog.selected_upload_ids(),
            "metadata": dialog.selected_metadata_ids(),
            "read": dialog.selected_read_targets(),
            "unread": dialog.selected_read_unmarks(),
        }

    def _plan_actions(self, settings, metadata, state, selection):
        """Aus der Auswahl die Jobs und den neuen Sync-Stand bauen."""
        uploads, removals, current = unpack_plan_result(plan_sync(
            metadata, state, settings["preferred_formats"],
            settings["enable_deletions"],
            upload_book_ids=selection["uploads"],
        ))
        # "Metadaten" gleicht vorhandene Cloud-Einträge in-place ab
        # (/meta) -- nur für die ausgewählten Bücher (0.13.0).
        uploads, meta_updates = plan_metadata_updates(
            metadata, state, uploads, settings["preferred_formats"],
            only_book_ids=selection["metadata"])
        # Serien → Sammlungen: ohne eingeschaltete Sammlungen bleibt
        # die Zuordnung leer und die Cloud wird nicht angefasst.
        series_map = (series_map_from_metadata(metadata)
                      if settings["sync_collections"] else {})
        jobs, skipped_uploads = self._upload_jobs(settings, metadata, uploads)
        # Nur wirklich ausgeführte Aktionen zählen als abgeglichen:
        # alles andere behält den alten Cloud-Stand (0.13.0).
        synced = {job.book_uuid for job in jobs}
        synced.update(str(record[1]) for record in meta_updates)
        revert_unsynced_fingerprints(current, state, synced)
        settings["state"] = current
        return {
            "jobs": jobs,
            "removals": removals,
            "meta_updates": meta_updates,
            "series_map": series_map,
            "skipped_uploads": skipped_uploads,
            "selected_read": selection["read"],
            "selected_unread": selection["unread"],
        }

    def _upload_jobs(self, settings, metadata, uploads):
        """Upload-Jobs bauen; fehlende Dateien landen in `skipped_uploads`."""
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
                    raise ValueError("Datei nicht gefunden: %s" % path)
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
        return jobs, skipped_uploads

    def _start_sync_worker(self, settings, preparation):
        """Übersprungene Uploads melden und den Hintergrundlauf starten."""
        skipped_uploads = preparation["skipped_uploads"]
        if skipped_uploads:
            QMessageBox.warning(
                self, "Tolino Cloud Sync",
                "Einige Uploads wurden übersprungen:\n\n%s" %
                "\n".join(skipped_uploads))
        jobs = preparation["jobs"]
        summary = sync_summary(len(jobs), len(preparation["removals"]))
        self.progress.setRange(0, summary["total"] or 1)
        self.progress.setValue(0)
        self.thread = QThread(self)
        self.worker = SyncWorker(settings, jobs, preparation["removals"],
                                 preparation["meta_updates"],
                                 preparation["series_map"],
                                 preparation["selected_read"],
                                 preparation["selected_unread"])
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.sync_completed)
        self.worker.failed.connect(self.sync_failed)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.progress.setValue(0)
        self.progress_label.setText("Synchronisierung läuft")
        self.update_status()
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
            self.primary.setEnabled(False)

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
        except Exception as exc:
            # Nicht still scheitern: eine unterbliebene Rotation faellt sonst
            # erst Stunden spaeter als abgelaufene Sitzung auf. Ohne Token.
            log_warning("Tolino-Keep-alive konnte den neuen Zugang nicht "
                        "speichern: %s" % sanitize_error(exc, (refresh,)))

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

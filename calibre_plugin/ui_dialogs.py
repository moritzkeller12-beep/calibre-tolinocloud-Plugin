"""Dialoge für den tolino-Bibliotheksvergleich."""
import re
from .ui_common import (QCheckBox, QComboBox, QDialog, QFileDialog,
                        QHBoxLayout, QLabel, QMessageBox,
                        QMenu, QPushButton, QTableWidget, QTableWidgetItem,
                        QVBoxLayout, Qt, _set_enabled, header_mode, plural,
                        start_call)
from calibre.gui2 import error_dialog, info_dialog
from .comparison import (ReadState, comparison_group, comparison_sort_key,
                         sort_comparison_rows)
from .library import TOLINO_IGNORE_COLUMN, selected_table_rows
from .tolino import report_error


class InventoryDialog(QDialog):
    """Bestandsvergleich: pro Buch drei Aktionen zum Auswählen.

    Links stehen **Upload** (Datei), **Metadaten** (Titel, Autor und ISBN)
    oder **Lesestatus**. Die Spalten **In Calibre** und **In tolino** zeigen
    den Bestand; **Hinweis** erklärt die Zeile. Technische Angaben und
    Sammelaktionen sind getrennt erreichbar.
    """

    HEADERS = ("Datei hochladen", "Metadaten aktualisieren", "Gelesen",
               "In Calibre", "In tolino", "Buchtitel", "Autor(en)",
               "Hinweis", "ISBN", "tolino-ID", "UUID")
    # Technische Spalten stehen ganz rechts und sind standardmässig aus.
    TECHNICAL_COLUMNS = (8, 9, 10)
    GROUP_FILTERS = (
        ("Alle", None),
        ("Aktion nötig", "action"),
        ("Nur in tolino", "tolino_only"),
        ("Zu prüfen", "review"),
        ("Unverändert", "unchanged"),
        ("Ignoriert", "ignored"),
    )

    # Ein Klick ändert die Auswahl. Die Zusammenfassung zählt die Änderungen.
    READ_TOOLTIP = "Ausgewählt = gelesen, nicht ausgewählt = ungelesen."
    INTRO_TEXT = ("Wähle pro Buch: Upload (Datei), Metadaten "
                  "(Titel, Autor, ISBN) oder Lesestatus. Geändert wird erst "
                  "mit «Synchronisierung starten».")

    # Aktionen für markierte Zeilen, zusammen an einer Stelle.
    ROW_ACTIONS = (
        ("Upload aktivieren", "upload_on"),
        ("Upload deaktivieren", "upload_off"),
        ("Metadaten aktivieren", "metadata_on"),
        ("Metadaten deaktivieren", "metadata_off"),
        ("Als gelesen markieren", "read"),
        ("Als ungelesen markieren", "read_off"),
        # Download arbeitet nur für eine markierte Cloud-Kopie.
        ("Aus tolino herunterladen …", "download"),
        ("Aus der tolino Cloud löschen …", "cloud_delete"),
        ("Löschmarkierung aufheben", "cloud_delete_off"),
    )

    # Menüschlüssel -> (Methode, feste Argumente). Die Reihenfolge im Menü
    # steht in ROW_ACTIONS; hier steht nur noch, was ausgeführt wird.
    ROW_ACTION_HANDLERS = {
        "read": ("mark_read_for_marked_rows", ()),
        "read_off": ("clear_read_for_marked_rows", ()),
        "upload_on": ("set_column_for_marked_rows",
                      ("upload", True,
                       "haben schon diese Auswahl oder keine Datei")),
        "upload_off": ("set_column_for_marked_rows",
                       ("upload", False,
                        "waren schon abgewählt oder haben keine Datei")),
        "metadata_on": ("set_column_for_marked_rows",
                        ("metadata", True,
                         "haben schon diese Auswahl oder liegen nicht in "
                         "der Cloud")),
        "metadata_off": ("set_column_for_marked_rows",
                         ("metadata", False,
                          "waren schon abgewählt oder liegen nicht in "
                          "der Cloud")),
        "download": ("download_selected", ()),
        "cloud_delete": ("delete_marked_from_cloud", ()),
        "cloud_delete_off": ("clear_cloud_delete_for_marked_rows", ()),
    }

    _SORT_KEYS = (
        # 0 Hochladen / 1 Metadaten: Ausgewähltes zuerst, dann Titel/Autor(en)
        comparison_sort_key,
        lambda row: ((0 if row.get("metadata") else 1,)
                     + comparison_sort_key(row)[2:]),
        # 2 Lesestatus: gelesene Bücher zuerst
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
        # 8 ISBN / 9 tolino-ID / 10 UUID
        lambda row: (str(row.get("isbn") or ""),
                     str(row.get("title") or "").casefold()),
        lambda row: (str(row.get("tolino_id") or ""),
                     str(row.get("title") or "").casefold()),
        lambda row: (str(row.get("uuid") or ""),
                     str(row.get("title") or "").casefold()),
    )

    @staticmethod
    def default_actions(row):
        """Vorschläge für Upload und Metadaten dieser Zeile.

        Unsichere Zuordnungen bleiben unverändert, da ein Upload Duplikate
        erzeugen könnte. Bücher ohne passendes Upload-Format erhalten keinen
        Upload-Vorschlag.
        """
        # Ein ignoriertes Buch wird nie vorgeschlagen -- weder Upload noch
        # Metadaten.
        if row.get("ignored"):
            return (False, False)
        # Bei unsicheren Zuordnungen könnte ein Upload Duplikate erzeugen.
        if row.get("status") == "review":
            return (False, False)
        if row.get("book_id") is None or not row.get("tolino_id"):
            new_book = row.get("book_id") is not None
            if row.get("stale_cloud_id"):
                return (False, False)
            return (new_book and row.get("has_preferred_format", True), False)
        if row.get("status") == "new_in_calibre":
            return (True, False)
        if row.get("status") == "changed":
            return (False, True) if not row.get("file_changed") else (True, False)
        return (False, False)

    def __init__(self, rows, parent=None, client_factory=None, options=None):
        QDialog.__init__(self, parent)
        self.client_factory = client_factory
        # Eine Anmeldung fuer das ganze Fenster: jede Aktion loggte sonst neu
        # ein und rotierte dabei den Refresh-Token.
        self._cached_client = None
        self.options = dict(options or {})
        self._technical_visible = False
        self.rows = []
        self._prepare_rows(rows)
        self.setWindowTitle("Vergleich")
        # Schmal genug fuer 1366x768 bei 100-125 % Skalierung; die breiten
        # Textspalten wachsen mit (siehe _size_columns).
        self.setMinimumSize(640, 460)
        resize = getattr(self, "resize", None)
        if resize is not None:
            resize(960, 560)
        layout = QVBoxLayout(self)
        self._build_header(layout)
        self._build_toolbar(layout)
        self._build_table(layout)
        self._build_footer(layout)

    def _prepare_rows(self, rows):
        """Zeilen normalisieren, Standardaktionen setzen und sortieren."""
        for row in rows:
            row = dict(row)
            # Auch von Hand gebaute Zeilen (Tests, künftige Aufrufer) haben
            # eine Gruppe; sonst würde der Gruppenfilter sie verstecken.
            row.setdefault("group", comparison_group(row.get("status")))
            if "upload" not in row or "metadata" not in row:
                row["upload"], row["metadata"] = self.default_actions(row)
            # Lesestatus aus der Cloud: eine Auswahl ändert ihn erst mit
            # «Synchronisierung starten».
            row["read"] = ReadState.from_row(row)
            # Die Calibre-Spalte wird nur angezeigt. Die Auswahl folgt dem
            # Lesestatus des aktuell ausgewählten Tolino-Kontos.
            self.rows.append(row)
        # Ausgewählte Aktionen zuerst, dann Titel/Autor(en).
        self.rows = sort_comparison_rows(self.rows)

    def _build_header(self, layout):
        """Einleitung, Zusammenfassung und Hinweis zu Automatiken."""
        intro = QLabel(self.INTRO_TEXT)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.note = QLabel(self._automatic_note())
        self.note.setWordWrap(True)
        if not self.note.text() and any(
                not ReadState.from_row(row).known for row in self.rows):
            self.note.setText(
                "Lesestatus unbekannt: konnte nicht geladen werden und "
                "wird nicht verändert.")
        if not self.note.text() and self.options.get("ignore_column") is False:
            self.note.setText(
                "Spalte %s (Ja/Nein) anlegen: so markierte Bücher werden "
                "nicht hochgeladen." % TOLINO_IGNORE_COLUMN)
        layout.addWidget(self.note)

    def _build_toolbar(self, layout):
        """Gruppenfilter, Empfehlung/Auswahl und Zeilenaktionen."""
        helpers = QHBoxLayout()
        self.group_filter = QComboBox()
        for label, key in self.GROUP_FILTERS:
            self.group_filter.addItem(label, key)
        self.group_filter.setToolTip(
            "Filtert nach Aktion, Cloud-Bestand, Prüfung oder unverändert.")
        self.group_filter.currentIndexChanged.connect(
            self._apply_group_filter)
        self.recommend = QPushButton("Empfohlene Aktionen auswählen")
        self.recommend.setToolTip(
            "Wählt Upload oder Metadaten für eindeutig zugeordnete Bücher.")
        self.clear_selection = QPushButton("Auswahl zurücksetzen")
        self.clear_selection.setToolTip(
            "Setzt alle Auswahlen zurück. Nichts wird verändert.")
        helpers.addWidget(QLabel("Anzeigen"))
        helpers.addWidget(self.group_filter)
        helpers.addWidget(self.recommend)
        helpers.addWidget(self.clear_selection)
        helpers.addStretch(1)
        self.row_actions = QPushButton("Aktionen für markierte Bücher …")
        self.row_actions.setToolTip(
            "Zeilen mit Strg-/Shift-Klick markieren; alle mit Strg+A.")
        helpers.addWidget(self.row_actions)
        layout.addLayout(helpers)

    def _build_table(self, layout):
        """Tabelle samt Kopfzeilen-Tooltips und Sortierung aufbauen."""
        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        # Strg-/Shift-Klick und Strg+A werden in Tooltips und Menue
        # versprochen -- ohne ExtendedSelection waere das falsch.
        selection_mode = getattr(QTableWidget, "ExtendedSelection", None)
        if selection_mode is not None:
            self.table.setSelectionMode(selection_mode)
        header_tooltips = (
            "Buchdatei in die Cloud hochladen.",
            "Titel, Autor und ISBN ohne Upload aktualisieren.",
            "Auswahl = gelesen; keine Auswahl = ungelesen. Die Calibre-Spalte "
            "#tolino_read ist nur informativ.",
            "Buch in Calibre vorhanden.",
            "Buch im ausgewählten tolino-Konto vorhanden.",
            "Buchtitel", "Autor(en)",
            "Grund und Unterschiede des Vergleichs.",
            "ISBN (technisch, ausgeblendet)",
            "tolino-ID der Cloud-Kopie (technisch, ausgeblendet)",
            "Calibre-UUID (technisch, ausgeblendet)",
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
        table_header.setSortIndicatorShown(True)
        table_header.setSortIndicator(0, Qt.AscendingOrder)
        table_header.sectionClicked.connect(self._sort_by_column)
        self._rebuild_table(self.rows)
        layout.addWidget(self.table)

    def _build_footer(self, layout):
        """Rueckmeldung, technische Spalten und die Abschlussknoepfe."""
        # Bilanz einer Zeilen-Aktion – nicht modal, direkt unter der Tabelle.
        self.action_feedback = QLabel()
        self.action_feedback.setWordWrap(True)
        layout.addWidget(self.action_feedback)

        self.technical_toggle = QCheckBox(
            "Technische Details anzeigen (ISBN, tolino-ID, UUID)")
        self.technical_toggle.setToolTip(
            "Zeigt ISBN, tolino-ID und UUID.")
        self.technical_toggle.toggled.connect(self._toggle_technical_columns)
        layout.addWidget(self.technical_toggle)
        self._apply_technical_visibility()

        buttons = QHBoxLayout()
        confirm = QPushButton("Synchronisierung starten")
        confirm.setToolTip(
            "Überträgt die ausgewählten Änderungen.")
        cancel = QPushButton("Abbrechen")
        cancel.setToolTip(
            "Schließt den Vergleich, ohne Änderungen.")
        confirm.clicked.connect(self.accept)
        cancel.clicked.connect(self.reject)
        self.confirm = confirm
        self.cancel = cancel
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
        """Anzahl der automatisch geplanten Sammlungen anzeigen."""
        count = int(self.options.get("automatic") or 0)
        if self.options.get("collections") and count:
            return "Serien werden als Sammlungen in tolino übernommen (%d offen)." % count
        return ""

    def _toggle_technical_columns(self, visible):
        self._technical_visible = bool(visible)
        self._apply_technical_visibility()

    def _apply_technical_visibility(self):
        for column in self.TECHNICAL_COLUMNS:
            self.table.setColumnHidden(column, not self._technical_visible)

    def _read_state(self, row):
        """Lesestatus der Zeile; normalisiert auch von Hand gebaute Zeilen."""
        state = ReadState.from_row(row)
        row["read"] = state
        return state

    def _choice_changed(self, index, key):
        """Die Zeile hält die Auswahl; die Checkbox zeigt sie nur an."""
        if 0 <= index < len(self.rows):
            row = self.rows[index]
            checked = bool(
                (self.checks if key == "upload" else
                 self.meta_checks if key == "metadata" else
                 self.read_checks)[index].isChecked())
            if key == "read":
                self._read_state(row).chosen = checked
            else:
                row[key] = checked
            self._update_summary()

    @staticmethod
    def _set_checked_quietly(check_widget, value):
        """Auswahlkästchen ohne Signal setzen.

        Sammelaktionen setzten bisher jedes Kästchen einzeln und jede
        Änderung rief `_update_summary()` -- bei einigen hundert Büchern
        quadratisch. Die Signale bleiben hier aus; die Zusammenfassung
        schreibt der Aufrufer genau einmal.
        """
        blocker = getattr(check_widget, "blockSignals", None)
        if callable(blocker):
            blocker(True)
        check_widget.setChecked(value)
        if callable(blocker):
            blocker(False)

    def _set_selection(self, chooser, clear_deletions=False):
        """Auswahl in einem Schritt setzen (Zeilen und Widgets synchron).

        Der Lesestatus bleibt bei Empfehlungen unverändert. Zum Löschen
        markierte Zeilen bleiben unberührt; «Auswahl zurücksetzen» hebt auch
        die Löschmarkierung auf.
        """
        if clear_deletions:
            for index, row in enumerate(self.rows):
                row.pop("delete_cloud", None)
                row["upload"] = row["metadata"] = False
                state = self._read_state(row)
                state.chosen = state.cloud
                for check in (self.checks[index], self.meta_checks[index]):
                    self._set_checked_quietly(check, False)
            if getattr(self, "table", None) is not None:
                self._rebuild_table(self.rows)
            else:
                self._update_summary()
            return
        for index, row in enumerate(self.rows):
            upload_check = self.checks[index]
            metadata_check = self.meta_checks[index]
            if row.get("delete_cloud") or row.get("ignored"):
                continue
            wanted = tuple(chooser(row))
            upload, metadata = bool(wanted[0]), bool(wanted[1])
            row.update(upload=upload, metadata=metadata)
            for check, checked in ((upload_check, upload),
                                   (metadata_check, metadata)):
                self._set_checked_quietly(check, checked)
        self._update_summary()

    def apply_recommended(self):
        """Die vorgeschlagenen Aktionen für alle Zeilen auswählen."""
        self._set_selection(self.default_actions)

    def clear_all_selection(self):
        self._set_selection(lambda _row: (False, False),
                            clear_deletions=True)

    def _undecided_count(self):
        """Zeilen, die eine Entscheidung brauchen (Gruppe «Zu prüfen»)."""
        groups = ("review", "duplicate_tolino", "not_matchable")
        # Eine bereits zugeordnete Cloud-Zeile ist entschieden: sie zaehlt
        # nicht mehr als offene Frage.
        return sum(1 for row in self.rows if row.get("group") in groups)

    def selected_work_count(self):
        """Zahl der Aktionen, die die aktuelle Auswahl auslösen würde.

        Genau die Zahl, die «Synchronisierung starten (n)» nennt -- und die
        gleiche Grundlage für die Frage, ob ein Start überhaupt etwas tut.
        """
        return (len(self.selected_upload_ids())
                + len(self.selected_metadata_ids())
                + len(self.selected_read_changes())
                + len(self.selected_cloud_deletions()))

    def automatic_actions_enabled(self):
        """Tun Automatiken ohne Auswahl WIRKLICH etwas?

        Eine eingeschaltete Option ist noch keine Aktion: nur der explizite
        Plan-Zähler beschreibt offene Sammlungszuordnungen. Fehlt er, wird
        keine unbelegte automatische Arbeit angenommen.
        """
        try:
            return int(self.options.get("automatic") or 0) > 0
        except (TypeError, ValueError):
            return False

    def accept(self):
        """«Synchronisierung starten» -- nie stillschweigend ohne Aktion.

        Feldbefund: Der Vergleich schloss sich, ohne dass etwas
        übertragen wurde, und sagte nichts dazu. Ohne ausgewählte Aktion und
        ohne Automatiken bleibt das Fenster jetzt offen und erklärt es.
        """
        if not self.selected_work_count() and not self.automatic_actions_enabled():
            text = ("Wähle mindestens eine Aktion oder nutze "
                    "«Empfohlene Aktionen auswählen».")
            undecided = self._undecided_count()
            if undecided:
                text += " %d %s «Zu prüfen»: nicht vorausgewählt." % (
                    undecided, plural(undecided, "Buch", "Bücher"))
            QMessageBox.information(
                self, "Nichts ausgewählt", text)
            return
        QDialog.accept(self)

    def selected_cloud_deletions(self):
        """(UUID, tolino-ID, Titel) der zum Löschen markierten Cloud-Kopien."""
        return [(str(row.get("uuid") or ""), str(row["tolino_id"]),
                 str(row.get("title") or ""))
                for row in self.rows
                if row.get("delete_cloud") and row.get("tolino_id")
                and not row.get("ignored")]

    def selected_upload_ids(self):
        """Bücher, deren Datei hochgeladen werden soll."""
        return {row["book_id"] for row in self.rows
                if row.get("upload") and not row.get("ignored")
                and row.get("book_id") is not None}

    def selected_metadata_ids(self):
        """Bücher, deren Cloud-Metadaten aktualisiert werden sollen."""
        return {row["book_id"] for row in self.rows
                if row.get("metadata") and not row.get("ignored")
                and row.get("book_id") is not None}

    def selected_read_changes(self):
        """(tolino-ID, Calibre-Nummer, gelesen?) der offenen Änderungen.

        Die Zeile kennt Cloud-Stand und Auswahl; übertragen wird nur die
        Abweichung. Die Calibre-Nummer wandert mit, damit der bestätigte
        Stand ohne zweite Suche in die Spalte #tolino_read wandert.
        """
        changes = []
        for row in self.rows:
            tolino_id = row.get("tolino_id")
            state = self._read_state(row)
            if (not tolino_id or row.get("ignored") or row.get("delete_cloud")
                    or not state.changed):
                continue
            changes.append((str(tolino_id), row.get("book_id"), state.chosen))
        return changes

    def _marked_row_indexes(self):
        """Markierte Zeilen -- oder ein Hinweis, wenn keine markiert ist."""
        marked = selected_table_rows(self.table)
        if not marked:
            QMessageBox.information(
                self, "Aktionen für markierte Bücher",
                "Markiere zuerst mindestens ein Buch in der Tabelle. "
                "Mehrere Bücher wählst du mit Strg-/Shift-Klick aus.")
        return marked

    def choose_row_action(self):
        """Eine Stelle für alle Aktionen auf markierte Zeilen.

        Aktionen für markierte Bücher sind hier zusammengefasst; auch Upload,
        Metadaten und Lesestatus sind verfügbar.
        """
        marked = self._marked_row_indexes()
        if not marked:
            return
        menu = QMenu(self)
        actions = {}
        row_indexes = [index for index in marked
                       if 0 <= index < len(self.rows)]
        predicates = {
            "upload_on": lambda row: (
                row.get("book_id") is not None and not row.get("upload")
                and row.get("has_preferred_format", True)),
            "upload_off": lambda row: bool(row.get("upload")),
            "metadata_on": lambda row: (
                row.get("book_id") is not None and row.get("tolino_id")
                and not row.get("metadata")),
            "metadata_off": lambda row: bool(row.get("metadata")),
            "read": lambda row: row.get("tolino_id") and not row.get("read"),
            "read_off": lambda row: row.get("tolino_id") and row.get("read"),
            "download": lambda row: bool(row.get("tolino_id")),
            "cloud_delete": lambda row: bool(row.get("tolino_id")),
            "cloud_delete_off": lambda row: bool(row.get("delete_cloud")),
        }
        for label, key in self.ROW_ACTIONS:
            if not any(predicates[key](self.rows[index])
                       for index in row_indexes):
                continue
            actions[menu.addAction(label)] = key
        position = self.row_actions.mapToGlobal(
            self.row_actions.rect().bottomLeft())
        execute = getattr(menu, "exec", None) or menu.exec_
        chosen = execute(position)
        key = actions.get(chosen)
        handler = self.ROW_ACTION_HANDLERS.get(key)
        if handler is None:
            return
        method, args = handler
        getattr(self, method)(*args)

    def mark_read_for_marked_rows(self):
        """Lesestatus für alle markierten Zeilen setzen.

        Mehrfachauswahl und Sammelaktion erlauben, viele Bücher gemeinsam
        auszuwählen.
        """
        self.set_column_for_marked_rows(
            "read", True,
            "sind schon gelesen oder liegen nicht in der Cloud")

    def clear_read_for_marked_rows(self):
        """Lesestatus markierter Cloud-Bücher auf ungelesen setzen."""
        self.set_column_for_marked_rows(
            "read", False,
            "sind bereits ungelesen oder liegen nicht in der Cloud")

    def set_column_for_marked_rows(self, key, value, summary):
        """Eine Spalte für alle markierten Zeilen setzen.

        Dieselbe Wirkung wie die Auswahl je Zeile, nur für viele auf einmal:
        passt die Spalte nicht zur Zeile, wird sie gezählt und ausgelassen.
        Geschrieben wird weiterhin erst mit «Synchronisierung starten».
        """
        marked = self._marked_row_indexes()
        if not marked:
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
            self._show_action_feedback("")
            count = skipped or len(marked)
            QMessageBox.information(
                self, "Nichts geändert",
                "Nichts geändert: %d markierte %s %s."
                % (count, plural(count, "Buch", "Bücher"), summary))
            return
        feedback = "%s für %d von %d markierten Büchern" % (
            self._action_label(key, value), changed, len(marked))
        if skipped:
            feedback += "; %d übersprungen" % skipped
        self._show_action_feedback(feedback + ".")

    ACTION_LABELS = {
        ("upload", True): "Upload aktiviert",
        ("upload", False): "Upload deaktiviert",
        ("metadata", True): "Metadaten aktiviert",
        ("metadata", False): "Metadaten deaktiviert",
        ("read", True): "Als gelesen markiert",
        ("read", False): "Als ungelesen markiert",
    }

    @classmethod
    def _action_label(cls, key, value):
        """Klartext der Spalten-Aktion für die Rückmeldung."""
        return cls.ACTION_LABELS.get((key, bool(value)), "Aktion ausgeführt")

    def _show_action_feedback(self, text):
        """Bilanz unter der Tabelle setzen; leerer Text entfernt sie wieder.

        Bewusst nicht modal: eine erfolgreiche Auswahlaktion soll den
        Ablauf nicht mit einem weiteren Dialog anhalten.
        """
        label = getattr(self, "action_feedback", None)
        if label is None:
            return
        label.setText(text)

    @staticmethod
    def _column_applies(key, row):
        """Geht diese Spalte bei dieser Zeile? (wie beim einzelnen Klick)"""
        if row.get("delete_cloud") or row.get("ignored"):
            return False
        if key == "upload":
            return row.get("book_id") is not None
        if key == "metadata":
            return row.get("book_id") is not None and bool(row.get("tolino_id"))
        if key == "read":
            return bool(row.get("tolino_id"))
        return False

    def _set_row_choice(self, index, key, value):
        """Zeile und Auswahlkästchen setzen; False = war schon so.

        Die Auswahl folgt dem Cloud-Lesestatus; übertragen wird sie beim Sync.
        """
        row = self.rows[index]
        if row.get("delete_cloud") or row.get("ignored"):
            return False
        if key == "read":
            check = self.read_checks[index]
            state = self._read_state(row)
            if not state.known or state.chosen == bool(value):
                return False
            state.chosen = bool(value)
        else:
            check = (self.checks if key == "upload" else self.meta_checks)[index]
            if bool(row.get(key)) == bool(value):
                return False
            row[key] = bool(value)
        self._set_checked_quietly(check, bool(value))
        return True

    def _sort_by_column(self, column):
        """Spaltenkopf anklicken: aufsteigend <-> absteigend umschalten."""
        if not 0 <= column < len(self._SORT_KEYS):
            return
        # Die Zeilen sind die einzige Quelle der Auswahl; Checkboxen werden
        # beim Neuaufbau aus ihnen erzeugt.
        if self._sort_column == column:
            self._sort_descending = not self._sort_descending
        else:
            self._sort_column = column
            self._sort_descending = False
        ordered = sorted(self.rows, key=self._SORT_KEYS[column],
                         reverse=self._sort_descending)
        self.table.horizontalHeader().setSortIndicator(
            column,
            Qt.DescendingOrder if self._sort_descending
            else Qt.AscendingOrder)
        self._rebuild_table(ordered)

    def _action_check_specs(self, row):
        """Return enabled state and tooltips for this row's action checks."""
        has_book = row.get("book_id") is not None
        in_cloud = bool(row.get("tolino_id"))
        delete_cloud = bool(row.get("delete_cloud"))
        read_known = self._read_state(row).known
        ignored = bool(row.get("ignored"))
        delete_tooltip = (
            "Zum Löschen aus tolino markiert – zuerst "
            "«Löschmarkierung aufheben».")
        ignored_tooltip = ("In Calibre ignoriert (%s) – wird nicht "
                           "abgeglichen." % TOLINO_IGNORE_COLUMN)
        return (
            ("upload", has_book and not delete_cloud and not ignored
             and row.get("has_preferred_format", True),
             "Buchdatei in die Cloud hochladen.",
             ("Nur in tolino – es gibt keine Calibre-Datei zum Hochladen."
              if not has_book else
              "Kein Buchformat aus den Upload-Formaten vorhanden.")),
            ("metadata", has_book and in_cloud and not delete_cloud
             and not ignored,
             "Titel, Autor und ISBN in der Cloud aktualisieren, ohne "
             "Upload.",
             "Nur in Calibre – es gibt in tolino nichts zu "
             "aktualisieren." if has_book else
             "Kein Calibre-Buch – nichts zu aktualisieren."),
            ("read", in_cloud and read_known and not delete_cloud
             and not ignored,
             self.READ_TOOLTIP + " Im ausgewählten tolino-Konto änderbar.",
             "Lesestatus unbekannt – nicht geladen." if in_cloud else
             "Geht erst, wenn das Buch in tolino liegt."),
        ), delete_cloud, ignored, delete_tooltip, ignored_tooltip

    def _build_action_checks(self, row_index, row):
        """Create the three selection cells for one book row."""
        checks, delete_cloud, ignored, delete_tooltip, ignored_tooltip = \
            self._action_check_specs(row)
        for column, (key, enabled, tooltip, disabled_tooltip) in enumerate(checks):
            check = QCheckBox()
            check.setChecked(bool(row.get(key)) and not delete_cloud)
            check.setEnabled(enabled)
            if (key == "upload" and row.get("match_reason") in
                    ("title_only", "ambiguous")):
                tooltip = "Kann ein Duplikat erzeugen."
            check.setToolTip(
                ignored_tooltip if ignored
                else tooltip if enabled
                else (delete_tooltip if delete_cloud else disabled_tooltip))
            state_signal = getattr(check, "stateChanged", None)
            state_signal.connect(
                lambda _state, i=row_index, k=key:
                self._choice_changed(i, k))
            (self.checks, self.meta_checks,
             self.read_checks)[column].append(check)
            self.table.setCellWidget(row_index, column, check)

    def _row_display_values(self, row, has_book, in_cloud):
        """Render explanation and the non-action cells for one comparison row."""
        explanation = str(row.get("explanation", "") or "")
        if in_cloud and not ReadState.from_row(row).known:
            explanation = (explanation + " Lesestatus unbekannt.").strip()
        elif in_cloud and has_book and row.get("read_column"):
            note = self._read_note(row)
            if note:
                explanation = (explanation + " " + note).strip()
        if row.get("delete_cloud"):
            explanation = (explanation + " Wird beim Start aus tolino gelöscht.").strip()
        return (
            "✓" if has_book else "",
            "✓" if in_cloud else "",
            row.get("title", ""), row.get("authors", ""), explanation,
            row.get("isbn", ""), row.get("tolino_id", ""), row.get("uuid", ""),
        )

    def _rebuild_table(self, rows):
        """Tabelle aus den (ggf. sortierten) Zeilen neu aufbauen."""
        self.table.setUpdatesEnabled(False)
        # Zeilen sind die einzige Quelle -- auch wenn ein Aufrufer oder Test
        # sie als rohes Dict übergibt.
        self.rows = [dict(row, read=ReadState.from_row(row)) for row in rows]
        self.checks = []
        self.meta_checks = []
        self.read_checks = []
        self.table.setRowCount(0)
        self.table.setRowCount(len(self.rows))
        for row_index, row in enumerate(self.rows):
            has_book = row.get("book_id") is not None
            in_cloud = bool(row.get("tolino_id"))
            self._build_action_checks(row_index, row)
            values = self._row_display_values(row, has_book, in_cloud)
            for column, value in enumerate(values, 3):
                self.table.setItem(
                    row_index, column, QTableWidgetItem(str(value or "")))
        self.table.resizeColumnsToContents()
        self._size_columns()
        self._apply_technical_visibility()
        self._apply_group_filter()
        self.table.setUpdatesEnabled(True)
        self._update_summary()

    @staticmethod
    def _read_note(row):
        """Informative Calibre-Spaltenanzeige ohne Einfluss auf Auswahl."""
        local = ReadState.from_row(row).local
        if local is None:
            return "Calibre-Lesestatus: leer."
        return "Calibre-Lesestatus: %s." % (
            "gelesen" if local else "ungelesen")

    def _size_columns(self):
        """Schmale Standardansicht: Textspalten wachsen, Rest bleibt kompakt.

        Ohne das erzwingen Titel und Hinweis eine sehr breite Tabelle und
        damit eine Mindestbreite weit über einem 1366x768-Fenster.
        """
        header = self.table.horizontalHeader()
        for column in (5, 7):
            header.setSectionResizeMode(column, header_mode("Stretch"))
        for column in (0, 1, 2, 3, 4, 6):
            header.setSectionResizeMode(column, header_mode("ResizeToContents"))

    def _apply_group_filter(self, *_args):
        """Gruppenfilter ohne die Zeilen/Wahlzustaende zu verlieren.

        Es werden nur Tabellenzeilen versteckt; `self.rows` bleibt die
        vollstaendige Liste, damit Auswahl und Sortierung unberuehrt bleiben.
        """
        combo = getattr(self, "group_filter", None)
        selected = combo.currentData() if combo is not None else None
        for index, row in enumerate(getattr(self, "rows", ())):
            visible = selected is None or row.get("group") == selected
            self.table.setRowHidden(index, not visible)

    def _update_summary(self, *_args):
        """Kurzzeile: wie viele Aktionen gerade ausgewählt sind."""
        uploads = len(self.selected_upload_ids())
        metadata = len(self.selected_metadata_ids())
        read_changes = self.selected_read_changes()
        marked_read = sum(1 for _tolino, _book, finished in read_changes
                          if finished)
        marked_unread = len(read_changes) - marked_read
        deletions = len(self.selected_cloud_deletions())
        changes = marked_read + marked_unread
        summary = getattr(self, "summary", None)
        if summary is not None:
            summary_text = (
                "Ausgewählt: %d %s · %d Metadaten · %d Lesestatus"
                % (uploads, plural(uploads, "Upload", "Uploads"),
                   metadata, changes))
            if changes:
                summary_text += (
                    " (davon %d gelesen, %d ungelesen)"
                    % (marked_read, marked_unread))
            if deletions:
                summary_text += " · %d %s" % (
                    deletions, plural(deletions, "Löschung", "Löschungen"))
            undecided = self._undecided_count()
            if undecided:
                summary_text += (
                    " · %d %s unklar («Zu prüfen», nicht vorausgewählt)"
                    % (undecided, plural(undecided, "Buch", "Bücher")))
            summary.setText(summary_text)
        confirm = getattr(self, "confirm", None)
        if confirm is not None:
            total = uploads + metadata + changes + deletions
            confirm.setText("Synchronisierung starten (%d)" % total
                            if total else "Synchronisierung starten")
            confirm.setEnabled(total > 0 or self.automatic_actions_enabled())

    # --- Cloud actions on the currently selected table row ---------------

    def _selected_row(self):
        """The comparison row for the selection, or None with a hint.

        Download und Sammlungen arbeiten mit genau einem Buch; mehrere
        markierte Zeilen werden deshalb abgelehnt statt still nur die erste
        zu verarbeiten.
        """
        selected = selected_table_rows(self.table)
        if not selected:
            QMessageBox.information(
                self, "Buch auswählen",
                "Markiere zuerst das Buch, für das du diese Aktion "
                "ausführen möchtest.")
            return None
        if len(selected) > 1:
            QMessageBox.information(
                self, "Ein Buch auswählen",
                "Diese Aktion gilt nur für ein Buch. Markiere genau ein "
                "Buch in der Tabelle.")
            return None
        row_index = selected[0]
        if row_index >= len(self.rows):
            return None
        return self.rows[row_index]

    def _drop_client(self):
        """Nach einem Fehler neu anmelden lassen.

        Ein Client, dessen Aufruf gescheitert ist, kann einen toten Token
        oder eine abgelaufene Sitzung halten; ihn weiterzureichen würde den
        Fehler auf alle folgenden Aktionen übertragen.
        """
        self._cached_client = None

    def _cached_or_new_client(self):
        """Client holen -- im Worker-Thread, ohne Widget-Zugriff.

        Fehler werden als Exception weitergereicht; Meldungsfenster und
        Zustand bleiben Sache des GUI-Threads.
        """
        if self._cached_client is not None:
            return self._cached_client
        if self.client_factory is None:
            raise RuntimeError("Keine Anmeldung verfügbar.")
        client = self.client_factory()
        if client is not None:
            self._cached_client = client
        return client

    def _start_action(self, call, on_done, on_failed, text=""):
        """Netzaufruf auslagern; genau eine Aktion zur Zeit.

        Waehrenddessen sind Tabelle und Startknopf gesperrt, die Statuszeile
        nennt den Schritt -- kein modales Fenster, kein eingefrorenes Calibre.
        """
        if getattr(self, "_call_thread", None) is not None:
            return
        self._set_action_busy(True, text)
        start_call(self, call, on_done, on_failed)

    def _end_action(self):
        self._call_thread = None
        self._set_action_busy(False)

    def _set_action_busy(self, busy, text=""):
        _set_enabled(self, ("table", "confirm"), not busy)
        summary = getattr(self, "summary", None)
        if summary is None:
            return
        if busy:
            summary.setText(text or "Verbindung zu tolino …")
        else:
            self._update_summary()

    def download_selected(self):
        row = self._selected_row()
        if row is None:
            return
        tolino_id = row.get("tolino_id")
        if not tolino_id:
            QMessageBox.information(
                self, "Buch nicht in tolino vorhanden",
                "Dieses Buch kann nicht aus tolino heruntergeladen werden, "
                "weil es im ausgewählten Konto nicht vorhanden ist.")
            return
        # Erst den Speicherort erfragen, dann laden: ein Abbruch im
        # Dateidialog soll keinen Download ausloesen.
        title = str(row.get("title") or "tolino-book")
        title = re.sub(r'[\\/:*?"<>|]', "_", title).strip(" .") or "tolino-book"
        extension = ".epub"
        suggested = title + extension
        path, _ = QFileDialog.getSaveFileName(
            self, "Buch speichern", suggested,
            "EPUB (*.epub);;PDF (*.pdf)")
        if not path:
            return
        self._download_path = path
        self._start_action(
            lambda: self._download_content(tolino_id),
            self._download_finished,
            self._download_failed,
            "Buchdatei wird aus tolino geladen …")

    def _download_content(self, tolino_id):
        """Buchdatei ohne Zusatzdateien im Hintergrund laden."""
        client = self._cached_or_new_client()
        if client is None:
            return None
        return client.download(tolino_id)

    def _download_finished(self, result):
        path = self._download_path
        self._end_action()
        if result is None:
            return
        if result[:4] == b"%PDF" and path.casefold().endswith(".epub"):
            path = path[:-5] + ".pdf"
        try:
            with open(path, "wb") as handle:
                handle.write(result)
        except OSError as exc:
            message = report_error(exc, "Speichern")
            error_dialog(self, "Speichern fehlgeschlagen",
                         message, show=True)
            return
        info_dialog(self, "Buch gespeichert", "Gespeichert unter: %s" % path,
                    show=True, show_copy_button=False)

    def _action_failed(self, title, summary, exc, context):
        """Nach einem gescheiterten Netzaufruf: aufräumen, neu anmelden, melden."""
        self._end_action()
        self._drop_client()
        error_dialog(self, title,
                     summary + "\n\n" + report_error(exc, context), show=True)

    def _download_failed(self, exc):
        self._action_failed(
            "Cloud-Download fehlgeschlagen",
            "Der Download ist fehlgeschlagen. Details stehen im Calibre-Log.",
            exc, "Download")

    def delete_marked_from_cloud(self):
        """Markierte Cloud-Kopien zum Löschen vormerken.

        Gelöscht wird erst mit «Synchronisierung starten»; dort werden die
        konkreten Titel noch einmal bestätigt. Die Calibre-Bücher und ihre
        Dateien bleiben unangetastet.
        """
        marked = self._marked_row_indexes()
        if not marked:
            return
        candidates = []
        for index in marked:
            if not 0 <= index < len(self.rows):
                continue
            row = self.rows[index]
            if not row.get("tolino_id") or row.get("delete_cloud"):
                continue
            candidates.append(row)
        if not candidates:
            QMessageBox.information(
                self, "Nichts zu löschen",
                "Es wurde nichts geändert: Die markierten Bücher liegen nicht "
                "in tolino oder sind bereits zum Löschen markiert.")
            return
        # Die endgültige Löschliste wird einmalig vor dem Sync bestätigt.
        for row in candidates:
            row["delete_cloud"] = True
            row["upload"] = False
            row["metadata"] = False
        self._rebuild_table(self.rows)

    def clear_cloud_delete_for_marked_rows(self):
        """Löschmarkierung der markierten Zeilen wieder aufheben."""
        marked = self._marked_row_indexes()
        if not marked:
            return
        changed = 0
        for index in marked:
            if 0 <= index < len(self.rows):
                if self.rows[index].pop("delete_cloud", None) is not None:
                    changed += 1
        if not changed:
            QMessageBox.information(
                self, "Löschmarkierung aufheben",
                "Es wurde nichts geändert: Die markierten Bücher sind nicht "
                "zum Löschen vorgemerkt.")
            return
        self._rebuild_table(self.rows)

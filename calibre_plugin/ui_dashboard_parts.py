"""Aufbau und Bestätigungen des Sync-Dashboards."""
from calibre.gui2 import error_dialog, info_dialog
from .identifiers import display_title
from .library import COLUMN_KIND_NAMES, CUSTOM_COLUMNS, create_custom_columns
from .partners import PARTNERS
from .tolino import log_warning, sanitize_error
from qt.core import (QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout,
                     QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton,
                     QVBoxLayout)
from .ui_common import (COMPARE_BUTTON_TEXT, COMPARE_BUTTON_TIP,
                        LOGIN_BUTTON_TEXT, LOGIN_BUTTON_TIP, gui_guard)


def _button(text, tip="", slot=None):
    button = QPushButton(text)
    if tip:
        button.setToolTip(tip)
    if slot is not None:
        button.clicked.connect(slot)
    return button


def _combo(items=(), tip="", slot=None, activated=None):
    combo = QComboBox()
    for text, data in items:
        combo.addItem(text, data)
    if tip:
        combo.setToolTip(tip)
    if slot is not None:
        # Nur echte Indexwechsel; programmgesteuertes Laden blockt Signale.
        combo.currentIndexChanged.connect(slot)
    if activated is not None:
        # Auch die bewusste erneute Auswahl desselben Eintrags zaehlt.
        combo.activated.connect(activated)
    return combo


def _settings_dialog(dashboard):
    """Einstellungsfenster mit den beiden Formularen des Dashboards."""
    dialog = QDialog(dashboard)
    dialog.setWindowTitle("Einstellungen")
    dialog.setMinimumWidth(620)
    layout = QVBoxLayout(dialog)
    for name, title in (("transfer", "Einstellungen"),
                        ("accounts", "Konten und Verbindung")):
        group = QGroupBox(title)
        setattr(dialog, name, group)
        setattr(dialog, name + "_form", QFormLayout(group))
        layout.addWidget(group)
    dialog.close_button = _button(
        "Speichern und schliessen",
        "Speichert die Änderungen und schliesst die Einstellungen.",
        dashboard.close_settings)
    row = QHBoxLayout()
    row.addStretch(1)
    row.addWidget(dialog.close_button)
    layout.addLayout(row)
    return dialog


def _ask(dialog, title, text):
    yes = QMessageBox.StandardButton.Yes
    no = QMessageBox.StandardButton.No
    return QMessageBox.question(dialog, title, text, yes | no, no) == yes


def _default_buttons(login_button, primary, settings_button):
    """Enter gehoert dem Hauptknopf -- nie der Browser-Anmeldung.

    Ohne diese Zuweisung wird der zuerst gebaute Knopf automatisch der
    Standardknopf des Dialogs. Ein versehentliches Return im Fenster (etwa
    nach einem Kontowechsel per Tastatur) oeffnete dann ein Browserfenster
    zur Anmeldung.
    """
    login_button.setAutoDefault(False)
    settings_button.setAutoDefault(False)
    primary.setDefault(True)


@gui_guard
def offer_next_step(dialog):
    """Nach der Anmeldung ist der Hauptknopf der naechste Schritt."""
    dialog.primary.setDefault(True)
    dialog.primary.setFocus()


def confirm_deletions(dialog, removals, titles=None):
    """Ausdrücklich markierte Cloud-Kopien vor dem Löschen bestätigen."""
    titles = titles if isinstance(titles, dict) else {}
    lines = []
    for _key, tolino_id in removals:
        title = display_title(titles.get(str(tolino_id)))
        lines.append("- %s" % title)
    if len(removals) == 1:
        text = ("1 Buch wird aus tolino gelöscht (in Calibre bleibt es):"
                "\n\n%s" % "\n".join(lines))
    else:
        text = ("%d Bücher werden aus tolino gelöscht (in Calibre bleiben "
                "sie):\n\n%s" % (len(removals), "\n".join(lines)))
    return _ask(dialog, "Bücher aus tolino löschen?", text)


def confirm_replacements(dialog, jobs):
    """Ersetzen einer vorhandenen Cloud-Kopie vorher ansprechen."""
    titles = ["- %s" % job.label() for job in jobs[:20]]
    count = len(jobs)
    if count == 1:
        text = ("Die Cloud-Kopie dieses Buchs wird ersetzt. Lesestatus, "
                "Lesefortschritt und Sammlungen der alten Kopie gehen "
                "verloren:\n\n%s" % "\n".join(titles))
    else:
        text = ("Die Cloud-Kopie dieser %d Bücher wird ersetzt. "
                "Lesestatus, Lesefortschritt und Sammlungen der alten "
                "Kopien gehen verloren:\n\n%s" % (count, "\n".join(titles)))
    if count > 20:
        text += "\n- … und %d weitere" % (count - 20)
    return _ask(dialog, "Cloud-Kopie ersetzen?", text)


def report_skipped_uploads(dialog, skipped_uploads, nothing_else=False):
    """Bücher ohne verwendbare Datei melden; True heisst «nichts zu tun»."""
    text = ("Diese Bücher haben keine Datei im gewählten Format:\n\n%s"
            % "\n".join(skipped_uploads))
    if not nothing_else:
        text += "\n\nDie übrigen Aktionen werden trotzdem ausgeführt."
    QMessageBox.warning(dialog, "Einige Bücher wurden nicht hochgeladen", text)
    return bool(nothing_else)


class DashboardParts:
    """Widget-Aufbau; Ablauf und Zustand bleiben im Dashboard."""

    def _build_ui(self):
        root = QVBoxLayout(self)
        intro = QLabel(
            "Melde dich bei tolino an, vergleiche die Bibliotheken und "
            "wähle aus, was übertragen wird. Erst danach wird etwas geändert.")
        intro.setWordWrap(True)
        root.addWidget(intro)
        self._build_main_actions(root)
        self.settings_dialog = _settings_dialog(self)
        self._build_transfer_settings()
        self._build_account_settings()
        root.addWidget(_button("Schliessen", slot=self.close))

    def _build_main_actions(self, root):
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.account_quick = _combo(
            tip="Jedes Konto hat eine eigene Anmeldung und einen eigenen "
                "Sync-Stand.",
            slot=lambda index: self._account_selected(
                index, self.account_quick))
        self.account_quick_label = QLabel("Konto")
        self.login_button = _button(
            LOGIN_BUTTON_TEXT, LOGIN_BUTTON_TIP, self.browser_login)
        self.primary = _button(
            COMPARE_BUTTON_TEXT, COMPARE_BUTTON_TIP, self.primary_action)
        self.progress_label = QLabel("Bereit")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.settings_button = _button(
            "Einstellungen …", "Upload-Formate und Konten.",
            self.open_settings)
        _default_buttons(self.login_button, self.primary, self.settings_button)
        form = QFormLayout()
        for label, widget in (
                (self.account_quick_label, self.account_quick),
                ("", self.login_button), ("", self.primary),
                ("", self.status), ("", self.progress_label),
                ("", self.progress), ("", self.settings_button)):
            form.addRow(label, widget)
        root.addLayout(form)

    def _build_transfer_settings(self):
        form = self.settings_dialog.transfer_form
        self.formats = QLineEdit()
        self.formats.setToolTip(
            "EPUB oder PDF, durch Komma getrennt. Es wird das erste "
            "vorhandene Format hochgeladen.")
        form.addRow("Upload-Formate", self.formats)
        self.column_buttons = {}
        for index, (label, _name, datatype) in enumerate(CUSTOM_COLUMNS):
            button = _button(
                "#%s anlegen …" % label,
                "Legt die Spalte #%s (%s) an, falls sie fehlt. Vorhandene "
                "Spalten bleiben unverändert."
                % (label, COLUMN_KIND_NAMES.get(datatype, datatype)),
                lambda _checked=False, key=label: self.create_columns(key))
            self.column_buttons[label] = button
            form.addRow("Calibre-Spalten" if index == 0 else "", button)

    def _build_account_settings(self):
        form = self.settings_dialog.accounts_form
        self.account_select = _combo(
            slot=lambda index: self._account_selected(
                index, self.account_select))
        self.account_new = _button("Konto hinzufügen", slot=self.new_account)
        self.account_remove = _button("Konto entfernen …",
                                      slot=self.remove_account)
        buttons = QHBoxLayout()
        buttons.addWidget(self.account_new)
        buttons.addWidget(self.account_remove)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(QLineEdit.EchoMode.Password)
        self.refresh.setPlaceholderText(
            "Gespeichert – nur zum Ersetzen eintippen")
        self.logout_button = _button(
            "Abmelden", "Löscht den gespeicherten Zugang dieses Kontos.",
            self.logout)
        self.partner = _combo(
            [(PARTNERS[pid]["name"], pid) for pid in sorted(PARTNERS)],
            "Nur nötig, wenn ein Zugang keinen Buchhändler nennt – sonst "
            "wird er beim Anmelden aus dem Token gelesen.",
            slot=self._partner_selected, activated=self._partner_selected)
        for label, widget in (
                ("Aktives Konto", self.account_select), ("", buttons),
                ("Buchhändler (aus dem Token)", self.partner),
                ("Geräte-ID (automatisch)", self.hardware),
                ("Zugangstoken (nur bei Problemen ändern)", self.refresh),
                ("", self.logout_button)):
            form.addRow(label, widget)

    def create_columns(self, label):
        database = getattr(self.gui, "current_db", None)
        try:
            created = create_custom_columns(database, (label,))
        except Exception as exc:
            log_warning("Spalte #%s konnte nicht angelegt werden: %s"
                        % (label, sanitize_error(exc)))
            error_dialog(self, "Spalte nicht angelegt", str(exc), show=True)
            return
        if not created:
            info_dialog(self, "Calibre-Spalte",
                        "Die Spalte #%s ist schon vorhanden." % label,
                        show=True, show_copy_button=False)
            return
        info_dialog(
            self, "Calibre-Spalte angelegt",
            "Angelegt: #%s. Calibre neu starten, damit die Spalte in der "
            "Bibliothek nutzbar ist." % label,
            show=True, show_copy_button=False)


"""Hauptfenster, Einstellungen und der sichtbare Sync-Ablauf."""
from .ui_common import (CANCEL_MESSAGE, QComboBox, QDialog,
                        QFormLayout, QGroupBox, QHBoxLayout, QInputDialog,
                        QLabel, QLineEdit, QMessageBox, QProgressBar,
                        QPushButton, QThread, QVBoxLayout, _apply_busy,
                        _dialog_busy, gui_guard, qt_enum,
                        start_worker,
                        _login_failure_guidance,
                        _stop_thread, sync_result_detail,
                        sync_result_text, Qt)
from calibre.gui2 import error_dialog, info_dialog
from .token_vault import TOKEN_VAULT
from .config import (CredentialStore, PREFERRED_FORMATS, account_by_name,
                     load_state, remove_account as remove_saved_account,
                     save_account, save_credentials,
                     set_active_account, settings)
from .run_context import RunContext
from .library import (COLUMN_KIND_NAMES, CUSTOM_COLUMNS, TOLINO_IGNORE_COLUMN,
                      create_custom_columns, column_state, bool_column_available,
                      update_read_status, update_tolino_ids)

from . import dashboard_plan as _dashboard_plan
from .partners import PARTNERS
from .tolino import (RELOGIN, hardware_id, log_warning,
                     normalize_refresh_token, report_error, sanitize_error)
from .cdp import grabber_window_state
from .ui_dialogs import InventoryDialog
from .ui_workers import PrepareWorker, SyncWorker
from .ui_auth_workers import BrowserLoginWorker, SessionCheckWorker


class SettingsDialog(QDialog):
    """Bündelt Übertragungsoptionen und Kontoverwaltung."""

    def __init__(self, dashboard):
        QDialog.__init__(self, dashboard)
        self.dashboard = dashboard
        self.setWindowTitle("Einstellungen")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)

        self.transfer = QGroupBox("Einstellungen")
        self.transfer_form = QFormLayout(self.transfer)
        layout.addWidget(self.transfer)

        self.accounts = QGroupBox("Konten und Verbindung")
        self.accounts_form = QFormLayout(self.accounts)
        layout.addWidget(self.accounts)

        self.close_button = QPushButton("Speichern und schließen")
        self.close_button.setToolTip(
            "Speichert die Änderungen und schließt die Einstellungen.")
        self.close_button.clicked.connect(self.dashboard.close_settings)
        bottom = QHBoxLayout()
        bottom.addStretch(1)
        bottom.addWidget(self.close_button)
        layout.addLayout(bottom)


@gui_guard
def _offer_next_step(dialog):
    """Nach der Anmeldung ist der Hauptknopf der naechste Schritt.

    Kein modaler Dialog: der Knopf wird
    Standardknopf und bekommt den Fokus, damit «Enter» den Vergleich startet
    -- die Statuszeile sagt vorher, was er tut. Als freie Funktion, weil
    Vorablauf und Anmeldung sie aus verschiedenen Zustaenden aufrufen.
    """
    primary = getattr(dialog, "primary", None)
    if primary is None:
        return
    primary.setDefault(True)
    primary.setFocus()


def _persist_sync_result(dialog, state, updates):
    """Sync-Zustand, Tolino-IDs und bestätigten Lesestatus sichern.

    Die beiden Calibre-Schreibvorgänge laufen getrennt und sind einzeln
    abgesichert: ein Fehler in einer Spalte darf weder den anderen
    Schreibvorgang noch den Abschluss des Laufs verhindern.
    """
    account_name = str(
        getattr(getattr(dialog, "worker", None), "account_name", None)
        or getattr(dialog, "account_name", "") or "")
    try:
        if account_name:
            save_account(account_name, {"state": state}, active=account_name)
    except Exception as exc:
        log_warning("Ergebnis der Synchronisierung konnte nicht "
                    "gespeichert werden: %s" % sanitize_error(exc))
    if dialog.columns.get("id"):
        try:
            update_tolino_ids(
                dialog.gui.current_db, updates, enabled=True,
                account_name=account_name)
        except Exception as exc:
            log_warning("Tolino-IDs konnten nicht in Calibre geschrieben "
                        "werden: %s" % sanitize_error(exc))
    if dialog.columns.get("read"):
        worker = getattr(dialog, "worker", None)
        try:
            update_read_status(
                dialog.gui.current_db,
                list(getattr(worker, "read_updates", ()) or ()))
        except Exception as exc:
            log_warning("Lesestatus konnte nicht in Calibre geschrieben "
                        "werden: %s" % sanitize_error(exc))


def _sync_cancelled_text(outcome):
    """Abschlusstext eines regulären Abbruchs -- auch mit Teilerfolgen."""
    result = sync_result_text(outcome, "")
    if result:
        return "Abgebrochen. " + result
    return "Abgebrochen. Es wurde nichts übertragen."


def _sync_failure_heading(outcome):
    """Titel und Vorsatz eines fehlerhaften Laufs.

    «Teilweise fehlgeschlagen» heisst: mindestens eine Aktion wurde wirklich
    übertragen. Der Unterschied gehört in Titel und Text; die beiden Handler
    bleiben getrennt, weil sich ihre Ergebnisfälle unterscheiden.
    """
    succeeded = (int(outcome.get("uploaded") or 0)
                 + int(outcome.get("updated") or 0)
                 + int(outcome.get("deleted") or 0))
    if succeeded:
        return "Teilweise fehlgeschlagen", "Teilweise abgeschlossen: "
    return "Fehlgeschlagen", "Fehlgeschlagen: "


class SyncDashboard(QDialog):
    thread = None
    worker = None
    login_thread = None
    login_worker = None
    prepare_thread = None
    prepare_worker = None

    def __init__(self, gui):
        QDialog.__init__(self, gui)
        self.gui = gui
        # Ein Plugin-Fenster darf beim Schliessen nicht die Anwendung beenden.
        self.setAttribute(
            qt_enum(Qt, "WA_QuitOnClose", "WidgetAttribute"), False)
        self.thread = None
        self.worker = None
        self.login_thread = None
        self.login_worker = None
        self.prepare_thread = None
        self.prepare_worker = None
        # Welche tolino-Spalten die Bibliothek hat (id/read/ignore).
        self.columns = {}
        self.account_name = ""
        self.setWindowTitle("tolino Cloud Sync")
        self.setMinimumWidth(560)
        root = QVBoxLayout(self)
        self._build_intro(root)
        self._build_main_actions(root)
        self.settings_dialog = SettingsDialog(self)
        self._build_transfer_settings()
        self._build_account_settings()
        self._build_footer(root)
        self.load_values()

    def _build_intro(self, root):
        """Kurze Erklaerung des Ablaufs ueber den Knoepfen."""
        intro = QLabel(
            "Melde dich bei tolino an, vergleiche die Bibliotheken und "
            "wähle aus, was übertragen wird. Erst danach wird etwas geändert.")
        intro.setWordWrap(True)
        root.addWidget(intro)

    def _build_main_actions(self, root):
        """Buchhaendler, Anmeldung und der eine Hauptknopf."""
        # --- Ein Ablauf: anmelden, dann vergleichen ------------------------
        self.partner = QComboBox()
        for pid, partner in sorted(PARTNERS.items()):
            self.partner.addItem(partner["name"], pid)
        self.partner.setToolTip(
            "Bei diesem Händler hast du dein tolino-Konto.")
        self.status = QLabel()
        self.status.setWordWrap(True)
        # Kontoauswahl direkt auf der ersten Seite -- aber nur, wenn es
        # wirklich mehrere Konten gibt. Bei einem einzigen Konto wäre die
        # Zeile nur Ballast.
        self.account_quick = QComboBox()
        self.account_quick.setToolTip(
            "Jedes Konto hat eine eigene Anmeldung und einen eigenen Sync-Stand.")
        self.account_quick.currentIndexChanged.connect(
            self.quick_account_changed)
        self.account_quick_label = QLabel("Konto")
        # Der Anmeldeknopf steht wieder sichtbar auf der ersten Seite --
        # auch wenn schon ein Zugang gespeichert ist, denn genau dann
        # braucht man ihn, wenn der Zugang abgelaufen ist.
        self.login_button = QPushButton("Bei tolino anmelden")
        self.login_button.setToolTip(
            "Öffnet ein Browserfenster zur Anmeldung.")
        self.login_button.clicked.connect(self.browser_login)
        self.primary = QPushButton("Bibliothek vergleichen …")
        self.primary.setToolTip(
            "Vergleicht deine Calibre-Bibliothek mit tolino.")
        self.primary.clicked.connect(self.primary_action)
        self.progress_label = QLabel("Bereit")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setVisible(False)
        self.settings_button = QPushButton("Einstellungen …")
        self.settings_button.setToolTip("Upload-Formate und Konten.")
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

    def _build_transfer_settings(self):
        """Upload-Optionen und ein Knopf je Calibre-Spalte."""
        transfer = self.settings_dialog.transfer_form
        self.formats = QLineEdit()
        self.formats.setToolTip(
            "EPUB oder PDF, durch Komma getrennt. Es wird das erste "
            "vorhandene Format hochgeladen.")
        transfer.addRow("Upload-Formate", self.formats)
        # Die Spalten von Hand anzulegen ist fehleranfaellig. Je Spalte ein
        # eigener Knopf: er legt NUR seine Spalte an, wenn sie fehlt, und
        # laesst Vorhandenes unberuehrt.
        self.column_buttons = {}
        for index, (label, _name, datatype) in enumerate(CUSTOM_COLUMNS):
            button = QPushButton("#%s anlegen …" % label)
            button.setToolTip(
                "Legt die Spalte #%s (%s) an, falls sie fehlt. Vorhandene "
                "Spalten bleiben unverändert."
                % (label, COLUMN_KIND_NAMES.get(datatype, datatype)))
            button.clicked.connect(
                lambda _checked=False, key=label: self.create_columns(key))
            self.column_buttons[label] = button
            # Nur die erste Zeile traegt die Beschriftung; drei eigene
            # Zeilen sind der klarere Weg als ein Knopf fuer alles.
            transfer.addRow("Calibre-Spalten" if index == 0 else "", button)

    def _build_account_settings(self):
        """Kontoverwaltung und die beiden technischen Zugangsfelder."""
        accounts = self.settings_dialog.accounts_form
        self.account_select = QComboBox()
        self.account_new = QPushButton("Konto hinzufügen")
        self.account_remove = QPushButton("Konto entfernen …")
        account_buttons = QHBoxLayout()
        account_buttons.addWidget(self.account_new)
        account_buttons.addWidget(self.account_remove)
        self.account_select.currentIndexChanged.connect(self.account_changed)
        self.account_new.clicked.connect(self.new_account)
        self.account_remove.clicked.connect(self.remove_account)
        self.hardware = QLineEdit()
        self.refresh = QLineEdit()
        self.refresh.setEchoMode(
            qt_enum(QLineEdit, "Password", "EchoMode"))
        # Das Feld zeigt den Token nicht an: das Konto ist die Quelle, ein
        # leeres Feld heisst «gespeicherten Zugang behalten».
        self.refresh.setPlaceholderText(
            "Gespeichert – nur zum Ersetzen eintippen")
        accounts.addRow("Aktives Konto", self.account_select)
        accounts.addRow("", account_buttons)
        accounts.addRow("Geräte-ID (automatisch)", self.hardware)
        accounts.addRow("Zugangstoken (nur bei Problemen ändern)", self.refresh)
        # Das Feld ist Handeingabe ohne Zustand; abgemeldet wird ausdruecklich
        # hier -- sonst gaebe es keinen Weg mehr, den Zugang zu loeschen.
        self.logout_button = QPushButton("Abmelden")
        self.logout_button.setToolTip(
            "Löscht den gespeicherten Zugang dieses Kontos.")
        self.logout_button.clicked.connect(self.logout)
        accounts.addRow("", self.logout_button)

    def _build_footer(self, root):
        """Schliessen-Knopf ohne QDialogButtonBox (Qt6-Rollenwarnung)."""
        close = QPushButton("Schließen")
        close.clicked.connect(self.close)
        root.addWidget(close)

    def create_columns(self, label):
        """Genau diese Calibre-Spalte anlegen -- vorhandene bleibt unberührt."""
        database = getattr(self.gui, "current_db", None)
        try:
            created = create_custom_columns(database, (label,))
        except Exception as exc:
            log_warning("Spalte #%s konnte nicht angelegt werden: %s"
                        % (label, sanitize_error(exc)))
            error_dialog(self, "Spalte nicht angelegt", str(exc), show=True)
            return
        if not created:
            info_dialog(
                self, "Calibre-Spalte",
                "Die Spalte #%s ist schon vorhanden." % label,
                show=True, show_copy_button=False)
            return
        info_dialog(
            self, "Calibre-Spalte angelegt",
            "Angelegt: #%s.\n\nCalibre neu starten, damit die Spalte in der "
            "Bibliothek nutzbar ist." % label,
            show=True, show_copy_button=False)
        self._refresh_column_flags(database)

    def _refresh_column_flags(self, database):
        """Spaltenstatus neu lesen: der naechste Vergleich nutzt sie sofort."""
        try:
            # Die Spalte selbst entscheidet -- frueher konnte eine
            # Einstellung sie zusaetzlich abschalten.
            self.columns = column_state(database)
        except Exception as exc:
            log_warning("Spaltenstatus konnte nicht gelesen werden: %s"
                        % sanitize_error(exc))
        self.update_status()

    def _save_visible_account(self, values=None):
        """Das sichtbare Konto und die globalen Optionen EINMAL einlesen.

        Vorher lasen zwei getrennte Pfade dieselben Widgets (inklusive
        zweimaliger Format-Liste). `values()` ist die eine Quelle; die
        globalen Schluessel stehen in config.
        """
        if not getattr(self, "account_name", None):
            return
        values = values or self.values()
        name = str(values.get("account_name") or "")
        if not name:
            # Das angezeigte Konto existiert nicht mehr (z. B. extern
            # geänderte Einstellungen). Dann darf weder der Zustand eines
            # anderen Kontos noch ein leerer Zustand unter dem alten Namen
            # gespeichert werden.
            log_warning("Das sichtbare Konto existiert nicht mehr; "
                        "Änderungen wurden nicht gespeichert.")
            return
        # Formate und Konto in EINEM Commit: vorher waren es zwei
        # vollständige Snapshots und zwei Schreibvorgänge.
        save_account(name, values, active=name,
                     preferred_formats=values[PREFERRED_FORMATS])

    def account_changed(self, index):
        """Konto in den Einstellungen gewechselt."""
        self._account_selected(index, self.account_select)

    def quick_account_changed(self, index):
        """Konto auf der ersten Seite gewechselt."""
        self._account_selected(index, self.account_quick)

    def _account_selected(self, index, combo):
        """Gewähltes Konto übernehmen -- ein Weg für beide Comboboxen."""
        if index < 0 or not getattr(self, "account_names", None):
            return
        self.switch_account(combo.itemData(index))

    def switch_account(self, name):
        """Konto wechseln – erst sichern, dann das neue laden.

        Ein Weg für beide Stellen (erste Seite und Einstellungen): das
        sichtbare Konto wird gespeichert, das gewählte wird das aktive, und
        `load_values` holt Buchhändler, Gerätekennung und Anmeldung dazu.
        Angezeigtes und aktives Konto sind danach IMMER dasselbe: sonst
        lesen settings() und die tolino-ID-Zuordnung das vorherige Konto.
        """
        name = str(name or "")
        if not name or name == self.account_name:
            return False
        if name not in getattr(self, "account_names", ()):
            return False
        # Während Sync/Vorbereitung/Anmeldung bleibt das Konto stehen: Der
        # laufende Kontext gehört zum gestarteten Konto. (Die Worker-Logik
        # sichert zusätzlich selbst gegen den Kontonamen ab.)
        if _dialog_busy(self):
            try:
                self.load_values()
            except Exception:
                pass
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
        yes = qt_enum(QMessageBox, "Yes", "StandardButton")
        no = qt_enum(QMessageBox, "No", "StandardButton")
        answer = QMessageBox.question(
            self, "Konto entfernen",
            "Konto «%s» entfernen? Anmeldung und Sync-Stand dieses Kontos "
            "werden gelöscht. Bücher in tolino und Calibre bleiben." % name,
            yes | no, no)
        if answer != yes:
            return
        if not remove_saved_account(name):
            return
        self.load_values()

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
        partner_id = values["partner_id"]
        if self.partner.findData(partner_id) < 0:
            partner_id = self.partner.itemData(0)
            if partner_id is not None:
                save_account(self.account_name, {"partner_id": partner_id})
        self.partner.setCurrentIndex(max(0, self.partner.findData(partner_id)))
        self.hardware.setText(values["hardware_id"] or hardware_id())
        # Der Token steht NUR im Konto (config). Ein Feldinhalt war ein
        # zweiter Ort, der einen im Hintergrund rotierten Token mit der
        # alten Kopie ueberschreiben konnte -- jetzt bleibt es leer.
        self.refresh.setText("")
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.update_status()

    def open_settings(self):
        """Einstellungen öffnen – speichert beim Schliessen."""
        self.settings_dialog.exec()

    def close_settings(self):
        self._save_visible_account()
        self.settings_dialog.accept()

    def primary_action(self):
        """Hauptknopf: vergleichen – oder einen laufenden Sync abbrechen.

        Das Anmelden hat wieder seinen eigenen Knopf darüber,
        damit es auch bei abgelaufenem Zugang sofort erreichbar ist.
        """
        if self.thread is not None or self.prepare_thread is not None:
            self.cancel_sync()
        else:
            self.start_sync()

    def update_status(self):
        logged_in = bool(self._effective_refresh())
        running = _dialog_busy(self)
        if running:
            _apply_busy(self, True)
            if self.thread is not None:
                running_text = "Synchronisierung läuft …"
            elif self.prepare_thread is not None:
                running_text = "Vergleich läuft …"
            else:
                running_text = "Anmeldung wird geprüft …"
            self.status.setText(running_text)
        elif logged_in:
            self.status.setText("Angemeldet.")
        else:
            self.status.setText("Nicht angemeldet.")
        self.progress.setVisible(running)
        self.progress_label.setVisible(running)
        # Knopf und Tooltip folgen dem gespeicherten Zugang.
        login_button = getattr(self, "login_button", None)
        if login_button is not None:
            if logged_in:
                login_button.setText("Anmeldung prüfen")
                login_button.setToolTip(
                    "Prüft die gespeicherte Anmeldung. Öffnet den Browser nur bei Bedarf.")
            else:
                login_button.setText("Bei tolino anmelden")
                login_button.setToolTip("Öffnet ein Browserfenster zur Anmeldung.")
        if running:
            self.primary.setText("Abbrechen")
            self.primary.setToolTip(
                "Bereits übertragene Änderungen bleiben bestehen.")
            self.primary.setEnabled(True)
        else:
            _apply_busy(self, False)
            self.primary.setText("Bibliothek vergleichen …")
            # Ohne Zugang gibt es nichts zu vergleichen -- der Grund steht
            # im Tooltip und in der Statuszeile, der Anmeldeknopf ist direkt
            # darüber sichtbar.
            self.primary.setEnabled(logged_in)
            self.primary.setToolTip(
                "Vergleicht deine Calibre-Bibliothek mit tolino."
                if logged_in else "Noch nicht angemeldet. " + RELOGIN)

    def _stored_refresh(self):
        """Gespeicherter Zugang des ANGEGEZEIGTEN Kontos -- nie ein anderes."""
        account = self._selected_account() or {}
        return normalize_refresh_token(account.get("refresh_token") or "")

    def _typed_refresh(self):
        """Von Hand eingetippter Token; leer heisst «nichts ersetzen»."""
        refresh = getattr(self, "refresh", None)
        return normalize_refresh_token(refresh.text()) if refresh is not None else ""

    def _effective_refresh(self):
        """Der Token, mit dem gearbeitet wird: eingetippt vor gespeichert."""
        return self._typed_refresh() or self._stored_refresh()

    def store_refresh(self, refresh, hardware=""):
        """Token im Konto speichern -- der einzige Ort fuer Zugangsdaten."""
        token = normalize_refresh_token(refresh)
        if not token or not self.account_name:
            return
        save_credentials(
            self.account_name, token,
            str(hardware or "").strip() or self.hardware.text().strip()
            or hardware_id())
        self.refresh.setText("")
        self.update_status()

    def logout(self):
        """Nur den gespeicherten Zugang loeschen -- Konto und ID bleiben."""
        if not self.account_name:
            return
        save_account(self.account_name, {"refresh_token": ""},
                     active=self.account_name)
        self.refresh.setText("")
        self.update_status()

    def _selected_account(self):
        """The account this dialog shows – never a different 'active' one."""
        return account_by_name(getattr(self, "account_name", None))

    def run_context(self, values=None):
        """Konto und Optionen des Laufs als expliziter Kontext."""
        values = self.values() if values is None else values
        return RunContext.from_values(values, self.columns)

    def values(self):
        """Sichtbare Werte des Kontos; der Token nur, wenn eingetippt wurde.

        Ohne Eingabe fehlt der Schluessel ganz: ``save_account()`` mischt
        nur vorhandene Schluessel und laesst den gespeicherten Token damit
        unberuehrt -- auch wenn der Keep-alive ihn gerade rotiert hat.
        """
        # Nur ohne Kontonamen auf das aktive Konto zurückfallen. Ist der
        # angezeigte Name gesetzt, aber nicht mehr vorhanden, bleibt der
        # Zustand leer -- sonst landeten die Werte eines anderen Kontos
        # unter diesem Namen in den Einstellungen.
        account = self._selected_account()
        if not account and not getattr(self, "account_name", None):
            account = settings()
        if not isinstance(account, dict):
            account = {}
        hardware = self.hardware.text().strip() or hardware_id()
        values = {
            "account_name": str(account.get("name") or ""),
            "partner_id": self.partner.currentData(),
            "hardware_id": hardware,
            "preferred_formats": ([
                value for value in dict.fromkeys(
                    x.strip().upper() for x in self.formats.text().split(",")
                    if x.strip()) if value in ("EPUB", "PDF")
            ] or ["EPUB"]),
            "state": (account.get("state")
                      if isinstance(account.get("state"), dict) else {}),
        }
        typed = self._typed_refresh()
        if typed:
            values["refresh_token"] = typed
        return values

    def _login_running(self):
        """Läuft gerade eine Anmeldeprüfung oder ein Anmeldefenster?"""
        return self.login_thread is not None and self.login_thread.isRunning()

    def browser_login(self):
        """Anmeldeknopf: erst die gespeicherte Anmeldung prüfen.

        Ein neues Anmeldefenster ist nur nötig, wenn die vorhandene Anmeldung
        nicht mehr funktioniert. Deshalb läuft zuerst eine stille Prüfung im
        Hintergrund; das Fenster geht erst auf, wenn tolino die Anmeldung
        ablehnt. Ohne gespeicherte Anmeldung gibt es nichts zu prüfen.
        """
        if self._login_running():
            return  # eine Prüfung oder Anmeldung läuft bereits
        values = self.values()
        if self._typed_refresh():
            # Von Hand eingetippt: erst ins Konto speichern, dann pruefen.
            self.store_refresh(self._typed_refresh())
            values = self.values()
        context = RunContext.from_values(values, self.columns)
        if self._effective_refresh():
            self._check_stored_login(context)
            return
        self._start_login_window(context)

    def _check_stored_login(self, context):
        """Gespeicherte Anmeldung im Hintergrund prüfen."""
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText("Anmeldung wird geprüft …")
        worker = SessionCheckWorker(context)
        worker.valid.connect(self.login_still_valid)
        worker.invalid.connect(self.login_rejected)
        worker.busy.connect(self.login_check_busy)
        start_worker(self, worker, QThread(), "login_thread", "login_worker",
                     (worker.valid, worker.invalid, worker.busy))

    @gui_guard
    def login_still_valid(self):
        """Prüfung bestanden: Anmeldung bleibt gültig."""
        self._login_cleanup()
        self.status.setText("Angemeldet.")
        _offer_next_step(self)

    @gui_guard
    def login_rejected(self, message=""):
        """Prüfung fehlgeschlagen: jetzt das Anmeldefenster öffnen.

        Der technische Grund landet im Log statt im Fenster: die Box bleibt
        eine Zeile und die Fehlermeldung knapp.
        """
        self._login_cleanup()
        context = self.run_context()
        log_warning("Anmeldung abgelehnt: " + str(message))
        self.status.setText("Anmeldung abgelaufen – bitte neu anmelden.")
        self._start_login_window(context)

    @gui_guard
    def login_check_busy(self, _reason=""):
        """Anmeldung wird gerade geprüft; keine zweite Prüfung starten."""
        self._login_cleanup()
        self.status.setText("Die Anmeldung wird gerade erneuert, bitte kurz warten.")

    def _start_login_window(self, context):
        """Das eigene Anmeldefenster öffnen und die Anmeldung übernehmen."""
        if self._login_running():
            return  # a sign-in attempt is already running
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText(
            "Bitte im Browserfenster anmelden, bis die Bücherliste erscheint.")
        worker = BrowserLoginWorker(context.partner_id, context.hardware_id)
        worker.progress.connect(self._login_progress)
        worker.completed.connect(self.login_completed)
        worker.failed.connect(self.login_failed)
        start_worker(self, worker, QThread(), "login_thread", "login_worker",
                     (worker.completed, worker.failed))

    @gui_guard
    def login_completed(self, refresh, hardware):
        self._login_cleanup()
        if hardware:
            self.hardware.setText(str(hardware))
        self.store_refresh(refresh, str(hardware or ""))
        # Erfolg steht in der Statuszeile -- kein zusaetzlicher modaler
        # Dialog fuer den Normalfall.
        self.status.setText("Angemeldet.")
        # Der Grabber schliesst sein Fenster bereits. Die UI fragt nur
        # den Zustand ab; ein fehlender Besitzsatz heisst nicht «offen».
        if grabber_window_state()["window"]:
            self.status.setText(
                "Angemeldet. Das Anmeldefenster ist noch offen.")
        _offer_next_step(self)

    @gui_guard
    def login_failed(self, error):
        """Fehlgeschlagene Anmeldung: Ursache + naechster Schritt nennen."""
        self._login_cleanup()
        # Technische Details (inkl. Traceback) nur ins Log.
        # Der sichtbare Zugang ist ein bekanntes Geheimnis: er darf
        # weder im Dialog noch im Log auftauchen.
        secrets = tuple(token for token in
                        (self._stored_refresh(), self._typed_refresh()) if token)
        if isinstance(error, BaseException):
            detail = report_error(error, "Anmeldefenster", secrets)
        else:
            detail = sanitize_error(error, secrets)
        log_warning("Anmeldung nicht abgeschlossen: " + detail)
        title, step = _login_failure_guidance(error)
        code = str(getattr(error, "code", "") or "")
        if code:
            title = "%s (Code %s)" % (title, code)
        self.status.setText("Anmeldung fehlgeschlagen.")
        QMessageBox.warning(self, title, step)

    @gui_guard
    def _login_progress(self, text):
        """Live-Hinweise des Browser-Grabs in der Statuszeile zeigen."""
        self.status.setText(str(text))

    @gui_guard
    def _login_cleanup(self):
        self.login_thread = None
        self.login_worker = None
        # `update_status` sperrt bzw. gibt die Bedienelemente selbst frei.
        self.update_status()

    @staticmethod
    def _worker_client(context):
        """Client aus dem Lauf-Kontext bauen."""
        store = CredentialStore(context.credentials, context.account_name)
        return TOKEN_VAULT.login(store)

    def _show_prepare_error(self, exc, context):
        """Nur den kurzen Fehlertext zeigen; technische Details protokollieren."""
        secrets = context.secrets()
        message = report_error(exc, "Vorbereitung", secrets)
        error_dialog(self, "Vergleich fehlgeschlagen",
                     message + " Es wurde nichts geändert.", show=True)

    def start_sync(self):
        """Bibliothek vergleichen: lokaler Snapshot im GUI, Netz im Worker.

        Der Calibre-Datenbankzugriff (Metadaten + Format-Signaturen) bleibt
        im GUI-Thread. Login und Cloud-Inventar laufen in `PrepareWorker`;
        der Vergleich und die Auswahl kommen über Qt-Signale zurück.
        """
        if self.thread is not None or self.prepare_thread is not None:
            return
        values = self.values()
        if not self._effective_refresh():
            status = getattr(self, "status", None)
            if status is not None:
                status.setText("Nicht angemeldet.")
            return
        self._save_visible_account(values)
        self.columns = column_state(self.gui.current_db)
        context = RunContext.from_values(values, self.columns)
        state = load_state(values.get("state"))
        try:
            # Calibre-DB und Qt-Widgets nur hier (GUI-Thread) lesen.
            metadata = _dashboard_plan.local_metadata(self, context, state)
        except Exception as exc:
            self._show_prepare_error(exc, context)
            return
        self._start_prepare_worker(context, metadata, state)

    def _start_prepare_worker(self, context, metadata, state):
        """Netz-Vorbereitung im Hintergrund starten (GUI bleibt bedienbar)."""
        _apply_busy(self, True)
        self.status.setText("Vergleich läuft …")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_label.setText("Verbinde mit tolino …")
        worker = PrepareWorker(context, metadata, state)
        worker.progress.connect(self._prepare_progress)
        worker.ready.connect(self._prepare_ready)
        worker.failed.connect(self._prepare_failed)
        start_worker(self, worker, QThread(self), "prepare_thread",
                     "prepare_worker", (worker.ready, worker.failed))
        self.update_status()

    @gui_guard
    def _prepare_progress(self, text):
        self.progress_label.setText(str(text))

    def _finish_prepare(self):
        """Vorbereitungs-Thread sauber abräumen (ohne unbegrenzt zu warten)."""
        thread = self.prepare_thread
        self.prepare_thread = None
        self.prepare_worker = None
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setText("Bereit")
        self.progress_label.setVisible(False)
        _stop_thread(thread, timeout_ms=2000)
        self.update_status()

    @gui_guard
    def _prepare_failed(self, message):
        worker = getattr(self, "prepare_worker", None)
        cancelled = bool(getattr(worker, "cancelled", False))
        self._finish_prepare()
        if cancelled or str(message) == CANCEL_MESSAGE:
            info_dialog(
                self, "Abgebrochen", "Abgebrochen. Es wurde nichts geändert.",
                show=True, show_copy_button=False)
            return
        log_warning("Vergleich fehlgeschlagen: " + sanitize_error(message))
        error_dialog(self, "Vergleich fehlgeschlagen", str(message), show=True)

    @gui_guard
    def _prepare_ready(self, payload):
        """Vergleich anzeigen (GUI-Thread) und den Sync starten."""
        context = payload["context"]
        account_name = context.account_name
        metadata = payload["metadata"]
        state = payload["state"]
        self._finish_prepare()

        # Nur die Hardware-ID gehört nach dem Vergleich in die Einstellungen.
        # Der Token wird durch den Client-Callback beim Rotieren gespeichert.
        hardware = getattr(self, "hardware", None)
        if payload.get("hardware"):
            context.hardware_id = str(payload["hardware"])
        if (payload.get("hardware") and hardware is not None
                and self.account_name == account_name):
            hardware.setText(str(payload["hardware"]))
        if payload.get("read_status_known") is False:
            # Der Dialog zeigt denselben Hinweis in der Tabelle.
            log_warning("Lesestatus unbekannt – er bleibt unverändert.")
        try:
            selection = self._confirm_comparison(
                payload["comparison"], context,
                client_factory=lambda: self._worker_client(context),
                automatic=len(_dashboard_plan.plan_collection_updates(
                    _dashboard_plan.series_map_from_metadata(
                        _dashboard_plan.syncable_metadata(metadata)), state)))
            if selection is None:
                return
            preparation = _dashboard_plan.plan_actions(
                self, context, metadata, state, selection)
            if preparation["removals"] and not self._confirm_deletions(
                    preparation["removals"], state,
                    preparation.get("removal_titles")):
                return
            self._start_sync_worker(context, preparation, state)
        except Exception as exc:
            # Ein Fehler NACH dem Vergleichsfenster darf nicht als
            # Traceback im Slot verschwinden: der Nutzer sähe sonst nur ein
            # geschlossenes Fenster und keinen einzigen Upload.
            self._show_prepare_error(exc, context)

    def _confirm_deletions(self, removals, state, titles=None):
        """Ausdrücklich markierte Cloud-Kopien vor dem Löschen bestätigen.

        `titles` enthält die Titel der im Vergleich markierten Kopien; als
        Rückfall kommen die Namen aus dem gespeicherten Zustand.
        """
        titles = titles if isinstance(titles, dict) else {}
        lines = []
        for key, tolino_id in removals:
            entry = state.get(key) if isinstance(state, dict) else {}
            title = (titles.get(str(tolino_id))
                     or (entry.get("title") if isinstance(entry, dict) else "")
                     or key)
            lines.append("- %s" % title)
        yes = qt_enum(QMessageBox, "Yes", "StandardButton")
        no = qt_enum(QMessageBox, "No", "StandardButton")
        answer = QMessageBox.question(
            self, "Bücher aus tolino löschen?",
            ("1 Buch wird aus tolino gelöscht (in Calibre bleibt es):\n\n%s"
             if len(removals) == 1 else
             "%d Bücher werden aus tolino gelöscht (in Calibre bleiben sie):\n\n%s")
            % (("\n".join(lines),) if len(removals) == 1 else
               (len(removals), "\n".join(lines))),
            yes | no, no)
        return answer == yes

    def _confirm_comparison(self, comparison, context, client_factory=None,
                            automatic=0):
        """Bestätigungsfenster zeigen; None, wenn es abgebrochen wurde."""
        dialog = InventoryDialog(
            comparison, self,
            client_factory=(client_factory or
                            (lambda: self._worker_client(context))),
            options={
                "collections": True,
                "automatic": int(automatic or 0),
                # Für den Hinweis im Fenster: ohne diese Spalte bleiben
                # gekaufte Bücher Upload-Kandidaten.
                "ignore_column": bool_column_available(
                    getattr(getattr(self, "gui", None), "current_db", None),
                    TOLINO_IGNORE_COLUMN),
            })
        if dialog.exec() != qt_enum(QDialog, "Accepted", "DialogCode"):
            return None
        # Die Auswahl des bestätigten Fensters entscheidet, was
        # passiert -- jede Aktion für sich; eine leere Auswahl ändert
        # nichts.
        return {
            "uploads": dialog._selected_book_ids("upload"),
            "metadata": dialog._selected_book_ids("metadata"),
            "read_changes": dialog.selected_read_changes(),
            "cloud_deletions": dialog.selected_cloud_deletions(),
        }

    def _confirm_replacements(self, jobs):
        """Ersetzen einer vorhandenen Cloud-Kopie vorher ansprechen.

        Ein Upload mit alter Kopie loescht diese danach (`SyncWorker`,
        `job.old_id`), und die alten Sammlungen gelten nicht mehr. Der
        Tooltip am Haekchen sagte das bisher nur; jetzt steht es mit
        Titeln vor dem Lauf -- Standardantwort «Nein».
        """
        titles = []
        for job in jobs[:20]:
            label = getattr(job, "label", None)
            titles.append("- %s" % (label() if callable(label) else "Buch"))
        count = len(jobs)
        if count == 1:
            text = ("Die Cloud-Kopie dieses Buchs wird ersetzt. Lesestatus, "
                    "Lesefortschritt und Sammlungen der alten Kopie gehen "
                    "verloren:\n\n%s" % "\n".join(titles))
        else:
            text = ("Die Cloud-Kopie dieser %d Bücher wird ersetzt. "
                    "Lesestatus, Lesefortschritt und Sammlungen der alten "
                    "Kopien gehen verloren:\n\n%s"
                    % (count, "\n".join(titles)))
        if len(jobs) > 20:
            text += "\n- … und %d weitere" % (len(jobs) - 20)
        yes = qt_enum(QMessageBox, "Yes", "StandardButton")
        no = qt_enum(QMessageBox, "No", "StandardButton")
        answer = QMessageBox.question(
            self, "Cloud-Kopie ersetzen?", text, yes | no, no)
        return answer == yes

    def _report_skipped_uploads(self, skipped_uploads, nothing_else=False):
        """Bücher ohne verwendbare Datei melden; True heisst «nichts zu tun».

        `nothing_else` sagt, dass ausser diesen übersprungenen Uploads keine
        Aktion mehr übrig ist: dann endet der Lauf hier, und die Meldung sagt
        das auch.
        """
        text = ("Diese Bücher haben keine Datei im gewählten Format:\n\n%s"
                % "\n".join(skipped_uploads))
        if not nothing_else:
            text += "\n\nDie übrigen Aktionen werden trotzdem ausgeführt."
        QMessageBox.warning(
            self, "Einige Bücher wurden nicht hochgeladen", text)
        return bool(nothing_else)

    def _start_sync_worker(self, context, preparation, state):
        """Übersprungene Uploads melden und den Hintergrundlauf starten."""
        skipped_uploads = preparation["skipped_uploads"]
        jobs = preparation["jobs"]
        # Das Vergleichsfenster kann beliebig lange offen bleiben; in der
        # Zeit darf der Keep-alive den (single-use) Token rotieren. Deshalb
        # nimmt der Vault im Worker den GESPEICHERTEN Zugang des Kontos
        # unmittelbar vor dem Login -- ein zweiter Snapshot hier waere
        # derselbe Aufruf mit demselben Ergebnis.
        total = _dashboard_plan.plan_counts(preparation, len(jobs))["steps"]
        if not total and not skipped_uploads:
            # Kein stiller Lauf: ohne ausführbare Aktion gibt es eine
            # Antwort statt eines Hintergrundstarts.
            # Calibre zeigt ein info_dialog nur mit show=True an; ohne das
            # Argument entsteht das Fenster und bleibt unsichtbar.
            info_dialog(self, "Nichts zu tun",
                        _dashboard_plan.nothing_to_do_text(preparation),
                        show=True, show_copy_button=False)
            return
        if skipped_uploads and self._report_skipped_uploads(
                skipped_uploads, nothing_else=not total):
            return
        replacements = [job for job in jobs
                        if getattr(job, "old_id", None)]
        if replacements and not self._confirm_replacements(replacements):
            status = getattr(self, "status", None)
            if status is not None:
                status.setText("Abgebrochen. Die Cloud-Kopie bleibt unverändert.")
            return
        plan = _dashboard_plan.plan_text(preparation, len(jobs))
        # Der Plan steht im Log, auch wenn der Lauf danach scheitert;
        # Zugangsdaten werden dabei nicht ausgegeben.
        log_warning("Sync geplant: %s" % plan)
        _apply_busy(self, True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        worker = SyncWorker(context, jobs, preparation["removals"],
                            preparation["meta_updates"],
                            preparation["series_map"],
                            read_changes=preparation.get("read_changes"),
                            titles=preparation.get("titles"),
                            metadata=preparation.get("metadata", {}),
                            state=state)
        worker.progress.connect(self.progress_changed)
        worker.completed.connect(self.sync_completed)
        worker.failed.connect(self.sync_failed)
        start_worker(self, worker, QThread(self), "thread", "worker")
        self.progress.setValue(0)
        # Der Plan steht sofort im Fenster: der Nutzer sieht, dass der Lauf
        # begonnen hat und was er tut.
        self.progress_label.setText(plan)
        self.update_status()

    @gui_guard
    def progress_changed(self, done, total, text):
        self.progress.setRange(0, total or 1)
        self.progress.setValue(done)
        if text:
            self.progress_label.setText(text)
        elif total:
            self.progress_label.setText(
            "Synchronisierung läuft: %d von %d Aktionen abgeschlossen."
            % (done, total))
        else:
            self.progress_label.setText("Synchronisierung läuft …")

    def cancel_sync(self):
        prepare = getattr(self, "prepare_worker", None)
        if prepare is not None:
            prepare.cancel()
        if self.worker:
            self.worker.cancel()
        self.progress_label.setText("Abbruch angefordert …")
        self.primary.setEnabled(False)

    @gui_guard
    def sync_completed(self, state, updates):
        """Erfolgreicher Lauf (auch nach regulaerem Abbruch).

        Persistiert wird unter dem KONTO DES LAUFS. UI-Felder werden nur
        ueberschrieben, solange dieses Konto noch angezeigt wird.
        """
        outcome = dict(getattr(self.worker, "outcome", None) or {})
        try:
            _persist_sync_result(self, state, updates)
        finally:
            # Auch ein unerwarteter Fehler beim Schreiben in Calibre darf
            # den Dialog nicht im Zustand «läuft» zurücklassen.
            self.finish_thread()
        if outcome.get("cancelled"):
            info_dialog(self, "Abgebrochen", _sync_cancelled_text(outcome),
                        show=True, show_copy_button=False)
            return
        if outcome.get("failed"):
            title, prefix = _sync_failure_heading(outcome)
            error_dialog(self, title, prefix + sync_result_text(outcome, ""),
                         det_msg=sync_result_detail(outcome), show=True)
            return
        info_dialog(self, "Fertig",
                    sync_result_text(outcome, "Fertig"), show=True,
                    show_copy_button=False)

    @gui_guard
    def sync_failed(self, message):
        outcome = dict(getattr(self.worker, "outcome", None) or {})
        worker = self.worker
        cancelled = bool(getattr(worker, "cancelled", False)) or \
            str(message) == CANCEL_MESSAGE
        # Auch ein Fehlerlauf hat evtl. schon Uploads abgeschlossen: deren
        # tolino-IDs in die Spalte schreiben, niemals für ein anderes Konto.
        try:
            _persist_sync_result(
                self, getattr(worker, "state", {}),
                getattr(worker, "updates", ()))
        finally:
            self.finish_thread()
        if cancelled:
            info_dialog(self, "Abgebrochen", _sync_cancelled_text(outcome),
                        show=True, show_copy_button=False)
            return
        result = sync_result_text(outcome, "")
        text = str(message or "Synchronisierung fehlgeschlagen.")
        if outcome.get("failed") and result:
            text += "\n\n" + result

        error_dialog(
            self, _sync_failure_heading(outcome)[0], text,
            det_msg=sync_result_detail(outcome), show=True)

    def finish_thread(self):
        thread = self.thread
        self.thread = None
        self.worker = None
        if thread is not None:
            _stop_thread(thread)
        self.progress_label.setText("Bereit")
        self.update_status()

    def closeEvent(self, event):
        """Schliessen: Abbruch anfordern, aber nie unbegrenzt blockieren."""
        if self.thread is not None or self.prepare_thread is not None:
            self.cancel_sync()
        _stop_thread(self.thread)
        _stop_thread(self.prepare_thread)
        self.thread = None
        self.worker = None
        self.prepare_thread = None
        self.prepare_worker = None
        # A running browser sign-in is left alone on purpose: its thread is
        # unparented and self-terminates after its timeout, and joining it
        # here could freeze the close for minutes.
        self.login_thread = None
        self.login_worker = None
        self._save_visible_account()
        event.accept()

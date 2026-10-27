"""Hauptfenster, Einstellungen und der sichtbare Sync-Ablauf."""
import os

from .ui_common import (CANCEL_MESSAGE, QComboBox, QDialog,
                        QFormLayout, QHBoxLayout, QInputDialog,
                        QLabel, QLineEdit, QMessageBox, QProgressBar,
                        QPushButton, QThread, QVBoxLayout, _apply_busy,
                        _dialog_busy, _discard_item,
                        _login_failure_guidance,
                        _stop_thread, _thread_running, _worker_account_name,
                        _worker_outcome,
                        sync_result_detail,
                        sync_result_text)
from calibre.gui2 import error_dialog, info_dialog
from .client import make_client
from .config import (CredentialStore, PREFERRED_FORMATS, account_by_name,
                     save_account, save_credentials, save_settings,
                     set_active_account, settings)
from .library import (COLUMN_KIND_NAMES, CUSTOM_COLUMNS,
                      create_custom_columns, ignore_column_available,
                      iter_book_ids, metadata_by_id, read_column_available,
                      safe_format_path, tolino_column_active,
                      update_read_column, update_tolino_ids)
from .config import load_state
from .config import settings as _global_settings

from . import dashboard_plan as _dashboard_plan
from .partners import PARTNERS
from .tolino import (RELOGIN, hardware_id, log_warning,
                     normalize_refresh_token, report_error, sanitize_error)
from .cdp import devtools_port_alive
from .ui_dialogs import InventoryDialog
from .ui_workers import PrepareWorker, SyncWorker
from .ui_auth_workers import (BrowserLoginWorker, SessionCheckWorker,
                              _LOGIN_WORKER_THREADS)
from .ui_settings import SettingsDialog

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
    try:
        primary.setDefault(True)
        primary.setFocus()
    except Exception:
        pass  # Test-Stub oder bereits geschlossenes Fenster


def _persist_sync_result(dialog, state, refresh, updates,
                         always_refresh=False):
    """Ergebnis speichern und die optionalen Calibre-Spalten aktualisieren."""
    _persist_run(dialog, state, refresh, updates,
                 always_refresh=always_refresh)
    if dialog.sync_column_enabled:
        update_tolino_ids(
            dialog.gui.current_db, updates, enabled=True,
            account_name=_worker_account_name(dialog))
    read_updates = list(getattr(dialog.worker, "read_updates", None) or ())
    if read_updates and getattr(dialog, "read_column_enabled", False):
        try:
            update_read_column(dialog.gui.current_db, read_updates)
        except Exception as exc:
            # Ein misslungener Spalten-Schreibvorgang darf das Ergebnis des
            # Laufs nicht verschlucken.
            log_warning("Lesestatus konnte nicht in #tolino_read "
                        "geschrieben werden: %s" % sanitize_error(exc))


def _persist_run(dialog, state, refresh, updates, always_refresh=False):
    """Ergebnis eines Laufs unter dem KONTO DES LAUFS sichern.

    Ein Speicherfehler darf das Ergebnis nie verschlucken: ohne diesen
    Schutz bliebe der Abschlussdialog aus, und ein erfolgreicher Upload
    sähe aus wie «nichts passiert». UI-Felder werden nur überschrieben,
    solange dieses Konto noch angezeigt wird.
    """
    account_name = _worker_account_name(dialog)
    worker = dialog.worker
    hardware = ""
    try:
        if worker is not None:
            hardware = str((getattr(worker, "account", {}) or {}).get(
                "hardware_id") or "")
        if account_name and not hardware:
            hardware = str(account_by_name(account_name).get("hardware_id") or "")
        if refresh and account_name:
            save_credentials(account_name, refresh, hardware)
        if account_name and dialog.account_name == account_name:
            if always_refresh or (refresh and
                                  refresh != dialog.refresh.text().strip()):
                dialog.set_refresh_token(refresh)
        if account_name:
            save_account(account_name, {
                "state": state, "refresh_token": refresh,
                "hardware_id": hardware or hardware_id(),
            }, active=account_name)
    except Exception as exc:
        log_warning("Ergebnis der Synchronisierung konnte nicht "
                    "gespeichert werden: %s"
                    % sanitize_error(exc, (refresh,)))


def _register_login_thread(dialog, thread, worker):
    """Anmelde-Thread starten und am Leben halten.

    Ohne Elternteil: ein bereits geschlossenes Fenster darf den laufenden
    Thread nicht zerstören. Die Liste hält die Referenz, bis der Thread
    fertig ist -- sonst räumt ihn der Garbage Collector vorzeitig ab.
    """
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(
        lambda: _discard_item(_LOGIN_WORKER_THREADS, thread))
    _LOGIN_WORKER_THREADS.append(thread)
    dialog.login_thread = thread
    dialog.login_worker = worker
    thread.start()


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
    def __init__(self, gui):
        QDialog.__init__(self, gui)
        self.gui = gui
        self.thread = None
        self.worker = None
        self.login_thread = None
        self.login_worker = None
        self.prepare_thread = None
        self.prepare_worker = None
        self.sync_column_enabled = False
        self.read_column_enabled = False
        self.account_name = ""
        self.temp_files = []
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
        self.refresh.setEchoMode(QLineEdit.Password)
        self.refresh.setPlaceholderText("Wird bei der Anmeldung gesetzt")
        accounts.addRow("Aktives Konto", self.account_select)
        accounts.addRow("", account_buttons)
        accounts.addRow("Geräte-ID (automatisch)", self.hardware)
        accounts.addRow("Zugangstoken (nur bei Problemen ändern)", self.refresh)

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
            self.sync_column_enabled = tolino_column_active(
                _global_settings(), database)
            self.read_column_enabled = read_column_available(database)
        except Exception as exc:
            log_warning("Spaltenstatus konnte nicht gelesen werden: %s"
                        % sanitize_error(exc))
        self.update_status()

    def _save_visible_account(self, values=None):
        """Das sichtbare Konto und die globalen Optionen EINMAL einlesen.

        Vorher lasen save_settings() und save_account() dieselben Widgets
        getrennt (inklusive zweimaliger Format-Liste). `values()` ist die
        eine Quelle; die globalen Schluessel stehen in config.
        """
        if not getattr(self, "account_name", None):
            return
        values = values or self.values()
        save_settings({PREFERRED_FORMATS: values[PREFERRED_FORMATS]})
        save_account(self.account_name, values, active=self.account_name)

    def account_changed(self, index):
        """Konto in den Einstellungen gewechselt."""
        if index < 0 or not getattr(self, "account_names", None):
            return
        self.switch_account(self.account_select.itemData(index))

    def quick_account_changed(self, index):
        """Konto auf der ersten Seite gewechselt."""
        if index < 0 or not getattr(self, "account_names", None):
            return
        self.switch_account(self.account_quick.itemData(index))

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
        answer = QMessageBox.question(
            self, "Konto entfernen",
            "Konto «%s» entfernen? Anmeldung und Sync-Stand dieses Kontos "
            "werden gelöscht. Bücher in tolino und Calibre bleiben." % name,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        remaining = [item for item in settings()["accounts"] if item["name"] != name]
        save_settings({"accounts": remaining,
                       "active_account": remaining[0]["name"]})
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
        self.refresh.setText(values["refresh_token"])
        # Vergleichsstand fuer values(): nur was der Nutzer WIRKLICH aendert,
        # darf den gespeicherten Zugang ersetzen. Ein im Hintergrund
        # rotierter Token liegt nicht im Feld und wuerde sonst von der alten
        # Kopie ueberschrieben.
        self._loaded_refresh = normalize_refresh_token(self.refresh.text())
        self._loaded_hardware = self.hardware.text().strip()
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
        if self.thread is not None or getattr(self, "prepare_thread", None) is not None:
            self.cancel_sync()
        else:
            self.start_sync()

    def update_status(self):
        logged_in = bool(self.refresh.text().strip())
        running = _dialog_busy(self)
        if running:
            # Sperren und «… läuft»-Text kommen aus dem gemeinsamen Helfer;
            # hier bleibt nur die Auswahl der Zweige.
            _apply_busy(self, True)
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

    def set_refresh_token(self, refresh):
        normalized = normalize_refresh_token(refresh)
        self.refresh.setText(normalized)
        # Frisch gelesen heisst gewollt: der Wert ist jetzt der Vergleichsstand
        # fuer values().
        self._loaded_refresh = normalized
        self.update_status()

    def persist_refresh_token(self, refresh):
        """Rotierten Token speichern (derselbe Weg wie im Worker)."""
        self.set_refresh_token(refresh)
        save_credentials(self.account_name, self.refresh.text(),
                         self.hardware.text().strip() or hardware_id())

    def _selected_account(self):
        """The account this dialog shows – never a different 'active' one."""
        return account_by_name(getattr(self, "account_name", None))

    def values(self):
        refresh_token = normalize_refresh_token(self.refresh.text())
        account = self._selected_account() or settings()
        if refresh_token == getattr(self, "_loaded_refresh", None):
            # Unveraendertes Feld: es gilt der GESPEICHERTE Zugang. Hat der
            # Keep-alive im Hintergrund rotiert, wuerde die alte Kopie sonst
            # beim Speichern oder beim Sync die Rotation verbrennen.
            stored_token = normalize_refresh_token(
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
            "preferred_formats": ([
                value for value in dict.fromkeys(
                    x.strip().upper() for x in self.formats.text().split(",")
                    if x.strip()) if value in ("EPUB", "PDF")
            ] or ["EPUB"]),
            "state": (account.get("state")
                      if isinstance(account.get("state"), dict) else {}),
        }

    def _login_running(self):
        """Läuft gerade eine Anmeldeprüfung oder ein Anmeldefenster?"""
        return _thread_running(getattr(self, "login_thread", None))

    def browser_login(self):
        """Anmeldeknopf: erst die gespeicherte Anmeldung prüfen.

        Ein neues Anmeldefenster ist nur nötig, wenn die vorhandene Anmeldung
        nicht mehr funktioniert. Deshalb läuft zuerst eine stille Prüfung im
        Hintergrund; das Fenster geht erst auf, wenn tolino die Anmeldung
        ablehnt. Ohne gespeicherte Anmeldung gibt es nichts zu prüfen.
        """
        if self._login_running():
            return  # eine Prüfung oder Anmeldung läuft bereits
        settings = self.values()
        if settings["refresh_token"]:
            self._check_stored_login(settings)
            return
        self._start_login_window(settings)

    def _check_stored_login(self, settings):
        """Gespeicherte Anmeldung im Hintergrund prüfen."""
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText("Anmeldung wird geprüft …")
        thread = QThread()  # no parent: dialog may close first
        worker = SessionCheckWorker(settings)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        for signal in (worker.valid, worker.invalid, worker.busy):
            signal.connect(thread.quit)
        worker.valid.connect(self.login_still_valid)
        worker.invalid.connect(self.login_rejected)
        worker.busy.connect(self.login_check_busy)
        _register_login_thread(self, thread, worker)

    def login_still_valid(self, refresh):
        """Prüfung bestanden: Anmeldung bleibt gültig."""
        try:
            self._login_cleanup()
            self.persist_refresh_token(refresh)
            self.status.setText("Angemeldet.")
            _offer_next_step(self)
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def login_rejected(self, message=""):
        """Prüfung fehlgeschlagen: jetzt das Anmeldefenster öffnen.

        Der technische Grund landet im Log statt im Fenster: die Box bleibt
        eine Zeile und die Fehlermeldung knapp.
        """
        try:
            self._login_cleanup()
            settings = self.values()
        except RuntimeError:
            return  # dialog already destroyed (Calibre shutdown)
        log_warning("Anmeldung abgelehnt: " + str(message))
        self.status.setText("Anmeldung abgelaufen – bitte neu anmelden.")
        self._start_login_window(settings)

    def login_check_busy(self, _reason=""):
        """Anmeldung wird gerade geprüft; keine zweite Prüfung starten."""
        try:
            self._login_cleanup()
            self.status.setText("Die Anmeldung wird gerade erneuert, bitte kurz warten.")
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def _start_login_window(self, settings):
        """Das eigene Anmeldefenster öffnen und die Anmeldung übernehmen."""
        if self._login_running():
            return  # a sign-in attempt is already running
        partner_id = settings["partner_id"]
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText(
            "Bitte im Browserfenster anmelden, bis die Bücherliste erscheint.")
        thread = QThread()  # no parent: dialog may close first
        worker = BrowserLoginWorker(partner_id, settings["hardware_id"])
        worker.moveToThread(thread)
        worker.progress.connect(self._login_progress)
        thread.started.connect(worker.run)
        worker.completed.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.completed.connect(self.login_completed)
        worker.failed.connect(self.login_failed)
        _register_login_thread(self, thread, worker)

    def login_completed(self, refresh, hardware):
        try:
            self._login_cleanup()
            self.set_refresh_token(refresh)
            if hardware:
                self.hardware.setText(str(hardware))
            self.persist_refresh_token(refresh)
            self.update_status()
            # Erfolg steht in der Statuszeile -- kein zusaetzlicher modaler
            # Dialog fuer den Normalfall.
            self.status.setText("Angemeldet.")
            # Der Grabber schliesst sein Fenster bereits. Die UI fragt nur
            # den Zustand ab; ein fehlender Besitzsatz heisst nicht «offen».
            if devtools_port_alive():
                self.status.setText(
                    "Angemeldet. Das Anmeldefenster ist noch offen.")
            _offer_next_step(self)
        except RuntimeError:
            pass  # dialog already destroyed (Calibre shutdown)

    def login_failed(self, error):
        """Fehlgeschlagene Anmeldung: Ursache + naechster Schritt nennen."""
        try:
            self._login_cleanup()
            # Technische Details (inkl. Traceback) nur ins Log.
            # Der sichtbare Zugang ist ein bekanntes Geheimnis: er darf
            # weder im Dialog noch im Log auftauchen.
            refresh = getattr(self, "refresh", None)
            try:
                token = str(refresh.text()) if refresh is not None else ""
            except Exception:
                token = ""
            secrets = (token,) if token else ()
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
            _apply_busy(self, False)
            self.update_status()
        except RuntimeError:
            pass  # dialog already destroyed

    @staticmethod
    def _worker_client(settings):
        """Client aus einem GUI-seitig erfassten Snapshot bauen."""
        store = CredentialStore(settings, settings.get("account_name"))
        return make_client(store)

    def _show_prepare_error(self, exc, settings):
        """Nur den kurzen Fehlertext zeigen; technische Details protokollieren."""
        secrets = (settings.get("refresh_token"),)
        message = report_error(exc, "Vorbereitung", secrets)
        error_dialog(self, "Vergleich fehlgeschlagen",
                     message + " Es wurde nichts geändert.", show=True)

    def start_sync(self):
        """Bibliothek vergleichen: lokaler Snapshot im GUI, Netz im Worker.

        Der Calibre-Datenbankzugriff (Metadaten + Format-Signaturen) bleibt
        im GUI-Thread. Login und Cloud-Inventar laufen in `PrepareWorker`;
        der Vergleich und die Auswahl kommen über Qt-Signale zurück.
        """
        if self.thread is not None or getattr(self, "prepare_thread", None) is not None:
            return
        settings = self.values()
        if not settings["refresh_token"]:
            status = getattr(self, "status", None)
            if status is not None:
                status.setText("Nicht angemeldet.")
            return
        self._save_visible_account(settings)
        self.sync_column_enabled = tolino_column_active(
            _global_settings(), self.gui.current_db)
        settings["sync_column_enabled"] = self.sync_column_enabled
        self.read_column_enabled = read_column_available(self.gui.current_db)
        settings["read_column_enabled"] = self.read_column_enabled
        try:
            # Calibre-DB und Qt-Widgets nur hier (GUI-Thread) lesen.
            metadata = self._local_metadata()
        except Exception as exc:
            self._show_prepare_error(exc, settings)
            return
        self._start_prepare_worker(settings, metadata)

    def _start_prepare_worker(self, settings, metadata):
        """Netz-Vorbereitung im Hintergrund starten (GUI bleibt bedienbar)."""
        _apply_busy(self, True)
        self.status.setText("Vergleich läuft …")
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_label.setText("Verbinde mit tolino …")
        thread = QThread(self)
        worker = PrepareWorker(settings, metadata)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._prepare_progress)
        worker.ready.connect(self._prepare_ready)
        worker.failed.connect(self._prepare_failed)
        worker.ready.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(thread.deleteLater)
        self.prepare_thread = thread
        self.prepare_worker = worker
        self.update_status()
        thread.start()

    def _prepare_progress(self, text):
        try:
            self.progress_label.setText(str(text))
        except RuntimeError:
            pass  # dialog already destroyed

    def _finish_prepare(self):
        """Vorbereitungs-Thread sauber abräumen (ohne unbegrenzt zu warten)."""
        thread = getattr(self, "prepare_thread", None)
        self.prepare_thread = None
        self.prepare_worker = None
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setText("Bereit")
        self.progress_label.setVisible(False)
        _stop_thread(thread, timeout_ms=2000)
        _apply_busy(self, False)
        self.update_status()

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

    def _prepare_ready(self, payload):
        """Vergleich anzeigen (GUI-Thread) und den Sync starten."""
        account_name = str(payload.get("account_name") or "")
        settings = payload["settings"]
        metadata = payload["metadata"]
        state = payload["state"]
        self._finish_prepare()
        # Token/Hardware nur dann in die Felder schreiben, wenn dieses Konto
        # noch angezeigt wird. Gespeichert wurde bereits unter dem Konto.
        refresh = payload.get("refresh")
        if refresh:
            # Der SyncWorker startet mit DIESEM Dict. Ohne die Uebernahme
            # wuerde er den schon verbrauchten Token aus der Vorbereitung
            # erneut einloesen (A02).
            settings["refresh_token"] = refresh
            if self.account_name == account_name:
                self.set_refresh_token(refresh)
            save_credentials(account_name, refresh,
                             payload.get("hardware") or settings["hardware_id"])
        hardware = getattr(self, "hardware", None)
        if payload.get("hardware"):
            settings["hardware_id"] = str(payload["hardware"])
        if (payload.get("hardware") and hardware is not None
                and self.account_name == account_name):
            hardware.setText(str(payload["hardware"]))
            self._loaded_hardware = hardware.text().strip()
        if payload.get("read_status_known") is False:
            # Der Dialog zeigt denselben Hinweis in der Tabelle.
            log_warning("Lesestatus unbekannt – er bleibt unverändert.")
        try:
            selection = self._confirm_comparison(
                payload["comparison"], settings,
                client_factory=lambda: self._worker_client(settings),
                automatic=self._automatic_action_count(settings, metadata, state))
        except Exception as exc:
            # Ein Fehler NACH dem Vergleichsfenster darf nicht als Traceback
            # im Slot verschwinden: der Nutzer sähe nur ein geschlossenes
            # Fenster und keinen einzigen Upload.
            self._cleanup_temp_files()
            self._show_prepare_error(exc, settings)
            return
        if selection is None:
            return
        try:
            preparation = self._plan_actions(settings, metadata, state, selection)
            if preparation["removals"] and not self._confirm_deletions(
                    preparation["removals"], state,
                    preparation.get("removal_titles")):
                self._cleanup_temp_files()
                return
            self._start_sync_worker(settings, preparation, state)
        except Exception as exc:
            # `_plan_actions` legt Cover-Dateien an, bevor es fertig ist.
            # Ein Fehler dort darf sie nicht bis zum naechsten Lauf liegen
            # lassen -- der fruehe Rueckweg raeumt sie mit auf.
            self._cleanup_temp_files()
            self._show_prepare_error(exc, settings)

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
        answer = QMessageBox.question(
            self, "Bücher aus tolino löschen?",
            ("1 Buch wird aus tolino gelöscht (in Calibre bleibt es):\n\n%s"
             if len(removals) == 1 else
             "%d Bücher werden aus tolino gelöscht (in Calibre bleiben sie):\n\n%s")
            % (("\n".join(lines),) if len(removals) == 1 else
               (len(removals), "\n".join(lines))),
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _local_metadata(self):
        """Calibre-Metadaten und benötigte Format-Signaturen lesen."""
        return _dashboard_plan.local_metadata(
            self, iter_books=iter_book_ids, metadata_for=metadata_by_id)

    def _automatic_action_count(self, settings, metadata, state):
        """Serienänderungen zählen, die ohne Auswahl ausgeführt werden.

        Der Vergleich darf ohne Auswahl nur schliessen, wenn diese Zahl
        über null liegt.
        """
        return _dashboard_plan.automatic_action_count(settings, metadata, state)

    def _confirm_comparison(self, comparison, settings, client_factory=None,
                            automatic=0):
        """Bestätigungsfenster zeigen; None, wenn es abgebrochen wurde."""
        dialog = InventoryDialog(
            comparison, self,
            client_factory=(client_factory or
                            (lambda: self._worker_client(settings))),
            options={
                "collections": True,
                "automatic": int(automatic or 0),
                # Für den Hinweis im Fenster: ohne diese Spalte bleiben
                # gekaufte Bücher Upload-Kandidaten.
                "ignore_column": ignore_column_available(
                    getattr(getattr(self, "gui", None), "current_db", None)),
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
            "read_sync": dialog.selected_read_sync(),
            "cloud_deletions": dialog.selected_cloud_deletions(),
        }

    def _plan_actions(self, settings, metadata, state, selection):
        """Aus der Auswahl die Jobs und den neuen Sync-Stand bauen."""
        return _dashboard_plan.plan_actions(
            self, settings, metadata, state, selection,
            upload_builder=self._upload_jobs,
            safe_format_lookup=safe_format_path)

    def _upload_jobs(self, settings, metadata, uploads):
        """Upload-Jobs bauen; fehlende Dateien landen in `skipped_uploads`."""
        return _dashboard_plan.upload_jobs(
            self, settings, metadata, uploads, warning=log_warning,
            safe_format_lookup=safe_format_path)

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
        answer = QMessageBox.question(
            self, "Cloud-Kopie ersetzen?",
            text,
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return answer == QMessageBox.Yes

    def _report_skipped_uploads(self, skipped_uploads, nothing_else=False):
        """Bücher ohne verwendbare Datei melden; True heisst «nichts zu tun».

        `nothing_else` sagt, dass ausser diesen übersprungenen Uploads keine
        Aktion mehr übrig ist: dann endet der Lauf hier, und die Meldung sagt
        das auch.
        """
        QMessageBox.warning(
            self, "Einige Bücher wurden nicht hochgeladen",
            "Diese Bücher haben keine Datei im gewählten Format:\n\n%s"
            % "\n".join(skipped_uploads))
        return bool(nothing_else)

    def _start_sync_worker(self, settings, preparation, state=None):
        """Übersprungene Uploads melden und den Hintergrundlauf starten."""
        skipped_uploads = preparation["skipped_uploads"]
        jobs = preparation["jobs"]
        if state is None:
            state = load_state(settings.get("state"))
        # Das Vergleichsfenster kann beliebig lange offen bleiben; in der
        # Zeit darf der Keep-alive den (single-use) Token rotieren. Deshalb
        # unmittelbar vor dem Start den GESPEICHERTEN Zugang des Kontos
        # nehmen -- sonst loest der Worker einen verbrauchten Token ein
        # (A02). Das schliesst die Luecke auch dann, wenn eine
        # Zaehlung einmal nicht greift.
        CredentialStore(settings, settings.get("account_name")).freshest()
        total = _dashboard_plan.planned_actions(preparation, len(jobs), state)
        if not total and not skipped_uploads:
            # Kein stiller Lauf: ohne ausführbare Aktion gibt es eine
            # Antwort statt eines Hintergrundstarts.
            # Calibre zeigt ein info_dialog nur mit show=True an; ohne das
            # Argument entsteht das Fenster und bleibt unsichtbar.
            self._cleanup_temp_files()
            info_dialog(self, "Nichts zu tun",
                        _dashboard_plan.nothing_to_do_text(preparation),
                        show=True, show_copy_button=False)
            return
        if skipped_uploads and self._report_skipped_uploads(
                skipped_uploads, nothing_else=not total):
            self._cleanup_temp_files()
            return
        replacements = [job for job in jobs
                        if getattr(job, "old_id", None)]
        if replacements and not self._confirm_replacements(replacements):
            self._cleanup_temp_files()
            status = getattr(self, "status", None)
            if status is not None:
                status.setText("Abgebrochen. Die Cloud-Kopie bleibt unverändert.")
            return
        if skipped_uploads:
            info_dialog(self, "Nicht hochgeladen",
                        "Diese Bücher wurden nicht hochgeladen: "
                        + "; ".join(skipped_uploads), show=True,
                        show_copy_button=False)
        plan = _dashboard_plan.plan_text(preparation, len(jobs), state)
        # Der Plan steht im Log, auch wenn der Lauf danach scheitert;
        # Zugangsdaten werden dabei nicht ausgegeben.
        log_warning("Sync geplant: %s" % plan)
        _apply_busy(self, True)
        self.progress.setRange(0, total)
        self.progress.setValue(0)
        self.thread = QThread(self)
        settings["metadata"] = preparation.get("metadata", {})
        self.worker = SyncWorker(settings, jobs, preparation["removals"],
                                 preparation["meta_updates"],
                                 preparation["series_map"],
                                 preparation["selected_read"],
                                 preparation["selected_unread"],
                                 titles=preparation.get("titles"),
                                 read_sync=preparation.get("read_sync"))
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self.progress_changed)
        self.worker.completed.connect(self.sync_completed)
        self.worker.failed.connect(self.sync_failed)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()
        self.progress.setValue(0)
        # Der Plan steht sofort im Fenster: der Nutzer sieht, dass der Lauf
        # begonnen hat und was er tut.
        self.progress_label.setText(plan)
        self.update_status()

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

    def sync_completed(self, state, refresh, updates):
        """Erfolgreicher Lauf (auch nach regulaerem Abbruch).

        Persistiert wird unter dem KONTO DES LAUFS. UI-Felder werden nur
        ueberschrieben, solange dieses Konto noch angezeigt wird.
        """
        outcome = _worker_outcome(self)
        _persist_sync_result(self, state, refresh, updates)
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

    def sync_failed(self, message, refresh):
        outcome = _worker_outcome(self)
        worker = self.worker
        cancelled = bool(getattr(worker, "cancelled", False)) or \
            str(message) == CANCEL_MESSAGE
        # Auch ein Fehlerlauf hat evtl. schon Uploads abgeschlossen: deren
        # tolino-IDs in die Spalte schreiben, niemals für ein anderes Konto.
        _persist_sync_result(
            self, getattr(worker, "state", {}), refresh,
            getattr(worker, "updates", ()), always_refresh=True)
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

    def _cleanup_temp_files(self):
        """Temporäre Cover-Dateien löschen -- auch auf frühen Rückwegen.

        Die Dateien entstehen in `_plan_actions`; stand die Aufraeumung nur
        im Abschluss, blieben sie bei «Nichts zu übertragen» oder einer
        abgebrochenen Löschbestätigung bis zum nächsten Lauf liegen
.
        """
        for path in getattr(self, "temp_files", ()):
            try:
                os.remove(path)
            except OSError:
                pass
        self.temp_files = []

    def finish_thread(self):
        self._save_visible_account()
        thread = self.thread
        self.thread = None
        self.worker = None
        if thread is not None:
            _stop_thread(thread)
        self.progress_label.setText("Bereit")
        self._cleanup_temp_files()
        _apply_busy(self, False)
        self.update_status()

    def closeEvent(self, event):
        """Schliessen: Abbruch anfordern, aber nie unbegrenzt blockieren."""
        if self.thread is not None or getattr(self, "prepare_thread", None) is not None:
            self.cancel_sync()
        _stop_thread(self.thread)
        _stop_thread(getattr(self, "prepare_thread", None))
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

"""Hauptfenster, Einstellungen und der sichtbare Sync-Ablauf."""
from qt.core import QDialog, QInputDialog, QMessageBox, QThread, Qt
from .ui_common import (CANCEL_MESSAGE, COMPARE_BUTTON_TEXT, COMPARE_BUTTON_TIP,
                        LOGIN_BUTTON_TEXT, LOGIN_BUTTON_TIP,
                        LOGIN_NEXT_STEP, _active_work_thread, _apply_busy,
                        _dialog_busy, _login_failure_guidance, _stop_thread,
                        gui_guard, start_worker)
from calibre.gui2 import error_dialog, info_dialog
from .token_vault import TOKEN_VAULT
from .config import (PREFERRED_FORMATS, account_by_name, account_summary,
                     load_state, remove_account as remove_saved_account,
                     save_account, save_credentials,
                     set_active_account, settings)
from .run_context import RunContext
from .library import (TOLINO_IGNORE_COLUMN, bool_column_available,
                      column_state, library_scope, normalize_formats)
from .sync_results import (finish_sync, mirror_account_reading,
                           mirror_comparison_reading, report, report_sync)

from . import planning as _planning
from .errors import needs_new_login
from .partners import UNKNOWN_SHOP, partner_from_token
from .tolino import (BUSY_MESSAGE, RELOGIN, TolinoError, hardware_id, log_info,
                     log_warning, normalize_hardware_id,
                     normalize_refresh_token, report_error, sanitize_error)
from .ui_auth_workers import BrowserLoginWorker, SessionCheckWorker
from .ui_dashboard_parts import (DashboardParts, confirm_deletions,
                                 confirm_replacements, offer_next_step,
                                 report_skipped_uploads)
from .ui_dialogs import InventoryDialog
from .ui_workers import LocalMetadataWorker, PrepareWorker, SyncWorker


# Statuszeile nach einem abgelehnten Zugang (A01/B01/B02).
NEEDS_LOGIN_STATUS = "Anmeldung abgelehnt – bitte neu anmelden."


class SyncDashboard(DashboardParts, QDialog):
    def __init__(self, gui):
        QDialog.__init__(self, gui)
        self.gui = gui
        # Ein Plugin-Fenster darf beim Schliessen nicht die Anwendung beenden.
        self.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
        self.thread = None
        self.worker = None
        self.login_thread = None
        self.login_worker = None
        self.prepare_thread = None
        self.prepare_worker = None
        self.snapshot_thread = None
        self.snapshot_worker = None
        self.account_name = ""
        self.setWindowTitle("tolino Cloud Sync")
        self.setMinimumWidth(560)
        self._build_ui()
        self._hardware_edited = False
        self.hardware.textEdited.connect(self._mark_hardware_edited)
        self.load_values()

    def _save_visible_account(self, values=None):
        """Das sichtbare Konto und die globalen Optionen EINMAL einlesen.

        Vorher lasen zwei getrennte Pfade dieselben Widgets (inklusive
        zweimaliger Format-Liste). `values()` ist die eine Quelle; die
        globalen Schluessel stehen in config. Der Zustand fehlt bewusst:
        `save_account()` merged ihn aus dem Konto, statt ihn zu ersetzen.
        """
        if not self.account_name:
            return True
        values = dict(values or self.values())
        name = str(values.get("account_name") or "")
        if not name:
            # Fehlende Konten dürfen keinen fremden Zustand überschreiben.
            log_warning("Das sichtbare Konto existiert nicht mehr; "
                        "Änderungen wurden nicht gespeichert.")
            return True
        # Ein unverändertes Feld darf eine vom Worker übernommene Geräte-ID
        # nicht mit einem veralteten Anzeigewert überschreiben.
        if not self._hardware_edited:
            values.pop("hardware_id", None)
        # ``False`` statt Ausnahme: der Speicher ist unlesbar oder das Konto
        # fehlt. Ein Aufrufer, der den Zustand braucht, muss abbrechen.
        if not save_account(name, values, active=name,
                            preferred_formats=values[PREFERRED_FORMATS]):
            log_warning("Die Einstellungen konnten nicht gespeichert werden.")
            return False
        self._hardware_edited = False
        return True

    def _save_failed(self, title):
        """Meldung, wenn die Einstellungsdatei nicht geschrieben wurde."""
        error_dialog(
            self, title,
            "Die Einstellungsdatei konnte nicht geschrieben werden. Die "
            "Eingaben bleiben stehen; bitte erneut versuchen.", show=True)

    def _save_settings(self):
        """Sichtbare Einstellungen sichern; bei Fehler das Schliessen verhindern."""
        if self._save_visible_account():
            return True
        self._save_failed("Einstellungen nicht gespeichert")
        return False

    def _mark_hardware_edited(self, _text=""):
        """Merken, dass die Geräte-ID von Hand geändert wurde."""
        self._hardware_edited = True

    def _account_selected(self, index, combo):
        """Gewähltes Konto übernehmen -- ein Weg für beide Comboboxen."""
        if index < 0 or not self.account_names:
            return
        self.switch_account(combo.itemData(index))

    def _restore_account_choice(self):
        """Beide Kontoauswahlen zurückstellen -- ohne Signal.

        Die Felder werden bewusst NICHT neu geladen: ungespeicherte Eingaben
        bleiben so stehen.
        """
        for widget in (getattr(self, "account_select", None),
                       getattr(self, "account_quick", None)):
            if widget is None:
                continue
            try:
                widget.blockSignals(True)
                widget.setCurrentIndex(
                    max(0, widget.findData(self.account_name)))
            finally:
                widget.blockSignals(False)

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
        if name not in self.account_names:
            return False
        # Eine laufende Anmeldeprüfung bleibt an ihr Startkonto gebunden.
        login = self.login_thread
        if login is not None and login.isRunning():
            try:
                self.load_values()
            except Exception:
                pass
            return False
        if not self._save_settings():
            # Ein nicht gesicherter Stand darf keinen Kontowechsel auslösen:
            # sonst überschreibt der nächste Schreibvorgang ihn endgültig.
            # Die Auswahl geht zurück; die Felder bleiben unangetastet.
            self._restore_account_choice()
            return False
        if not set_active_account(name):
            self._save_failed("Konto nicht gewechselt")
            self.load_values()
            return False
        self.load_values()
        return True

    def new_account(self):
        if not self._save_settings():
            return
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
        if not save_account(name, {"name": name}, active=name):
            self._save_failed("Konto nicht angelegt")
            return
        self.load_values()
        self.account_select.setCurrentIndex(self.account_select.findData(name))

    def remove_account(self):
        if len(self.account_names) <= 1:
            return
        name = self.account_name
        yes = QMessageBox.StandardButton.Yes
        no = QMessageBox.StandardButton.No
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
        # Der Buchhändler ist reine Anzeige: gesetzt wird er beim Anmelden
        # aus dem Token; die Auswahl in den Einstellungen ist nur Rückfall.
        self.partner.blockSignals(True)
        self.partner.setCurrentIndex(
            max(0, self.partner.findData(values["partner_id"])))
        self.partner.blockSignals(False)
        self._partner_chosen = False
        self.hardware.setText(values["hardware_id"] or hardware_id())
        self._hardware_edited = False
        # Das leere Feld darf einen rotierten Token nicht überschreiben.
        self.refresh.setText("")
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.update_status()
        mirror_account_reading(self, self.account_name)

    def open_settings(self):
        """Einstellungen öffnen – speichert beim Schliessen."""
        self.settings_dialog.exec()

    def close_settings(self):
        """Einstellungen schliessen -- ohne den eingetippten Token zu verlieren."""
        typed = self._typed_refresh()
        if not self._save_settings():
            return
        # Ein eingetippter Zugangstoken ist eine Eingabe wie jede andere:
        # «Speichern und schliessen» darf ihn nicht still verwerfen. Ohne
        # erkannten Buchhändler bleibt das Fenster offen, damit die Eingabe
        # nicht verloren geht -- die Meldung nennt den Grund.
        if typed and self.account_name and not self.store_refresh(typed):
            return
        self.settings_dialog.accept()

    def primary_action(self):
        """Hauptknopf: vergleichen – oder einen laufenden Sync abbrechen.

        Das Anmelden hat wieder seinen eigenen Knopf darüber,
        damit es auch bei abgelaufenem Zugang sofort erreichbar ist.
        """
        if _active_work_thread(self):
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
            elif self.snapshot_thread is not None:
                running_text = "Bibliothek wird gelesen …"
            else:
                running_text = "Anmeldung wird geprüft …"
            self.status.setText(running_text)
        elif logged_in:
            self.status.setText("Angemeldet.")
        else:
            self.status.setText("Nicht angemeldet.")
        self.progress.setVisible(running)
        self.progress_label.setVisible(running)
        login_button = self.login_button
        if login_button is not None:
            if logged_in:
                login_button.setText("Anmeldung prüfen")
                login_button.setToolTip(
                    "Prüft die gespeicherte Anmeldung. Öffnet den Browser nur bei Bedarf.")
            else:
                login_button.setText(LOGIN_BUTTON_TEXT)
                login_button.setToolTip(LOGIN_BUTTON_TIP)
        if running:
            self.primary.setText("Abbrechen")
            self.primary.setToolTip(
                "Bereits übertragene Änderungen bleiben bestehen.")
            self.primary.setEnabled(True)
        else:
            _apply_busy(self, False)
            self.primary.setText(COMPARE_BUTTON_TEXT)
            self.primary.setEnabled(logged_in)
            self.primary.setToolTip(
                COMPARE_BUTTON_TIP if logged_in
                else "Noch nicht angemeldet. " + RELOGIN)

    def _stored_refresh(self):
        """Gespeicherter Zugang des ANGEGEZEIGTEN Kontos -- nie ein anderes."""
        account = self._selected_account() or {}
        return normalize_refresh_token(account.get("refresh_token") or "")

    def _typed_refresh(self):
        """Von Hand eingetippter Token; leer heisst «nichts ersetzen»."""
        refresh = self.refresh
        return normalize_refresh_token(refresh.text()) if refresh is not None else ""

    def _effective_refresh(self):
        """Der Token, mit dem gearbeitet wird: eingetippt vor gespeichert."""
        return self._typed_refresh() or self._stored_refresh()

    def _partner_selected(self, _index=None):
        """Buchhändler von Hand wählen – nur für Zugänge ohne ``iss``.

        Die Auswahl bleibt lokal: gespeichert wird sie erst zusammen mit
        einem Ersatz-Token über `store_refresh()`. So bleibt der vorhandene
        Thalia-Token eines Kontos unangetastet.
        """
        self._partner_chosen = True

    def store_refresh(self, refresh):
        """Token im Konto speichern -- der einzige Ort fuer Zugangsdaten.

        Der Buchhändler wird aus dem Token gelesen; nennt er keinen, zählt
        nur eine bewusste Auswahl in den Einstellungen. Sonst gibt es eine
        klare Meldung statt eines geratenen Shops.
        """
        token = normalize_refresh_token(refresh)
        if not token or not self.account_name:
            return False
        partner_id = partner_from_token(token) or (
            self.partner.currentData()
            if self._partner_chosen else None)
        if not partner_id:
            error_dialog(self, "Buchhändler nicht erkannt", UNKNOWN_SHOP,
                         show=True)
            return False
        hardware = ""
        if not self._hardware_edited:
            account = account_summary(self.account_name) or {}
            hardware = str(account.get("hardware_id") or "")
        try:
            stored = save_credentials(
                self.account_name, token,
                hardware or normalize_hardware_id(self.hardware.text())
                or hardware_id(),
                partner_id)
        except OSError:
            # Ein E/A-Fehler ist derselbe Fall wie ein Rückgabewert ``False``.
            stored = False
        if not stored:
            # Nicht gespeichert: die Eingabe bleibt stehen, damit sie nicht
            # verloren geht, und der Aufrufer meldet keinen Erfolg.
            error_dialog(self, "Token nicht gespeichert",
                         "Der Token konnte nicht gespeichert werden. Die "
                         "Einstellungsdatei ist möglicherweise nicht lesbar. "
                         "Bitte die Eingabe stehen lassen und erneut "
                         "versuchen.", show=True)
            return False
        self.refresh.setText("")
        self.update_status()
        return True

    def logout(self):
        """Nur den gespeicherten Zugang löschen – Konto und ID bleiben."""
        if not self.account_name:
            return
        if not save_account(self.account_name, {"refresh_token": ""},
                            active=self.account_name):
            # Nicht gesichert: der Token gilt weiter, das Feld bleibt stehen.
            self._save_failed("Abmeldung nicht gespeichert")
            return
        self.refresh.setText("")
        self.update_status()

    def _selected_account(self):
        """The account this dialog shows – never a different 'active' one."""
        return account_summary(self.account_name)

    def values(self):
        """Sichtbare Werte des Kontos -- nie Zugangsdaten.

        Der Token steht ausschliesslich im Konto (config). Ein eingetippter
        Wert wird von `store_refresh()` gespeichert; `save_account()` mischt
        nur die hier vorhandenen Schluessel und laesst ihn unberuehrt.
        Der Sync-Zustand gehoert ebenfalls nicht in diesen Snapshot: sonst
        wuerde ein spaeteres Speichern einen parallelen Worker-Fortschritt
        ueberschreiben.
        """
        # Ein fehlendes angezeigtes Konto darf kein anderes überschreiben.
        account = self._selected_account()
        if not isinstance(account, dict):
            account = {}
        # Eine getippte 32-Hex-ID gehört schon hier ins UUID-Format: sonst
        # stünde im Konto eine andere Form als die, mit der getauscht wird.
        hardware = normalize_hardware_id(self.hardware.text()) or hardware_id()
        formats = [fmt for fmt in normalize_formats(self.formats.text())
                   if fmt in ("EPUB", "PDF")]
        return {
            "account_name": str(account.get("name") or ""),
            "hardware_id": hardware,
            "preferred_formats": formats or ["EPUB"],
        }

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
        typed = self._typed_refresh()
        values = self.values()
        if typed:
            # Von Hand eingetippt: erst ins Konto speichern, dann pruefen.
            if not self.store_refresh(typed):
                return
            values = self.values()
        context = RunContext.from_values(values)
        if typed or self._stored_refresh():
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
        offer_next_step(self)

    @gui_guard
    def login_rejected(self, message=""):
        """Prüfung fehlgeschlagen: jetzt das Anmeldefenster öffnen.

        Der technische Grund landet im Log statt im Fenster: die Box bleibt
        eine Zeile und die Fehlermeldung knapp.
        """
        self._login_cleanup()
        context = RunContext.from_values(self.values())
        log_warning("Anmeldung abgelehnt: " + str(message))
        self.status.setText("Anmeldung abgelaufen – bitte neu anmelden.")
        self._start_login_window(context)

    @gui_guard
    def login_check_busy(self, _reason=""):
        """Anmeldung wird gerade geprüft; keine zweite Prüfung starten."""
        self._login_cleanup()
        self.status.setText(BUSY_MESSAGE)

    def _start_login_window(self, context):
        """Das eigene Anmeldefenster öffnen und die Anmeldung übernehmen."""
        if self._login_running():
            return  # a sign-in attempt is already running
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText(
            "Bitte im Browserfenster anmelden, bis die Bücherliste lädt.")
        worker = BrowserLoginWorker(
            context.account_name, self.hardware.text().strip())
        worker.progress.connect(self._login_progress)
        worker.completed.connect(self.login_completed)
        worker.failed.connect(self.login_failed)
        start_worker(self, worker, QThread(), "login_thread", "login_worker",
                     (worker.completed, worker.failed))

    @gui_guard
    def login_completed(self, hardware):
        """Der Worker hat den Token bereits im richtigen Konto gespeichert."""
        self._login_cleanup()
        if hardware:
            self.hardware.setText(str(hardware))
        self.refresh.setText("")
        self.status.setText("Angemeldet.")
        offer_next_step(self)

    @gui_guard
    def login_failed(self, error):
        """Fehlgeschlagene Anmeldung: Ursache + naechster Schritt nennen."""
        self._login_cleanup()
        # Technische Details nur bereinigt protokollieren.
        secrets = tuple(token for token in
                        (self._stored_refresh(), self._typed_refresh()) if token)
        detail = report_error(error, "Anmeldefenster", secrets)
        log_warning("Anmeldung nicht abgeschlossen: " + detail)
        code = error.code if isinstance(error, TolinoError) else "E99"
        title = "%s (Code %s)" % (_login_failure_guidance(error), code)
        self.status.setText("Anmeldung fehlgeschlagen.")
        report(self, "warnung", title, LOGIN_NEXT_STEP, "Anmeldefenster")

    @gui_guard
    def _login_progress(self, text):
        """Live-Hinweise des Browser-Grabs in der Statuszeile zeigen."""
        self.status.setText(str(text))

    @gui_guard
    def _login_cleanup(self):
        self.login_thread = None
        self.login_worker = None
        self.update_status()

    @staticmethod
    def _worker_client(context):
        """Client aus dem Lauf-Kontext bauen; der Vault liest den Token."""
        return TOKEN_VAULT.login(context.account_name)

    def _show_prepare_error(self, exc, context):
        """Nur den kurzen Fehlertext zeigen; technische Details protokollieren."""
        message = report_error(exc, "Vorbereitung", context.secrets())
        report(self, "fehler", "Vergleich fehlgeschlagen",
               message + " Es wurde nichts geändert.", "Vorbereitung")

    def start_sync(self):
        """Bibliothek vergleichen: lokaler Snapshot und Netz im Hintergrund.

        Der Snapshot läuft in `LocalMetadataWorker`; Calibres `new_api` ist
        threadsafe. Danach übernimmt `PrepareWorker` Login und Cloud-Inventar.
        Nur der GUI-Thread fasst Widgets an.
        """
        if _active_work_thread(self):
            return
        values = self.values()
        if not self._effective_refresh():
            self.status.setText("Nicht angemeldet.")
            return
        if not self._save_visible_account(values):
            # Ohne gesicherten Stand darf kein Refresh-Token verbraucht
            # werden; die Meldung nennt den Speicherfehler.
            self._save_failed("Einstellungen nicht gespeichert")
            return
        database = self.gui.current_db
        context = RunContext.from_values(
            values, column_state(database),
            library_scope=library_scope(database))
        account = account_by_name(context.account_name) or {}
        state = load_state(account.get("state"))
        self._start_local_snapshot(database, context, state)

    def _begin_progress(self, status, label):
        """Sperren und den unbestimmten Fortschritt fuer einen Lauf zeigen."""
        _apply_busy(self, True)
        self.status.setText(status)
        self.progress.setRange(0, 0)
        self.progress.setVisible(True)
        self.progress_label.setVisible(True)
        self.progress_label.setText(label)

    def _start_local_snapshot(self, database, context, state):
        """Calibre-Metadaten in einem eigenen Thread lesen."""
        self._begin_progress("Bibliothek wird gelesen …",
                             "Lese Calibre-Bibliothek …")
        worker = LocalMetadataWorker(database, context, state)
        worker.ready.connect(self._snapshot_ready)
        worker.failed.connect(self._snapshot_failed)
        start_worker(self, worker, QThread(self), "snapshot_thread",
                     "snapshot_worker", (worker.ready, worker.failed))
        self.update_status()

    def _finish_progress_thread(self, thread_name, worker_name):
        """Thread-Referenzen und Fortschrittsanzeige gemeinsam abräumen."""
        thread = getattr(self, thread_name, None)
        setattr(self, thread_name, None)
        setattr(self, worker_name, None)
        self.progress.setRange(0, 1)
        self.progress.setVisible(False)
        self.progress_label.setText("Bereit")
        self.progress_label.setVisible(False)
        _stop_thread(thread, timeout_ms=2000)
        self.update_status()

    def _finish_snapshot(self):
        """Snapshot-Thread abräumen, bevor der Netz-Worker startet."""
        self._finish_progress_thread("snapshot_thread", "snapshot_worker")

    @gui_guard
    def _snapshot_ready(self, payload):
        """Lokalen Snapshot übernehmen und die Netz-Vorbereitung starten."""
        self._finish_snapshot()
        self._start_prepare_worker(payload["context"], payload["metadata"],
                                   payload["state"])

    def _failed(self, message, worker_name, finish, log_text):
        """Abbruch melden oder Fehler protokollieren und anzeigen."""
        worker = getattr(self, worker_name, None)
        cancelled = bool(getattr(worker, "cancelled", False))
        # Das Startkonto VOR finish() lesen: der Laufzustand wird dort
        # abgeraeumt, die angezeigte Auswahl kann sich aber geaendert haben.
        run_account = (getattr(worker, "account_name", None)
                       or getattr(getattr(worker, "context", None),
                                  "account_name", None))
        finish()
        if cancelled or str(message) == CANCEL_MESSAGE:
            report(self, "info", "Abgebrochen",
                   "Abgebrochen. Es wurde nichts geändert.", log_text)
            return
        log_warning(log_text + sanitize_error(message))
        shown = report(self, "fehler", "Vergleich fehlgeschlagen",
                       str(message), log_text.rstrip(": "))
        same_account = (run_account is None
                        or str(run_account) == str(self.account_name or ""))
        if shown and needs_new_login(message) and same_account:
            # Ein abgelehnter Zugang darf nicht als «Angemeldet.» stehen
            # bleiben: sonst sieht der Nutzer keinen Grund, sich neu
            # anzumelden, und der Vergleich scheitert immer wieder. Ein
            # inzwischen angezeigtes anderes Konto bleibt unberuehrt.
            self.status.setText(NEEDS_LOGIN_STATUS)

    @gui_guard
    def _snapshot_failed(self, message):
        self._failed(message, "snapshot_worker", self._finish_snapshot,
                     "Bibliothek konnte nicht gelesen werden: ")

    def _start_prepare_worker(self, context, metadata, state):
        """Netz-Vorbereitung im Hintergrund starten (GUI bleibt bedienbar)."""
        self._begin_progress("Vergleich läuft …",
                             "Verbinde mit tolino …")
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
        self._finish_progress_thread("prepare_thread", "prepare_worker")

    @gui_guard
    def _prepare_failed(self, message):
        self._failed(message, "prepare_worker", self._finish_prepare,
                     "Vergleich fehlgeschlagen: ")

    @gui_guard
    def _prepare_ready(self, payload):
        """Vergleich anzeigen (GUI-Thread) und den Sync starten."""
        context = payload["context"]
        account_name = context.account_name
        metadata = payload["metadata"]
        state = payload["state"]
        self._finish_prepare()
        active_name = str(self.account_name or "")
        is_active = active_name == account_name
        if is_active:
            # Nur bekannte Cloud-Werte spiegeln; die vollstaendige Anzeige des
            # Kontos stellt bereits der Kontowechsel her.
            mirror_comparison_reading(self, payload.get("comparison"))

        # Den rotierten Token speichert bereits der Client-Callback.
        hardware = self.hardware
        if payload.get("hardware") and hardware is not None and is_active:
            hardware.setText(str(payload["hardware"]))
        if payload.get("read_status_known") is False:
            log_warning("Lesestatus unbekannt – er bleibt unverändert.")
        if payload.get("progress_status_known") is False:
            log_warning("Lesefortschritt unbekannt – die Spalte bleibt "
                        "unverändert.")
        if not is_active:
            note = ("Vergleich für Konto «%s» abgeschlossen. Angezeigt "
                    "wird «%s»." % (account_name, active_name))
            log_warning(note)
            self.status.setText(note)
            return
        series_map = _planning.series_map_from_metadata(
            _planning.syncable_metadata(metadata))
        try:
            selection = self._confirm_comparison(
                payload["comparison"], context,
                client_factory=lambda: self._worker_client(context),
                automatic=len(_planning.plan_collection_updates(
                    series_map, state)))
            if selection is None:
                return
            preparation = _planning.plan_actions(
                self, context, metadata, state, selection,
                series_map=series_map)
            if preparation["removals"] and not confirm_deletions(
                    self, preparation["removals"],
                    preparation.get("removal_titles")):
                return
            # Erst nach allen Bestätigungen schreiben: ein Abbruch im Dialog
            # lässt die Calibre-Spalte unangetastet.
            self._start_sync_worker(context, preparation, state)
        except Exception as exc:
            # Ein Fehler NACH dem Vergleichsfenster darf nicht als
            # Traceback im Slot verschwinden: der Nutzer sähe sonst nur ein
            # geschlossenes Fenster und keinen einzigen Upload.
            self._show_prepare_error(exc, context)

    def _confirm_comparison(self, comparison, context, client_factory=None,
                            automatic=0):
        """Bestätigungsfenster zeigen; None, wenn es abgebrochen wurde."""
        dialog = InventoryDialog(
            comparison, self,
            client_factory=(client_factory or
                            (lambda: self._worker_client(context))),
            options={
                "automatic": int(automatic or 0),
                # Für den Hinweis im Fenster: ohne diese Spalte bleiben
                # gekaufte Bücher Upload-Kandidaten.
                "ignore_column": bool_column_available(
                    getattr(self.gui, "current_db", None),
                    TOLINO_IGNORE_COLUMN),
            })
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return {
            "uploads": dialog._selected_book_ids("upload"),
            "metadata": dialog._selected_book_ids("metadata"),
            "read_changes": dialog.selected_read_changes(),
            "cloud_deletions": dialog.selected_cloud_deletions(),
        }

    def _start_sync_worker(self, context, preparation, state):
        """Übersprungene Uploads melden und den Hintergrundlauf starten."""
        skipped_uploads = preparation["skipped_uploads"]
        jobs = preparation["jobs"]
        # Der Vault liest den möglicherweise rotierten Token erst beim Login.
        counts = _planning.PlanCounts(preparation, len(jobs))
        total = counts.steps
        replacements = [job for job in jobs
                        if job.old_id]
        if replacements and not confirm_replacements(self, replacements):
            self.status.setText(
                "Abgebrochen. Die Cloud-Kopie bleibt unverändert.")
            return
        if not total and not skipped_uploads:
            # Calibre zeigt info_dialog nur mit show=True an.
            info_dialog(self, "Nichts zu tun",
                        _planning.nothing_to_do_text(preparation),
                        show=True, show_copy_button=False)
            return
        if skipped_uploads and report_skipped_uploads(
                self, skipped_uploads, nothing_else=not total):
            return
        plan = counts.text()
        log_info("Sync geplant: %s" % plan)
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
        snapshot = self.snapshot_worker
        if snapshot is not None:
            snapshot.cancel()
        prepare = self.prepare_worker
        if prepare is not None:
            prepare.cancel()
        if self.worker:
            self.worker.cancel()
        self.progress_label.setText("Abbruch angefordert …")
        self.primary.setEnabled(False)

    @gui_guard
    def sync_completed(self, updates):
        """Erfolgreicher Lauf (auch nach regulaerem Abbruch).

        Die tolino-IDs landen unter dem KONTO DES LAUFS; Lesewerte werden nur
        ueberschrieben, solange dieses Konto noch angezeigt wird.
        """
        report_sync(self, *finish_sync(self, updates, self.worker))

    @gui_guard
    def sync_failed(self, message):
        worker = self.worker
        # Auch ein Fehlerlauf hat evtl. schon Uploads abgeschlossen: deren
        # tolino-IDs in die Spalte schreiben, niemals für ein anderes Konto.
        outcome, failures = finish_sync(
            self, getattr(worker, "updates", ()), worker)
        report_sync(
            self, outcome, failures,
            str(message or "Synchronisierung fehlgeschlagen."),
            cancelled=bool(getattr(worker, "cancelled", False)))

    def finish_thread(self):
        thread = self.thread
        self.thread = None
        self.worker = None
        if thread is not None:
            _stop_thread(thread)
        self.progress_label.setText("Bereit")
        self.update_status()

    def reject(self):
        """Esc schliesst wie das Fensterkreuz: erst laeuft `closeEvent`.

        `QDialog.reject()` blendet das Fenster nur aus und ueberspringt
        `closeEvent`; laufende Worker liefen dann ohne Fenster weiter und
        die sichtbaren Einstellungen blieben ungesichert.
        """
        self.close()

    def closeEvent(self, event):
        """Schliessen: erst sichern, dann Worker beenden, nie blockieren."""
        # Schliesst nur, wenn die sichtbaren Einstellungen gesichert sind:
        # `_save_settings` meldet den Fehler und der Dialog bleibt offen --
        # samt Worker-Referenzen, damit ein erneuter Versuch moeglich ist.
        if not self._save_settings():
            event.ignore()
            return
        if _active_work_thread(self):
            self.cancel_sync()
        for thread_name, worker_name in (
                ("thread", None),
                ("prepare_thread", "prepare_worker"),
                ("snapshot_thread", "snapshot_worker")):
            _stop_thread(getattr(self, thread_name, None))
            setattr(self, thread_name, None)
            if worker_name:
                setattr(self, worker_name, None)
        # `self.worker` bleibt bis zur Auswertung stehen: nach einem
        # Thread-Timeout kann das Ergebnis noch eintreffen und braucht
        # Ergebnis, Konto und Spalten; `finish_thread()` raeumt danach auf.
        # A running browser sign-in is left alone on purpose: its thread is
        # unparented and self-terminates after its timeout, and joining it
        # here could freeze the close for minutes.
        self.login_thread = None
        self.login_worker = None
        event.accept()

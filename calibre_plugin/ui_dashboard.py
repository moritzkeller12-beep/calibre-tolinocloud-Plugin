"""Hauptfenster, Einstellungen und der sichtbare Sync-Ablauf."""
from .ui_common import (CANCEL_MESSAGE, QDialog, QInputDialog,
                        QMessageBox, QThread, _apply_busy, _dialog_busy,
                        gui_guard, start_worker,
                        LOGIN_NEXT_STEP, _login_failure_guidance,
                        _stop_thread, sync_result_detail,
                        sync_result_text, Qt)
from calibre.gui2 import error_dialog, info_dialog
from .token_vault import TOKEN_VAULT
from .config import (PREFERRED_FORMATS, account_by_name, account_summary,
                     load_state, remove_account as remove_saved_account,
                     save_account, save_credentials,
                     set_active_account, settings)
from .run_context import RunContext
from .comparison import (comparison_progress_updates,
                         comparison_read_updates)
from .identifiers import valid_book_uuid
from .library import (TOLINO_IGNORE_COLUMN, column_state,
                      bool_column_available, normalize_formats,
                      update_progress_status, update_read_status,
                      update_tolino_ids)

from . import dashboard_plan as _dashboard_plan
from .tolino import (RELOGIN, TolinoError, hardware_id, log_warning,
                     normalize_refresh_token, report_error, sanitize_error)
from .ui_auth_workers import BrowserLoginWorker, SessionCheckWorker
from .ui_dashboard_parts import (DashboardParts, confirm_deletions,
                                 confirm_replacements, offer_next_step,
                                 report_skipped_uploads)
from .ui_dialogs import InventoryDialog
from .ui_workers import PrepareWorker, SyncWorker


def _book_key_index(dashboard):
    """Return all book ids and every key that maps to one of them.

    Calibre UUIDs are authoritative when present; the ``calibre-<id>``
    fallback is used for books whose UUID field contains a title or another
    non-UUID value.
    """
    api = getattr(
        getattr(getattr(dashboard, "gui", None), "current_db", None),
        "new_api", None)
    if api is None:
        return [], {}
    book_ids = list(api.all_book_ids())
    uuids = api.all_field_for("uuid", book_ids) or {}
    mapping = {}
    for book_id in book_ids:
        key = valid_book_uuid(uuids.get(book_id))
        if key:
            mapping[key] = book_id
        mapping.setdefault("calibre-%d" % book_id, book_id)
    return book_ids, mapping


def _write_reading_updates(dashboard, read_updates, progress_updates,
                           error_label):
    """Write confirmed values and refresh the affected rows once."""
    if not (read_updates or progress_updates):
        return 0
    try:
        database = dashboard.gui.current_db
        written = (update_read_status(database, read_updates)
                   + update_progress_status(database, progress_updates))
        if written:
            book_ids = {book_id for book_id, _value
                        in read_updates + progress_updates}
            _refresh_library_view(dashboard, book_ids)
        return written
    except Exception as exc:
        log_warning("%s: %s" % (error_label, sanitize_error(exc)))
        return 0


def _mirror_account_reading(dashboard, account_name=None):
    """Gespeicherte Lesewerte des angezeigten Kontos in Calibre spiegeln."""
    try:
        account = account_by_name(
            account_name or getattr(dashboard, "account_name", "")) or {}
        state = load_state(account.get("state"))
        book_ids, book_ids_by_key = _book_key_index(dashboard)
        if not book_ids:
            return 0
        # Erst alle Bücher auf unbekannt setzen, dann die Werte des Kontos
        # darüberlegen: kein Wert eines anderen Kontos bleibt stehen.
        read_updates = dict.fromkeys(book_ids)
        progress_updates = dict.fromkeys(book_ids)
        for key, entry in state.items():
            if not isinstance(entry, dict):
                continue
            book_id = book_ids_by_key.get(str(key))
            reading = entry.get("reading")
            if book_id is None or not isinstance(reading, dict):
                continue
            if isinstance(reading.get("read"), bool):
                read_updates[book_id] = reading["read"]
            progress = reading.get("progress")
            if isinstance(progress, dict) and "percent" in progress:
                progress_updates[book_id] = progress.get("percent")
        return _write_reading_updates(
            dashboard, list(read_updates.items()),
            list(progress_updates.items()),
            "Lese-Spalten konnten nicht aktualisiert werden")
    except Exception as exc:
        log_warning("Lese-Spalten konnten nicht aktualisiert werden: %s"
                    % sanitize_error(exc))
        return 0


def _mirror_comparison_reading(dashboard, comparison):
    """Bekannte Cloud-Lesewerte direkt nach dem Vergleich spiegeln.

    Der Konto-State bleibt die Quelle für den Konto-Wechsel; dieser direkte
    Weg stellt sicher, dass auch ein bereits vor dem Vergleich vorhandener
    Cloud-Stand in den Spalten ankommt.
    """
    return _write_reading_updates(
        dashboard, comparison_read_updates(comparison),
        comparison_progress_updates(comparison),
        "Lesewerte konnten nicht nach Calibre gespiegelt werden")


def _persist_sync_result(dialog, state, updates):
    """Sync-Zustand, Tolino-IDs und bestätigten Lesestatus sichern.

    Die beiden Calibre-Schreibvorgänge laufen getrennt und sind einzeln
    abgesichert: ein Fehler in einer Spalte darf weder den anderen
    Schreibvorgang noch den Abschluss des Laufs verhindern.
    """
    worker = getattr(dialog, "worker", None)
    account_name = str(
        getattr(worker, "account_name", None)
        or getattr(dialog, "account_name", "") or "")
    columns = getattr(getattr(worker, "context", None), "columns", {}) or {}
    failures = []
    try:
        if account_name:
            save_account(account_name, {"state": state}, create=False)
    except Exception as exc:
        failures.append("Sync-Zustand")
        log_warning("Ergebnis der Synchronisierung konnte nicht "
                    "gespeichert werden: %s" % sanitize_error(exc))
    writers = [
        ("id", "tolino-IDs",
         lambda db: update_tolino_ids(
             db, updates, account_name=account_name)),
    ]
    if account_name and account_name == str(
            getattr(dialog, "account_name", "") or ""):
        writers.append(
            ("read", "Lesestatus",
             lambda db: update_read_status(
                 db, list(getattr(worker, "read_updates", ()) or ()))))
    for key, label, write in writers:
        if not columns.get(key):
            continue
        try:
            write(dialog.gui.current_db)
        except Exception as exc:
            failures.append(label)
            log_warning("%s: Schreiben in Calibre fehlgeschlagen: %s"
                        % (label, sanitize_error(exc)))
    return failures


def _finish_sync(dialog, state, updates):
    """Laufergebnis sichern und den Thread in jedem Fall beenden."""
    outcome = dict(getattr(dialog.worker, "outcome", None) or {})
    try:
        return outcome, _persist_sync_result(dialog, state, updates)
    finally:
        # Auch ein unerwarteter Fehler beim Schreiben in Calibre darf
        # den Dialog nicht im Zustand «läuft» zurücklassen.
        dialog.finish_thread()


def _refresh_library_view(dashboard, book_ids):
    """Geänderte Calibre-Zeilen sichtbar machen, ohne die Bibliothek neu zu laden."""
    view = getattr(getattr(dashboard, "gui", None), "library_view", None)
    model = getattr(view, "model", None)
    if callable(model):
        model = model()
    refresh = getattr(model, "refresh_ids", None)
    if callable(refresh):
        try:
            refresh(tuple(book_ids))
        except Exception as exc:
            log_warning("Calibre-Ansicht konnte nicht aktualisiert werden: %s"
                        % sanitize_error(exc))


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
        if not getattr(self, "account_name", None):
            return
        values = dict(values or self.values())
        name = str(values.get("account_name") or "")
        if not name:
            # Fehlende Konten dürfen keinen fremden Zustand überschreiben.
            log_warning("Das sichtbare Konto existiert nicht mehr; "
                        "Änderungen wurden nicht gespeichert.")
            return
        # Ein unverändertes Feld darf eine vom Worker übernommene Geräte-ID
        # nicht mit einem veralteten Anzeigewert überschreiben.
        if not getattr(self, "_hardware_edited", False):
            values.pop("hardware_id", None)
        save_account(name, values, active=name,
                     preferred_formats=values[PREFERRED_FORMATS])
        self._hardware_edited = False

    def _mark_hardware_edited(self, _text=""):
        """Merken, dass die Geräte-ID von Hand geändert wurde."""
        self._hardware_edited = True

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
        # Eine laufende Anmeldeprüfung bleibt an ihr Startkonto gebunden.
        login = getattr(self, "login_thread", None)
        if login is not None and login.isRunning():
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
        partner_id = values["partner_id"]
        if self.partner.findData(partner_id) < 0:
            partner_id = self.partner.itemData(0)
            if partner_id is not None:
                save_account(self.account_name, {"partner_id": partner_id})
        self.partner.setCurrentIndex(max(0, self.partner.findData(partner_id)))
        self.hardware.setText(values["hardware_id"] or hardware_id())
        self._hardware_edited = False
        # Das leere Feld darf einen rotierten Token nicht überschreiben.
        self.refresh.setText("")
        self.formats.setText(", ".join(values["preferred_formats"]))
        self.update_status()
        _mirror_account_reading(self, self.account_name)

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
        hardware = str(hardware or "").strip()
        if not hardware and not getattr(self, "_hardware_edited", False):
            account = account_summary(getattr(self, "account_name", "")) or {}
            hardware = str(account.get("hardware_id") or "")
        save_credentials(
            self.account_name, token,
            hardware or self.hardware.text().strip() or hardware_id())
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
        return account_summary(getattr(self, "account_name", None))

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
        hardware = self.hardware.text().strip() or hardware_id()
        formats = [fmt for fmt in normalize_formats(self.formats.text())
                   if fmt in ("EPUB", "PDF")]
        return {
            "account_name": str(account.get("name") or ""),
            "partner_id": self.partner.currentData(),
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
            self.store_refresh(typed)
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
        self.status.setText("Die Anmeldung wird gerade erneuert, bitte kurz warten.")

    def _start_login_window(self, context):
        """Das eigene Anmeldefenster öffnen und die Anmeldung übernehmen."""
        if self._login_running():
            return  # a sign-in attempt is already running
        _apply_busy(self, True)
        self.primary.setEnabled(False)
        self.status.setText(
            "Bitte im Browserfenster anmelden, bis die Bücherliste erscheint.")
        worker = BrowserLoginWorker(
            context.account_name, context.partner_id,
            self.hardware.text().strip())
        worker.progress.connect(self._login_progress)
        worker.completed.connect(self.login_completed)
        worker.failed.connect(self.login_failed)
        start_worker(self, worker, QThread(), "login_thread", "login_worker",
                     (worker.completed, worker.failed))

    @gui_guard
    def login_completed(self, refresh, hardware):
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
        QMessageBox.warning(self, title, LOGIN_NEXT_STEP)

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
        return TOKEN_VAULT.login(context.account_name, context.partner_id)

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
            self.status.setText("Nicht angemeldet.")
            return
        self._save_visible_account(values)
        context = RunContext.from_values(
            values, column_state(self.gui.current_db))
        account = account_by_name(context.account_name) or {}
        state = load_state(account.get("state"))
        try:
            # Calibre-DB und Qt-Widgets nur hier (GUI-Thread) lesen.
            metadata = _dashboard_plan.local_metadata(self, context)
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
        active_name = str(getattr(self, "account_name", "") or "")
        is_active = active_name == account_name
        if is_active:
            _mirror_account_reading(self, account_name)
            _mirror_comparison_reading(self, payload.get("comparison"))

        # Den rotierten Token speichert bereits der Client-Callback.
        hardware = getattr(self, "hardware", None)
        if payload.get("hardware") and hardware is not None and is_active:
            hardware.setText(str(payload["hardware"]))
        if payload.get("read_status_known") is False:
            log_warning("Lesestatus unbekannt – er bleibt unverändert.")
        if payload.get("progress_status_known") is False:
            log_warning("Lesefortschritt unbekannt – die Spalte bleibt "
                        "unverändert.")
        if not is_active:
            log_warning("Vergleich für «%s» abgeschlossen; angezeigt wird «%s»."
                        % (account_name, active_name))
            self.status.setText(
                "Vergleich für Konto «%s» abgeschlossen. Angezeigt wird «%s»."
                % (account_name, active_name))
            return
        series_map = _dashboard_plan.series_map_from_metadata(
            _dashboard_plan.syncable_metadata(metadata))
        try:
            selection = self._confirm_comparison(
                payload["comparison"], context,
                client_factory=lambda: self._worker_client(context),
                automatic=len(_dashboard_plan.plan_collection_updates(
                    series_map, state)))
            if selection is None:
                return
            preparation = _dashboard_plan.plan_actions(
                self, context, metadata, state, selection,
                series_map=series_map)
            if preparation["removals"] and not confirm_deletions(
                    self, preparation["removals"], state,
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
                    getattr(getattr(self, "gui", None), "current_db", None),
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
        total = _dashboard_plan.plan_counts(preparation, len(jobs))["steps"]
        replacements = [job for job in jobs
                        if getattr(job, "old_id", None)]
        if replacements and not confirm_replacements(self, replacements):
            self.status.setText(
                "Abgebrochen. Die Cloud-Kopie bleibt unverändert.")
            return
        if not total and not skipped_uploads:
            # Calibre zeigt info_dialog nur mit show=True an.
            info_dialog(self, "Nichts zu tun",
                        _dashboard_plan.nothing_to_do_text(preparation),
                        show=True, show_copy_button=False)
            return
        if skipped_uploads and report_skipped_uploads(
                self, skipped_uploads, nothing_else=not total):
            return
        plan = _dashboard_plan.plan_text(preparation, len(jobs))
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
        outcome, failures = _finish_sync(self, state, updates)
        if failures:
            error_dialog(self, "Speichern fehlgeschlagen",
                         "Nicht gespeichert: %s. Cloud-Änderungen bleiben "
                         "bestehen; bitte den Bestand prüfen.\n\n%s"
                         % (", ".join(failures), sync_result_text(outcome, "")),
                         show=True)
            return
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
        worker = self.worker
        cancelled = bool(getattr(worker, "cancelled", False)) or \
            str(message) == CANCEL_MESSAGE
        # Auch ein Fehlerlauf hat evtl. schon Uploads abgeschlossen: deren
        # tolino-IDs in die Spalte schreiben, niemals für ein anderes Konto.
        outcome, failures = _finish_sync(
            self, getattr(worker, "state", {}),
            getattr(worker, "updates", ()))
        if cancelled and not failures:
            info_dialog(self, "Abgebrochen", _sync_cancelled_text(outcome),
                        show=True, show_copy_button=False)
            return
        result = sync_result_text(outcome, "")
        text = str(message or "Synchronisierung fehlgeschlagen.")
        if failures:
            text += ("\n\nNicht gespeichert: %s. Cloud-Änderungen bleiben "
                     "bestehen; bitte den Bestand prüfen." % ", ".join(failures))
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

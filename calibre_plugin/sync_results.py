"""Ergebnis eines Sync-Laufs sichern und melden -- ohne Widgets.

Die Funktionen arbeiten auf dem Dialog nur über einen Duck-Typ
(`gui.current_db`, `account_name`, `worker`, `finish_thread`) und schreiben
ausschliesslich über die Calibre-Schnittstelle und `config`. Ein Test dafür
braucht deshalb keinen Qt-Aufbau; das Dashboard ruft nur noch auf.
"""
from qt.core import QMessageBox

from calibre.gui2 import error_dialog, info_dialog
from .comparison import comparison_progress_updates, comparison_read_updates
from .config import account_by_name, load_state
from .identifiers import book_uuid, calibre_key
from .library import (library_scope, update_progress_status,
                      update_read_status, update_tolino_ids)
from .tolino import log_warning, sanitize_error
from .ui_common import (CANCEL_MESSAGE, sync_result_detail,
                        sync_result_text)


def book_key_index(dashboard):
    """Return all book ids and every key that maps to one of them.

    Calibre UUIDs are authoritative when present. The library-scoped
    fallback is only mapped for books whose UUID field contains a title or
    another non-UUID value; it never shadows a real UUID.
    """
    database = getattr(getattr(dashboard, "gui", None), "current_db", None)
    api = getattr(database, "new_api", None)
    if api is None:
        return [], {}
    book_ids = list(api.all_book_ids())
    uuids = api.all_field_for("uuid", book_ids) or {}
    scope = library_scope(database)
    mapping = {}
    for book_id in book_ids:
        key = book_uuid(uuids.get(book_id))
        if key:
            mapping[key] = book_id
        else:
            mapping.setdefault(calibre_key(book_id, scope), book_id)
    return book_ids, mapping


def write_reading_updates(dashboard, read_updates, progress_updates,
                          error_label):
    """Write confirmed values and refresh the affected rows once.

    Lesestatus und Fortschritt sind getrennte Spalten: ein Fehler in einer
    Spalte darf die andere nicht blockieren.
    """
    if not (read_updates or progress_updates):
        return 0
    try:
        database = dashboard.gui.current_db
    except Exception as exc:
        log_warning("%s: %s" % (error_label, sanitize_error(exc)))
        return 0
    written = 0
    for write, updates in ((update_read_status, read_updates),
                           (update_progress_status, progress_updates)):
        if not updates:
            continue
        try:
            written += write(database, updates)
        except Exception as exc:
            log_warning("%s: %s" % (error_label, sanitize_error(exc)))
    if written:
        book_ids = {book_id for book_id, _value
                    in read_updates + progress_updates}
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
    return written


def mirror_account_reading(dashboard, account_name=None):
    """Gespeicherte Lesewerte des angezeigten Kontos in Calibre spiegeln."""
    try:
        account = account_by_name(
            account_name or getattr(dashboard, "account_name", "")) or {}
        state = load_state(account.get("state"))
        book_ids, book_ids_by_key = book_key_index(dashboard)
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
        return write_reading_updates(
            dashboard, list(read_updates.items()),
            list(progress_updates.items()),
            "Lese-Spalten konnten nicht aktualisiert werden")
    except Exception as exc:
        log_warning("Lese-Spalten konnten nicht aktualisiert werden: %s"
                    % sanitize_error(exc))
        return 0


def mirror_comparison_reading(dashboard, comparison):
    """Bekannte Cloud-Lesewerte direkt nach dem Vergleich spiegeln.

    Der Konto-State bleibt die Quelle für den Konto-Wechsel; dieser direkte
    Weg stellt sicher, dass auch ein bereits vor dem Vergleich vorhandener
    Cloud-Stand in den Spalten ankommt.
    """
    return write_reading_updates(
        dashboard, comparison_read_updates(comparison),
        comparison_progress_updates(comparison),
        "Lesewerte konnten nicht nach Calibre gespiegelt werden")


def _stored_id_updates(dialog, updates, account_name):
    """(Buch, tolino-ID) aus dem gespeicherten Konto-Zustand.

    Welche Buecher ein Lauf betrifft, bestimmt sein Ergebnis; der Wert kommt
    aus dem Konto, denn `#tolino_id` spiegelt nur, was dort steht. So landet
    auch nach einem Fehlschlag keine tolino-ID in der Spalte, die der
    Zwischenstand nicht gesichert hat.
    """
    if not account_name or not updates:
        return []
    state = load_state((account_by_name(account_name) or {}).get("state"))
    _book_ids, book_ids_by_key = book_key_index(dialog)
    key_of = {}
    for key, book_id in book_ids_by_key.items():
        key_of.setdefault(book_id, key)
    found = []
    for book_id, _tolino_id in updates:
        entry = state.get(str(key_of.get(book_id)))
        # Fehlt das Buch im Konto, gibt es fuer dieses Konto keine Zuordnung
        # (etwa nach dem Loeschen): die Spalte wird geleert.
        found.append((book_id, entry.get("tolino_id")
                      if isinstance(entry, dict) else ""))
    return found


def persist_sync_result(dialog, updates, worker=None):
    """Tolino-IDs, Lesestatus und Fortschritt in Calibre spiegeln.

    Den Konto-Zustand sichern die Worker selbst; hier laufen nur die
    Calibre-Schreibvorgänge, einzeln abgesichert: ein Fehler in einer Spalte
    darf die anderen nicht blockieren. `worker` ist der Worker DIESES Laufs:
    nach einem Thread-Timeout beim Schliessen ist `dialog.worker` schon leer.
    """
    worker = worker if worker is not None else getattr(dialog, "worker", None)
    dialog_account = str(getattr(dialog, "account_name", "") or "")
    account_name = str(getattr(worker, "account_name", None) or dialog_account)
    columns = getattr(getattr(worker, "context", None), "columns", {}) or {}
    # Die Bibliothek des Starts ist die einzige, in die dieser Lauf schreiben
    # darf: sonst landen tolino-IDs in einer anderen Bibliothek.
    start_scope = str(getattr(getattr(worker, "context", None),
                             "library_scope", "") or "")
    if start_scope:
        try:
            current_scope = library_scope(dialog.gui.current_db)
        except Exception as exc:
            current_scope = ""
            log_warning("Bibliothek konnte nicht bestimmt werden: %s"
                        % sanitize_error(exc))
        if current_scope != start_scope:
            log_warning("Bibliothek gewechselt: Sync-Lauf abgebrochen, es "
                        "wurde nichts gespeichert.")
            return ["Bibliothek gewechselt – Lauf abgebrochen"]
    failures = []
    writers = [
        # Zuordnungen erst hier bestimmen: ein Lesefehler gilt so als Fehler
        # dieser Spalte, und ohne tolino-ID-Spalte wird nichts vorbereitet.
        ("id", "tolino-IDs",
         lambda db: update_tolino_ids(
             db, _stored_id_updates(dialog, updates, account_name),
             account_name=account_name)),
    ]
    if account_name and account_name == dialog_account:
        writers.extend([
            ("read", "Lesestatus",
             lambda db: update_read_status(
                 db, list(getattr(worker, "read_updates", ()) or ()))),
            ("progress", "Lesefortschritt",
             lambda db: update_progress_status(
                 db, list(getattr(worker, "progress_updates", ()) or ()))),
        ])
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


def finish_sync(dialog, updates, worker=None):
    """Laufergebnis spiegeln und den Thread in jedem Fall beenden."""
    worker = worker if worker is not None else getattr(dialog, "worker", None)
    outcome = dict(getattr(worker, "outcome", None) or {})
    try:
        return outcome, persist_sync_result(dialog, updates, worker)
    finally:
        # Auch ein unerwarteter Fehler beim Schreiben in Calibre darf
        # den Dialog nicht im Zustand «läuft» zurücklassen.
        dialog.finish_thread()


def sync_failure_heading(outcome):
    """Titel und Vorsatz eines fehlerhaften Laufs.

    «Teilweise fehlgeschlagen» heisst: mindestens eine Aktion wurde wirklich
    übertragen. Der Unterschied gehört in Titel und Text.
    """
    succeeded = sum(int(outcome.get(key) or 0) for key in (
        "uploaded", "metadata", "read_marked", "read_unmarked",
        "collections", "deleted"))
    if succeeded:
        return "Teilweise fehlgeschlagen", "Teilweise abgeschlossen: "
    return "Fehlgeschlagen", "Fehlgeschlagen: "


def report(dialog, kind, title, text, log, detail=""):
    """Rückmeldung an einem offenen Fenster; sonst nur eine Logzeile.

    `kind` ist «info», «fehler» oder «warnung». Rückgabe: False, wenn nur
    protokolliert wurde -- der Aufrufer spart sich dann die UI-Arbeit.
    """
    # Ein Dialog auf einem geschlossenen Fenster blockiert den GUI-Thread;
    # Doubles ohne `isVisible` gelten als offen.
    visible = getattr(dialog, "isVisible", None)
    if visible is not None and not visible():
        log_warning("Fenster geschlossen; %s: %s"
                    % (log, sanitize_error(text)))
        return False
    if kind == "info":
        info_dialog(dialog, title, text, show=True, show_copy_button=False)
    elif kind == "warnung":
        QMessageBox.warning(dialog, title, text)
    else:
        error_dialog(dialog, title, text, det_msg=detail, show=True)
    return True


def report_sync(dialog, outcome, failures, message="", cancelled=False):
    """Ausgang eines Laufs melden: Speicherfehler, Abbruch, Fehler, Erfolg.

    `sync_completed` und `sync_failed` melden denselben Lauf, nur mit
    unterschiedlichem Einstieg -- deshalb eine Regel für beide.
    """
    outcome = outcome if isinstance(outcome, dict) else {}
    detail = sync_result_detail(outcome)
    result = sync_result_text(outcome, "")
    text_message = str(message or "")
    # Eine konkrete Fehlermeldung hat Vorrang vor einem Abbruchflag: sonst
    # verschluckte ein abgebrochener Lauf seinen eigenen Fehlergrund.
    error_message = bool(text_message) and text_message != CANCEL_MESSAGE
    cancellation = (bool(cancelled) or bool(outcome.get("cancelled"))
                    or text_message == CANCEL_MESSAGE)
    if failures:
        text = ("Nicht gespeichert: %s. Cloud-Änderungen bleiben bestehen; "
                "bitte den Bestand prüfen." % ", ".join(failures))
        if text_message:
            text = text_message + "\n\n" + text
        elif result:
            text += "\n\n" + result
        report(dialog, "fehler", "Speichern fehlgeschlagen", text,
               "Speichern fehlgeschlagen", detail)
    elif cancellation and not error_message:
        report(dialog, "info", "Abgebrochen",
               ("Abgebrochen. " + result) if result
               else "Abgebrochen. Es wurde nichts übertragen.",
               "Lauf abgebrochen")
    elif text_message:
        text = text_message
        if outcome.get("failed") and result:
            text += "\n\n" + result
        report(dialog, "fehler", sync_failure_heading(outcome)[0], text,
               "Sync fehlgeschlagen", detail)
    elif outcome.get("failed"):
        title, prefix = sync_failure_heading(outcome)
        report(dialog, "fehler", title, prefix + result, "Lauf fehlgeschlagen",
               detail)
    else:
        report(dialog, "info", "Fertig", sync_result_text(outcome, "Fertig"),
               "Lauf beendet")

"""Gemeinsame Qt-Importe, Konstanten und Hilfsfunktionen der Oberfläche.

Alle UI-Module beziehen Qt-Namen und geteilte Helfer von hier, damit ein
Qt-Stub in den Tests nur an einer Stelle greift.
"""
import os
import re

try:
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox, QMenu,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QObject, QInputDialog,
                         QFileDialog, Qt, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QLabel, QLineEdit, QMessageBox,
                         QMenu,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QObject, QInputDialog,
                         QFileDialog, Qt, Signal as pyqtSignal)

def qt_enum(owner, scope, name):
    """Qt-Enum-Wert in der Schreibweise, die Qt 5 und Qt 6 verstehen.

    PyQt6 (Calibre 6 und neuer) kennt nur die vollständige Form
    ``QHeaderView.ResizeMode.Stretch``; die Kurzform ohne Enum-Klasse gibt
    es dort nicht mehr. PyQt5 kennt beide. Fehlt die Enum-Klasse (alte
    Bindungen, Test-Stubs), wird die Kurzform genommen.
    """
    value = getattr(getattr(owner, scope, None), name, None)
    if value is None:
        value = getattr(owner, name)
    return value


# Weitergegeben an die anderen UI-Module (und teilweise nur dort benutzt):
# die Liste ist absichtlich kurz. Fruehere Fassungen importierten hier das
# halbe sync-/tolino-Modul, ohne es zu benutzen.
from .identifiers import display_title
from .library import safe_format_path

# Namen, die andere UI-Module und die Tests ueber dieses Modul beziehen.
__all__ = [
    "CANCEL_MESSAGE", "OP_FAILED", "OP_NOT_STARTED", "OP_SKIPPED",
    "OP_SUCCEEDED", "QCheckBox", "QComboBox", "QDialog",
    "QFileDialog", "QFormLayout", "QGroupBox", "QHBoxLayout", "QMenu",
    "QInputDialog", "QLabel", "QLineEdit", "QMessageBox", "QObject",
    "QProgressBar", "QPushButton", "QTableWidget", "QTableWidgetItem",
    "QThread", "QVBoxLayout", "Qt", "SyncJob", "qt_enum",
    "_ORPHANED_THREADS", "_apply_busy", "_dialog_busy",
    "_display_value", "_format_signatures", "_login_failure_guidance",
    "_metadata_value", "_stop_thread", "plural",
    "_worker_account_name", "_worker_outcome", "_set_enabled",
    "_discard_item", "_thread_running",
    "pyqtSignal", "safe_format_path",
    "sync_result_detail",
    "sync_result_text",
]


def _metadata_value(item, name, default=""):
    try:
        return getattr(item, name)
    except AttributeError:
        try:
            return item.get(name, default)
        except AttributeError:
            return default


def plural(n, singular, plural_form):
    """Return the requested German noun form for a count."""
    return singular if int(n) == 1 else plural_form


def _display_value(value):
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(x) for x in value)
    if isinstance(value, dict):
        return ", ".join("%s: %s" % (key, val) for key, val in sorted(value.items()))
    return str(value or "")


def _format_signatures(database, book_id, formats):
    """size:mtime je Format-Datei – das billige Content-Signal.

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
    # `title` gehoert dazu: Fortschritt und Bericht nennen den
    # Titel, nicht die Kennung. Der Standardwert haelt aeltere Aufrufer
    # (und Tests) unveraendert am Laufen.
    __slots__ = ("book_id", "book_uuid", "format_name", "old_id", "path",
                 "cover_path", "title")

    def __init__(self, book_id, book_uuid, format_name, old_id, path,
                 cover_path, title=""):
        self.book_id = book_id
        self.book_uuid = book_uuid
        self.format_name = format_name
        self.old_id = old_id
        self.path = path
        self.cover_path = cover_path
        self.title = title

    def label(self):
        """Sichtbarer Name des Buchs: Titel, sonst «Buch <Nummer>»."""
        return display_title(self.title, self.book_id)


CANCEL_MESSAGE = "Abgebrochen."

# Ergebnis-Zustände je Operation. "not_started" ist bewusst ein eigener
# Zustand: nach einem Fehler oder Abbruch darf der Bericht nicht so tun,
# als wäre nichts passiert -- und auch nicht, als wäre alles versucht worden.
OP_SUCCEEDED = "succeeded"
OP_SKIPPED = "skipped"
OP_FAILED = "failed"
OP_NOT_STARTED = "not_started"

# Threads, die beim Schliessen nicht innerhalb des Zeitlimits fertig wurden:
# Die Referenz haelt sie am Leben, statt sie (und Calibre) abzustuerzen.
_ORPHANED_THREADS = []

# Laufende Einzelaufrufe (CallWorker). Wie bei der Anmeldung haelt diese
# Referenz den Thread am Leben, wenn das Fenster vorher schliesst.
_CALL_THREADS = []


def start_call(dialog, call, on_done, on_failed):
    """Einen Netzaufruf in einem eigenen QThread ausfuehren.

    `call` bekommt keine Widgets und keine Datenbank -- nur Werte, wie
    `PrepareWorker`. `on_done`/`on_failed` sind Methoden des Dialogs; Qt
    stellt sie in den GUI-Thread. Ohne Elternteil, damit ein geschlossenes
    Fenster keinen laufenden Thread zerstoert.
    """
    from .ui_auth_workers import CallWorker

    thread = QThread()
    worker = CallWorker(call)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.done.connect(thread.quit)
    worker.failed.connect(thread.quit)
    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(lambda: _discard_item(_CALL_THREADS, thread))
    _CALL_THREADS.append(thread)
    dialog._call_thread = thread
    dialog._call_worker = worker
    thread.start()
    return thread


def _discard_item(collection, item):
    """Remove `item` if present; a repeated signal must not raise."""
    try:
        collection.remove(item)
    except ValueError:
        pass


def _stop_thread(thread, timeout_ms=3000):
    """Request a stop and wait at most `timeout_ms`; detach on timeout."""
    if thread is None:
        return True
    try:
        thread.quit()
    except Exception:
        pass
    try:
        finished = bool(thread.wait(timeout_ms))
    except Exception:
        finished = False
    if not finished:
        try:
            thread.setParent(None)
        except Exception:
            pass
        _ORPHANED_THREADS.append(thread)
    return finished


def _worker_account_name(dialog):
    """Account of the running/finished worker -- never the visible one."""
    worker = getattr(dialog, "worker", None)
    name = getattr(worker, "account_name", None)
    return str(name or getattr(dialog, "account_name", "") or "")


def _worker_outcome(dialog):
    worker = getattr(dialog, "worker", None)
    return dict(getattr(worker, "outcome", None) or {})


def _thread_running(thread):
    """Läuft dieser Thread noch? (None = nein, Stub ohne isRunning = ja)"""
    try:
        return thread is not None and thread.isRunning()
    except Exception:
        return thread is not None


def _dialog_busy(dialog):
    """Läuft Sync, Vorbereitung oder Anmeldung? (ohne Qt-Abhängigkeit)"""
    if getattr(dialog, "thread", None) is not None:
        return True
    if getattr(dialog, "prepare_thread", None) is not None:
        return True
    return _thread_running(getattr(dialog, "login_thread", None))


def _set_enabled(owner, names, enabled):
    """Widgets sperren/freigeben -- tolerant gegen fehlende Qt-Stubs."""
    for name in names:
        setter = getattr(getattr(owner, name, None), "setEnabled", None)
        if setter is None:
            continue
        try:
            setter(enabled)
        except Exception:
            pass  # Qt-Stub ohne Aktivierung


def _apply_busy(dialog, busy):
    """Disable account/partner/sync controls while an operation runs."""
    _set_enabled(dialog, ("account_quick", "account_select", "account_new",
                          "account_remove", "partner", "login_button",
                          "settings_button", "hardware", "refresh",
                          "formats"),
                 not busy)
    if busy:
        status = getattr(dialog, "status", None)
        if status is not None:
            try:
                if getattr(dialog, "thread", None) is not None:
                    text = "Synchronisierung läuft …"
                elif getattr(dialog, "prepare_thread", None) is not None:
                    text = "Vergleich läuft …"
                else:
                    text = "Anmeldung wird geprüft …"
                status.setText(text)
            except Exception:
                pass


# Titel + naechster Schritt je Anmelde-Fehlercode. Kurz, einzeilig und
# ohne technische Antworten -- Details stehen nur im Log (report_error).
_LOGIN_GUIDANCE = {
    "A01": ("Anmeldung beendet",
            "Bitte neu bei tolino anmelden."),
    "A02": ("Zugang verbraucht",
            "Der gespeicherte Zugang ist verbraucht. Bitte neu anmelden."),
    "A03": ("Nicht angemeldet",
            "Bitte zuerst bei tolino anmelden."),
    "A07": ("Anmelde-Antwort ungültig",
            "Bitte erneut bei tolino anmelden."),
    "B01": ("Nicht angemeldet",
            "Im Browserfenster anmelden, bis die Bücherliste lädt."),
    "B02": ("Zugang verbraucht",
            "Bitte neu anmelden. Das Anmeldefenster wurde geschlossen."),
    "S91": ("Anmeldung wird erneuert",
            "Kurz warten und erneut versuchen."),
    "B05": ("Kein unterstützter Browser gefunden",
            "Chrome, Chromium, Brave oder Edge installieren und Calibre "
            "neu starten."),
    "B06": ("Flatpak: Hilfsprogramm fehlt",
            "«flatpak update» ausführen, Calibre neu starten."),
    "B08": ("Flatpak: Freigabe fehlt",
            "Den angezeigten flatpak override-Befehl ausführen und Calibre neu starten."),
    "B07": ("Anmeldefenster konnte nicht starten",
            "Alle Chrome-/Chromium-Fenster schließen und erneut versuchen."),
    "N01": ("Verbindung fehlgeschlagen",
            "Netzwerk prüfen. Nach einer Zeitüberschreitung ggf. neu anmelden."),
    "B03": ("Anmeldung abgebrochen",
            "Anmeldefenster offen lassen, bis die Bücherliste lädt, und "
            "erneut starten."),
    "B04": ("Zeitüberschreitung bei der Anmeldung",
            "Im Fenster anmelden, bis die Bücherliste erscheint, und dann "
            "erneut versuchen."),
    "A06": ("Anmeldung abgelehnt",
            "Details im Calibre-Log; Buchhändler und Zugang prüfen."),
}

_LOGIN_DEFAULT_GUIDANCE = (
    "Anmeldung nicht abgeschlossen",
    "Verbindung prüfen und die Anmeldung erneut starten.")


def _login_failure_guidance(error):
    """(Titel, nächster Schritt) für einen fehlgeschlagenen Browser-Login.

    Akzeptiert die Exception (mit ``.code``) oder die bereits formatierte
    Meldung («… (Code B04)»). Unbekannte Ursachen bekommen einen neutralen,
    aber umsetzbaren Hinweis; sensible Daten werden nie angezeigt.
    """
    code = str(getattr(error, "code", "") or "")
    if not code:
        match = re.search(r"\(Code ([A-Z][0-9]{2})\)", str(error or ""))
        code = match.group(1) if match else ""
    return _LOGIN_GUIDANCE.get(code, _LOGIN_DEFAULT_GUIDANCE)


def sync_result_text(outcome, prefix="Fertig"):
    """Kurzbilanz der erfolgreich übertragenen Aktionen."""
    outcome = outcome if isinstance(outcome, dict) else {}
    parts = []
    labels = (("uploaded", "hochgeladen"), ("metadata", "Metadaten"),
              ("read_marked", "als gelesen markiert"),
              ("read_unmarked", "als ungelesen markiert"),
              ("collections", "Sammlungen"), ("deleted", "gelöscht"),
              ("skipped", "übersprungen"), ("failed", "Fehler"))
    for key, label in labels:
        count = int(outcome.get(key) or 0)
        if count:
            parts.append("%d %s" % (count, label))
    if not parts and prefix == "":
        text = ""
    else:
        text = (("%s: %s" % (prefix, ", ".join(parts)) if parts else prefix)
                if prefix else ", ".join(parts))
    # Ein einzelner Fehler haelt den Lauf nicht mehr auf -- dann
    # muss der Bericht sagen, WELCHE Schritte es getroffen hat.
    failed = [item for item in (outcome.get("operations") or ())
              if isinstance(item, dict) and item.get("status") == OP_FAILED]
    if failed:
        lines = [_operation_line(item) for item in failed[:5]]
        if len(failed) > 5:
            lines.append("- … und %d weitere" % (len(failed) - 5))
        text += "\n\nNicht abgeschlossen:\n" + "\n".join(lines)
    return text


def _operation_line(item):
    """Eine Zeile je gescheitertem Schritt -- Name und kurzer Grund."""
    item = item if isinstance(item, dict) else {}
    error = item.get("error")
    if error and hasattr(error, "short"):
        detail = str(error.short or error)
        code = str(getattr(error, "code", "") or "")
        if code:
            detail += " (Code %s)" % code
    elif error:
        code_match = re.search(r"\(Code ([A-Z][0-9]{2})\)", str(error))
        detail = str(error)
        if code_match and code_match.group(1) not in detail[-16:]:
            detail += " (Code %s)" % code_match.group(1)
    else:
        detail = str(error or "")
    return "- %s%s" % (item.get("label") or "Schritt",
                       (": " + detail) if detail else "")


def sync_result_detail(outcome):
    """Komplette Liste der gescheiterten Schritte fuer den Detailtext.

    Der sichtbare Text nennt hoechstens fuenf (sonst wird der Dialog
    unlesbar); bei mehr als fuenf Fehlern steht hier jeder einzelne Schritt
    mit Titel und Grund. Leer heisst: kein Detailtext noetig.
    """
    outcome = outcome if isinstance(outcome, dict) else {}
    failed = [item for item in (outcome.get("operations") or ())
              if isinstance(item, dict) and item.get("status") == OP_FAILED]
    if len(failed) <= 5:
        return ""
    return "\n".join(_operation_line(item) for item in failed)



"""Gemeinsame Qt-Importe, Konstanten und Hilfsfunktionen der Oberfläche.

Alle UI-Module beziehen Qt-Namen und geteilte Helfer von hier, damit ein
Qt-Stub in den Tests nur an einer Stelle greift.
"""
import functools
import re

from qt.core import (QCheckBox, QComboBox, QDialog,
                     QFormLayout, QGroupBox, QHeaderView, QLabel,
                     QLineEdit, QMessageBox, QMenu,
                     QProgressBar, QPushButton, QThread,
                     QVBoxLayout, QHBoxLayout, QTableWidget,
                     QTableWidgetItem, QObject, QInputDialog,
                     QFileDialog, QStyledItemDelegate, QEvent, Qt, pyqtSignal)
from .tolino import TolinoError

# Re-export the Qt names other UI modules and the test loader import from here.
__all__ = [
    "CANCEL_MESSAGE", "OP_FAILED", "OP_NOT_STARTED", "OP_SKIPPED",
    "OP_SUCCEEDED", "QCheckBox", "QComboBox", "QDialog", "QFileDialog",
    "QFormLayout", "QGroupBox", "QHBoxLayout", "QHeaderView", "QMenu",
    "QInputDialog", "QLabel", "QLineEdit", "QMessageBox", "QObject",
    "QProgressBar", "QPushButton", "QTableWidget", "QTableWidgetItem",
    "QThread", "QVBoxLayout", "QStyledItemDelegate", "QEvent", "Qt",
    "_THREADS", "_apply_busy",
    "_dialog_busy", "LOGIN_NEXT_STEP", "_login_failure_guidance",
    "gui_guard", "start_worker", "_stop_thread", "_set_enabled",
    "_keep_thread_alive", "pyqtSignal", "sync_result_detail",
    "sync_result_text",
]


def _widget_deleted(widget):
    """True nur fuer ein von Qt bereits zerstoertes Widget.

    `sip` ist in Calibre vorhanden; ausserhalb (Tests, andere Qt-Bindings)
    bleibt die Pruefung aus, statt einen Importfehler zu riskieren.
    """
    try:
        import sip
    except ImportError:
        try:
            from PyQt6 import sip
        except ImportError:
            return False
    try:
        return bool(sip.isdeleted(widget))
    except (TypeError, RuntimeError):
        return False


def gui_guard(function):
    """GUI-Slot ausführen; ein bereits zerstörtes Fenster ist kein Fehler.

    Qt wirft RuntimeError, wenn ein Slot nach dem Schliessen des Fensters
    noch ein Widget anfasst. Das ist während des Calibre-Shutdowns normal.
    Ein echter RuntimeError aus dem Slot darf aber nicht verschluckt werden:
    deshalb wird nur bei nachweislich gelöschtem Widget geschluckt.
    """
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        target = args[0] if args else None
        if target is not None and _widget_deleted(target):
            return None
        try:
            return function(*args, **kwargs)
        except RuntimeError:
            if target is not None and _widget_deleted(target):
                return None
            raise
    return guarded


CANCEL_MESSAGE = "Abgebrochen."

# Ergebnis-Zustände je Operation. "not_started" ist bewusst ein eigener
# Zustand: nach einem Fehler oder Abbruch darf der Bericht nicht so tun,
# als wäre nichts passiert -- und auch nicht, als wäre alles versucht worden.
OP_SUCCEEDED = "succeeded"
OP_SKIPPED = "skipped"
OP_FAILED = "failed"
OP_NOT_STARTED = "not_started"

# Laufende oder nach einem Timeout noch nicht beendete Threads bleiben bis
# zum finished-Signal referenziert.
_THREADS = []


class CallWorker(QObject):
    """Run one value-only callback outside the GUI thread."""
    done = pyqtSignal(object)
    failed = pyqtSignal(object)

    def __init__(self, call):
        QObject.__init__(self)
        self.call = call

    def run(self):
        try:
            result = self.call()
        except Exception as exc:
            self.failed.emit(exc)
            return
        self.done.emit(result)


def start_worker(dialog, worker, thread, thread_name, worker_name,
                 stop_on=(), extra_slots=()):
    """Worker mit Thread verdrahten, Referenzen halten und starten."""
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    for signal in stop_on:
        signal.connect(thread.quit)
    for signal, slot in extra_slots:
        signal.connect(slot)
    thread.finished.connect(thread.deleteLater)
    _keep_thread_alive(thread)
    setattr(dialog, thread_name, thread)
    setattr(dialog, worker_name, worker)
    thread.start()
    return thread


def start_call(dialog, call, on_done, on_failed):
    """Einen Netzaufruf in einem eigenen QThread ausfuehren.

    `call` bekommt keine Widgets und keine Datenbank -- nur Werte, wie
    `PrepareWorker`. `on_done`/`on_failed` sind Methoden des Dialogs; Qt
    stellt sie in den GUI-Thread. Ohne Elternteil, damit ein geschlossenes
    Fenster keinen laufenden Thread zerstoert.
    """
    thread = QThread()
    worker = CallWorker(call)
    # Der Worker muss den Dialog ueberleben: ohne die Referenz kann Qt ihn
    # vor dem Thread-Start einsammeln, und der Dialog bleibt gesperrt.
    return start_worker(
        dialog, worker, thread, "_call_thread", "_call_worker",
        (worker.done, worker.failed),
        ((worker.done, on_done), (worker.failed, on_failed)))


def _discard_item(collection, item):
    """Remove `item` if present; a repeated signal must not raise."""
    try:
        collection.remove(item)
    except ValueError:
        pass


def _keep_thread_alive(thread):
    """Retain a thread until Qt reports that it has finished."""
    if thread not in _THREADS:
        _THREADS.append(thread)
        finished = getattr(thread, "finished", None)
        if finished is not None:
            finished.connect(lambda: _discard_item(_THREADS, thread))


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
        _keep_thread_alive(thread)
        try:
            if thread.isFinished():
                _discard_item(_THREADS, thread)
        except Exception:
            pass
    return finished


def _active_work_thread(dialog):
    """Gibt es einen laufenden Sync-/Vorbereitungs-Thread?"""
    return any(getattr(dialog, name, None) is not None
               for name in ("thread", "prepare_thread", "snapshot_thread"))


def _dialog_busy(dialog):
    """Läuft Sync, Vorbereitung oder Anmeldung? (ohne Qt-Abhängigkeit)"""
    login = getattr(dialog, "login_thread", None)
    return (_active_work_thread(dialog)
            or (login is not None and login.isRunning()))


def _set_enabled(owner, names, enabled):
    """Widgets sperren/freigeben -- nur vorhandene Widgets."""
    for name in names:
        setter = getattr(getattr(owner, name, None), "setEnabled", None)
        if setter is None:
            continue
        setter(enabled)


def _apply_busy(dialog, busy):
    """Disable sync controls while keeping the account switch available.

    Während Vergleich oder Sync darf das angezeigte Konto gewechselt werden;
    die laufende Aktion bleibt über RunContext an ihr Startkonto gebunden.
    Eine laufende Anmeldeprüfung blockiert den Wechsel dagegen weiterhin.
    """
    switchable = _active_work_thread(dialog)
    _set_enabled(dialog, ("account_new", "account_remove", "partner",
                          "login_button", "settings_button", "hardware",
                          "refresh", "formats"), not busy)
    # Während Vergleich/Sync bleibt die Kontoauswahl bedienbar. Das gilt
    # auch dann, wenn `_apply_busy` vor dem Setzen des Threads läuft.
    _set_enabled(dialog, ("account_quick", "account_select"),
                 not busy or switchable)


LOGIN_NEXT_STEP = "Anmeldung erneut starten; Details stehen im Calibre-Log."


def _login_failure_guidance(error):
    """Titel für einen fehlgeschlagenen Browser-Login.

    Der Titel ist der Kurztext der Exception -- die einzige Quelle für
    Fehlertexte; der nächste Schritt ist für alle Ursachen derselbe.
    """
    title = error.short if isinstance(error, TolinoError) else ""
    return str(title or "").strip() or "Anmeldung nicht abgeschlossen"


def _failed_operations(outcome):
    """Alle gescheiterten Schritte eines Laufs in ihrer Reihenfolge."""
    return [item for item in (outcome.get("operations") or ())
            if isinstance(item, dict) and item.get("status") == OP_FAILED]


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
    text = ", ".join(parts)
    if prefix:
        text = ("%s: %s" % (prefix, text)) if text else prefix
    # Ein einzelner Fehler haelt den Lauf nicht mehr auf -- dann
    # muss der Bericht sagen, WELCHE Schritte es getroffen hat.
    failed = _failed_operations(outcome)
    if failed:
        lines = [_operation_line(item) for item in failed[:5]]
        if len(failed) > 5:
            lines.append("- … und %d weitere" % (len(failed) - 5))
        text += "\n\nNicht abgeschlossen:\n" + "\n".join(lines)
    return text


def _operation_line(item):
    """Eine Zeile je gescheitertem Schritt -- Name und kurzer Grund."""
    item = item if isinstance(item, dict) else {}
    # `error` ist immer der bereits bereinigte Text aus sanitize_error().
    detail = str(item.get("error") or "")
    code_match = re.search(r"\(Code ([A-Z][0-9]{2})\)", detail)
    if code_match and code_match.group(1) not in detail[-16:]:
        detail += " (Code %s)" % code_match.group(1)
    return "- %s%s" % (item.get("label") or "Schritt",
                       (": " + detail) if detail else "")


def sync_result_detail(outcome):
    """Komplette Liste der gescheiterten Schritte fuer den Detailtext.

    Der sichtbare Text nennt hoechstens fuenf (sonst wird der Dialog
    unlesbar); bei mehr als fuenf Fehlern steht hier jeder einzelne Schritt
    mit Titel und Grund. Leer heisst: kein Detailtext noetig.
    """
    outcome = outcome if isinstance(outcome, dict) else {}
    failed = _failed_operations(outcome)
    if len(failed) <= 5:
        return ""
    return "\n".join(_operation_line(item) for item in failed)



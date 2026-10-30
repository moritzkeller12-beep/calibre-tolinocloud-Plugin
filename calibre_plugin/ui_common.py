"""Gemeinsame Qt-Importe, Konstanten und Hilfsfunktionen der Oberfläche.

Alle UI-Module beziehen Qt-Namen und geteilte Helfer von hier, damit ein
Qt-Stub in den Tests nur an einer Stelle greift.
"""
import functools
import re

try:
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QHeaderView, QLabel,
                         QLineEdit, QMessageBox, QMenu,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QObject, QInputDialog,
                         QFileDialog, Qt, pyqtSignal)
except ImportError:
    # Some Calibre Qt builds expose the signal type as Signal.
    from qt.core import (QCheckBox, QComboBox, QDialog,
                         QFormLayout, QGroupBox, QHeaderView, QLabel,
                         QLineEdit, QMessageBox, QMenu,
                         QProgressBar, QPushButton, QThread,
                         QVBoxLayout, QHBoxLayout, QTableWidget,
                         QTableWidgetItem, QObject, QInputDialog,
                         QFileDialog, Qt, Signal as pyqtSignal)

# Namen, die andere UI-Module ueber dieses Modul beziehen.
__all__ = [
    "CANCEL_MESSAGE", "OP_FAILED", "OP_NOT_STARTED", "OP_SKIPPED",
    "OP_SUCCEEDED", "QCheckBox", "QComboBox", "QDialog",
    "QFileDialog", "QFormLayout", "QGroupBox", "QHBoxLayout", "QHeaderView",
    "QMenu", "QInputDialog", "QLabel", "QLineEdit", "QMessageBox", "QObject",
    "QProgressBar", "QPushButton", "QTableWidget", "QTableWidgetItem",
    "QThread", "QVBoxLayout", "Qt",
    "_THREADS", "_apply_busy", "_dialog_busy",
    "_login_failure_guidance",
    "button", "dialog_code", "echo_mode",
    "gui_guard", "header_mode", "item_flag",
    "check_state", "selection_behavior",
    "selection_mode", "sort_order",
    "start_worker", "_stop_thread",
    "_worker_account_name", "_worker_outcome", "_set_enabled",
    "_keep_thread_alive", "_thread_running",
    "pyqtSignal",
    "sync_result_detail",
    "sync_result_text",
]


def _qt_value(owner, name, scope):
    """Qt-Konstante in alter und skopierter Schreibweise.

    Qt6 (Calibre 9) kennt nur ``QHeaderView.ResizeMode.Stretch``; die alte
    Form ``QHeaderView.Stretch`` fehlt dort und wirft AttributeError. Die
    Test-Stubs kennen wiederum keine Enums. Der Helfer nimmt, was da ist.
    """
    holder = getattr(owner, scope, None)
    return getattr(holder, name) if holder is not None else getattr(owner, name)


def header_mode(name):
    """Resize-Modus einer Tabellenspalte, unabhängig von der Qt-Version."""
    return _qt_value(QHeaderView, name, "ResizeMode")


def button(name):
    """Standardknopf eines Meldungsfensters, unabhängig von der Qt-Version."""
    return _qt_value(QMessageBox, name, "StandardButton")


def dialog_code(name):
    """Ergebniscode eines Dialogs, unabhängig von der Qt-Version."""
    return _qt_value(QDialog, name, "DialogCode")


def selection_behavior(name):
    """Auswahlverhalten einer Tabelle (Qt5 unskopiert, Qt6 als Enum)."""
    return _qt_value(QTableWidget, name, "SelectionBehavior")


def selection_mode(name):
    """Auswahlmodus einer Tabelle (Strg-/Shift-Klick)."""
    return _qt_value(QTableWidget, name, "SelectionMode")


def echo_mode(name):
    """Anzeigemodus eines Eingabefelds (z. B. Password)."""
    return _qt_value(QLineEdit, name, "EchoMode")


def sort_order(name):
    """Sortierreihenfolge einer Tabellenspalte."""
    return _qt_value(Qt, name, "SortOrder")


def item_flag(name):
    """Flag eines Tabelleneintrags (Qt5 unskopiert, Qt6 als Enum)."""
    return _qt_value(Qt, name, "ItemFlag")


def check_state(name):
    """Checkzustand eines Tabelleneintrags (Qt5 unskopiert, Qt6 als Enum)."""
    return _qt_value(Qt, name, "CheckState")


def gui_guard(function):
    """GUI-Slot ausführen; ein bereits zerstörtes Fenster ist kein Fehler.

    Qt wirft RuntimeError, wenn ein Slot nach dem Schliessen des Fensters
    noch ein Widget anfasst. Das ist während des Calibre-Shutdowns normal
    und darf keinen Traceback im Log hinterlassen.
    """
    @functools.wraps(function)
    def guarded(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except RuntimeError:
            return None
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


def _start_thread(dialog, worker, thread, thread_name, worker_name,
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
    return _start_thread(
        dialog, worker, thread, "_call_thread", "_call_worker",
        (worker.done, worker.failed),
        ((worker.done, on_done), (worker.failed, on_failed)))


def start_worker(dialog, worker, thread, thread_name, worker_name, stop_on=()):
    """Run ``worker.run`` in ``thread`` and remember both on the dialog.

    ``stop_on`` sind die Signale, nach denen der Worker fertig ist; sie
    beenden die Thread-Schleife. Ohne sie beendet der Aufrufer den Thread
    selbst (``_stop_thread``). Der Dialog haelt die Referenzen, damit ein
    geschlossenes Fenster den laufenden Thread nicht zerstoert.
    """
    return _start_thread(dialog, worker, thread, thread_name, worker_name,
                         stop_on)


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


def _worker_account_name(dialog):
    """Account of the running/finished worker -- never the visible one."""
    worker = getattr(dialog, "worker", None)
    name = getattr(worker, "account_name", None)
    return str(name or getattr(dialog, "account_name", "") or "")


def _worker_outcome(dialog):
    worker = getattr(dialog, "worker", None)
    return dict(getattr(worker, "outcome", None) or {})


def _thread_running(thread):
    """Läuft dieser Thread noch?"""
    return thread is not None and thread.isRunning()


def _dialog_busy(dialog):
    """Läuft Sync, Vorbereitung oder Anmeldung? (ohne Qt-Abhängigkeit)"""
    if getattr(dialog, "thread", None) is not None:
        return True
    if getattr(dialog, "prepare_thread", None) is not None:
        return True
    return _thread_running(getattr(dialog, "login_thread", None))


def _set_enabled(owner, names, enabled):
    """Widgets sperren/freigeben -- nur vorhandene Widgets."""
    for name in names:
        setter = getattr(getattr(owner, name, None), "setEnabled", None)
        if setter is None:
            continue
        setter(enabled)


def _apply_busy(dialog, busy):
    """Disable account/partner/sync controls while an operation runs.

    Der Statustext kommt von den Startern -- sie wissen, was läuft; hier
    würde nur geraten.
    """
    _set_enabled(dialog, ("account_quick", "account_select", "account_new",
                          "account_remove", "partner", "login_button",
                          "settings_button", "hardware", "refresh",
                          "formats"),
                 not busy)


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



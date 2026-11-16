"""Kindprozess fuer `DashboardCloseExitTests`: Fenster oeffnen, schliessen, enden.

Startet genau den Plugin-Pfad (`InterfaceAction.show_dashboard` ->
`SyncDashboard.exec()`), haelt ueber den Vault-Lock einen echten Worker im
Lauf und schliesst das Fenster mitten darin. Danach muss der Prozess von
selbst mit 0 enden -- ohne Timeout-Notausstieg und ohne `os._exit`.

Die Umgebung ist isoliert: temporaeres HOME/XDG, `QT_QPA_PLATFORM=offscreen`,
kein Konto und damit kein Netzaufruf.
"""
import faulthandler
import os
import sys
import threading
import time

sys.path.insert(0, os.environ["TOLINO_EXIT_ROOT"])

from qt.core import QApplication, QTimer, QWidget

from calibre.db.legacy import LibraryDatabase
from calibre_plugin import ui, ui_common
from calibre_plugin.config import load_state
from calibre_plugin.library import column_state, library_scope
from calibre_plugin.run_context import RunContext
from calibre_plugin.token_vault import TOKEN_VAULT
from calibre_plugin.ui_dashboard import SyncDashboard

app = QApplication([])
# Ein leerer Ordner wird von Calibre selbst als Bibliothek angelegt.
database = LibraryDatabase(os.environ["TOLINO_EXIT_LIBRARY"])


class Gui(QWidget):
    def __init__(self):
        QWidget.__init__(self)
        self.current_db = database


def dialog():
    for widget in QApplication.topLevelWidgets():
        if isinstance(widget, SyncDashboard):
            return widget
    return None


def start_prepare():
    """Echter Worker ueber die Starter-Methode des Dialogs, im Lauf gehalten."""
    window = dialog()
    if window is None:
        print("KEIN-FENSTER", flush=True)
        return
    TOKEN_VAULT._lock.acquire()
    context = RunContext.from_values(
        window.values(), column_state(database),
        library_scope=library_scope(database))
    window._start_prepare_worker(context, {}, load_state({}))
    print("WORKER-GESTARTET", flush=True)


def close_window():
    window = dialog()
    if window is None:
        print("KEIN-FENSTER-BEIM-SCHLIESSEN", flush=True)
        return
    window.close()
    print("FENSTER-GESCHLOSSEN Referenzen=%r" % (
        (window.prepare_thread, window.prepare_worker),), flush=True)


def release_lock():
    if TOKEN_VAULT._lock.locked():
        TOKEN_VAULT._lock.release()
    print("LOCK-FREI", flush=True)


def main():
    QTimer.singleShot(700, start_prepare)
    QTimer.singleShot(1300, close_window)
    QTimer.singleShot(2600, release_lock)
    # Genau der Plugin-Pfad: modal oeffnen, bis das Fenster geschlossen wird.
    # Haengt der Prozess doch, zeigt der Dump die Stelle im Testbericht.
    QTimer.singleShot(6000, faulthandler.dump_traceback)
    ui.TolinoSyncAction.show_dashboard(type("Host", (), {"gui": Gui()})())
    deadline = time.time() + 8
    while ui_common._WORKERS and time.time() < deadline:
        app.processEvents()
        time.sleep(0.05)
    app.processEvents()
    me = threading.current_thread()
    blocking = [thread.name for thread in threading.enumerate()
                if thread.is_alive() and not thread.daemon and thread is not me]
    print("BLOCKING=%d %s" % (len(blocking), ",".join(blocking)), flush=True)
    print("WORKERS=%d" % len(ui_common._WORKERS), flush=True)
    print("OK-EXIT" if not blocking else "BLOCKIERT", flush=True)
    return 0 if not blocking else 1


if __name__ == "__main__":
    sys.exit(main())

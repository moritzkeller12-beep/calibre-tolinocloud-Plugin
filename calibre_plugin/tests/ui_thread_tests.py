"""Thread-Lebenszyklus der Oberflaeche mit einem kontrollierten Doppel.

Kein echter QThread, keine Wartezeiten: geprueft wird, wann Referenzen
gehalten und wann sie freigegeben werden.
"""
import types
import unittest
from unittest.mock import patch

from ..errors import TolinoAuthError
from ._support import SyncThread, real_qt_stub, ui_module_for


class WorkerDouble:
    """Worker ohne Signale: nur Thread-Zuordnung und run()."""

    def __init__(self):
        self.thread = None
        self.runs = 0

    def moveToThread(self, thread):
        self.thread = thread

    def run(self):
        self.runs += 1


class ThreadLifecycleTests(unittest.TestCase):
    """Referenzen bleiben bis finished -- auch nach einem Timeout."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.common = self.ui.common
        self._saved = (list(self.common._THREADS),
                       list(self.common._WORKERS))
        self.addCleanup(self._restore)

    def _restore(self):
        self.common._THREADS[:] = self._saved[0]
        self.common._WORKERS[:] = self._saved[1]

    def start(self, thread=None, worker=None):
        dialog = types.SimpleNamespace()
        thread = thread or SyncThread()
        worker = worker or WorkerDouble()
        self.common.start_worker(dialog, worker, thread, "thread", "worker")
        return dialog, thread, worker

    def test_references_are_held_until_finished(self):
        dialog, thread, worker = self.start()
        self.assertEqual(1, worker.runs)
        self.assertIs(thread, dialog.thread)
        self.assertIs(worker, dialog.worker)
        self.assertIn(thread, self.common._THREADS)
        self.assertIn(worker, self.common._WORKERS)
        thread.finished.emit()
        self.assertNotIn(thread, self.common._THREADS)
        self.assertNotIn(worker, self.common._WORKERS)
        self.assertTrue(thread.deleted)

    def test_closing_the_dialog_does_not_drop_a_running_worker(self):
        dialog, thread, worker = self.start()
        # closeEvent gibt seine eigenen Referenzen ab; der Lauf bleibt sicher.
        dialog.thread = dialog.worker = None
        self.assertIn(worker, self.common._WORKERS)
        thread.finished.emit()
        self.assertNotIn(worker, self.common._WORKERS)

    def test_a_timeout_keeps_the_thread_referenced(self):
        thread = SyncThread(wait_result=False)
        self.start(thread=thread)
        self.assertFalse(self.common._stop_thread(thread))
        self.assertIn(thread, self.common._THREADS)
        thread.finished.emit()
        self.assertNotIn(thread, self.common._THREADS)

    def test_a_finished_thread_is_released(self):
        thread = SyncThread()
        self.start(thread=thread)
        self.assertTrue(self.common._stop_thread(thread))
        self.assertTrue(thread.stopped)


class CallWorkerTests(unittest.TestCase):
    """start_call fuehrt den Aufruf aus und beendet den Thread ueber Signale."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())
        self.common = self.ui.common
        self._saved = (list(self.common._THREADS),
                       list(self.common._WORKERS))
        self.addCleanup(self._restore)

    def _restore(self):
        self.common._THREADS[:] = self._saved[0]
        self.common._WORKERS[:] = self._saved[1]

    def test_the_result_reaches_the_dialog_and_stops_the_thread(self):
        dialog = types.SimpleNamespace()
        results = []
        self.common.start_call(dialog, lambda: 7, results.append,
                               results.append)
        self.assertEqual([7], results)
        self.assertTrue(dialog._call_thread.stopped)
        self.assertIn(dialog._call_worker, self.common._WORKERS)

    def test_an_error_reaches_the_dialog_and_stops_the_thread(self):
        dialog = types.SimpleNamespace()
        failures = []

        def broken():
            raise OSError("kaputt")

        self.common.start_call(dialog, broken, lambda _value: None,
                               failures.append)
        self.assertEqual(1, len(failures))
        self.assertIsInstance(failures[0], OSError)
        self.assertTrue(dialog._call_thread.stopped)


class AuthWorkerTests(unittest.TestCase):
    """Anmelde-Worker: erst speichern, dann melden."""

    def setUp(self):
        self.ui = ui_module_for(self, qt_stub=real_qt_stub())

    def worker(self, **patches):
        module = self.ui.auth_workers
        stack = [patch.object(module, name, **values)
                 for name, values in patches.items()]
        for patcher in stack:
            patcher.start()
            self.addCleanup(patcher.stop)
        worker = module.BrowserLoginWorker("A", "alt-hw")
        events = []
        worker.completed.connect(
            lambda hardware: events.append(("completed", hardware)))
        worker.failed.connect(lambda exc: events.append(("failed", exc)))
        return worker, events

    def test_the_token_is_saved_before_completed_is_emitted(self):
        saved = []
        worker, events = self.worker(
            grab_live_refresh={"return_value": ("neu", "hw", 1)},
            save_credentials={"side_effect": lambda *args: saved.append(args)})
        worker.run()
        self.assertEqual([("A", "neu", "hw", 1)], saved)
        self.assertEqual([("completed", "hw")], events)

    def test_a_failed_grab_reports_failed_without_completing(self):
        worker, events = self.worker(
            grab_live_refresh={"side_effect": TolinoAuthError("B02")},
            save_credentials={"side_effect": AssertionError("darf nicht")})
        worker.run()
        self.assertEqual("failed", events[0][0])
        self.assertNotIn("completed", [name for name, _value in events])

    def test_a_failed_save_is_reported_and_not_completed(self):
        worker, events = self.worker(
            grab_live_refresh={"return_value": ("neu", "hw", 1)},
            save_credentials={"side_effect": OSError("Platte voll")})
        worker.run()
        self.assertEqual("failed", events[0][0])
        self.assertNotIn("completed", [name for name, _value in events])

    def test_a_save_that_returns_false_is_reported_and_not_completed(self):
        worker, events = self.worker(
            grab_live_refresh={"return_value": ("neu", "hw", 1)},
            save_credentials={"return_value": False})
        worker.run()
        self.assertEqual("failed", events[0][0])
        self.assertNotIn("completed", [name for name, _value in events])

    def test_the_session_check_maps_each_status_to_its_signal(self):
        cases = (("valid", ""), ("busy", "Keep-alive"),
                 ("invalid", "Token abgelehnt"))
        for status, detail in cases:
            with self.subTest(status=status):
                module = self.ui.auth_workers
                with patch.object(module, "check_stored_login",
                                  return_value=(status, detail)):
                    worker = module.SessionCheckWorker(
                        types.SimpleNamespace(account_name="A",
                                              secrets=lambda: ()))
                    seen = []
                    worker.valid.connect(lambda: seen.append(("valid", "")))
                    worker.invalid.connect(
                        lambda text: seen.append(("invalid", text)))
                    worker.busy.connect(lambda text: seen.append(("busy", text)))
                    worker.run()
                self.assertEqual([(status, detail)], seen)

    def test_an_unexpected_error_in_the_check_is_reported_as_invalid(self):
        module = self.ui.auth_workers
        with patch.object(module, "check_stored_login",
                          side_effect=RuntimeError("kaputt")):
            worker = module.SessionCheckWorker(
                types.SimpleNamespace(account_name="A", secrets=lambda: ()))
            messages = []
            worker.invalid.connect(messages.append)
            worker.run()
        self.assertEqual(1, len(messages))
        self.assertIn("kaputt", messages[0])


if __name__ == "__main__":
    unittest.main()

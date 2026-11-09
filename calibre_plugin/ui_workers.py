"""Hintergrund-Worker für Vorbereitung, Synchronisierung und Anmeldung."""
import copy
from collections import Counter

from .ui_common import (CANCEL_MESSAGE, OP_FAILED, OP_NOT_STARTED, OP_SKIPPED,
                        OP_SUCCEEDED, QObject, pyqtSignal)
from .token_vault import TOKEN_VAULT
from .config import account_secrets, load_state, save_account
from .plan import plan_collection_updates, remember_collection
from .comparison import (compare_inventory, content_fingerprint,
                         metadata_fingerprint, reading_state,
                         store_reading_state)
from .dashboard_plan import local_metadata
from .identifiers import (adopt_column_tolino_ids, display_title, state_entry,
                          valid_book_uuid)
from .library import image_suffix
from .tolino import (TolinoApiError, TolinoAuthError, TolinoError, log_warning,
                     report_error, sanitize_error)


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, context, jobs, removals, meta_updates=None,
                 series_map=None, read_changes=None, titles=None,
                 metadata=None, state=None):
        QObject.__init__(self)
        # Alle Schreibvorgänge gehören zum Startkonto dieses Laufs.
        self.context = context
        self.account_name = self.context.account_name
        self.jobs = list(jobs or ())
        self.removals = list(removals or ())
        self.meta_updates = list(meta_updates or ())
        self.series_map = dict(series_map or {})
        # (tolino-ID, Calibre-Nummer, gelesen?) je ausdrücklich gewählter
        # Änderung aus dem Vergleichsfenster. Leer heisst: Lesestatus bleibt
        # unangetastet und die Spalte unverändert.
        self.read_changes = [
            (tolino_id, change[1], bool(change[2]),
             valid_book_uuid(change[3]) if len(change) > 3 else "")
            for change in (read_changes or ())
            if isinstance(change, (tuple, list)) and len(change) >= 3
            and (tolino_id := str(change[0] or "").strip())
        ]
        self.read_marks = [change[0] for change in self.read_changes
                           if change[2]]
        self.read_unmarks = [change[0] for change in self.read_changes
                             if not change[2]]
        self.titles = {str(key): str(value)
                       for key, value in (titles or {}).items()
                       if value}
        self.metadata_by_uuid = {
            str(item["uuid"]): item
            for item in (metadata or {}).values()
            if isinstance(item, dict) and item.get("uuid")
        }
        self.cancelled = False
        # Der Worker ändert und speichert eine private Zustandskopie.
        self.state = copy.deepcopy(state) if isinstance(state, dict) else {}
        self.updates = []
        # (Calibre-Nummer, gelesen?) je bestätigter Cloud-Änderung.
        self.read_updates = []
        self.operations = []
        self.outcome = {
            "uploaded": 0, "deleted": 0,
            "metadata": 0, "read_marked": 0, "read_unmarked": 0,
            "collections": 0, "skipped": 0, "failed": 0,
            "cancelled": False, "operations": self.operations,
        }

    def cancel(self):
        self.cancelled = True

    def _label(self, book_uuid, book_id=None, title=""):
        """Sichtbarer Name eines Buchs: Titel, sonst «Buch <Nummer>».

        Titel kommen aus dem Plan (`titles_for`); der Zustand liefert nur
        noch die Calibre-Nummer, wenn der Aufrufer keine kennt.
        """
        text = (str(title or "").strip()
                or self.titles.get(str(book_uuid), ""))
        entry = (self.state.get(str(book_uuid))
                 if isinstance(self.state, dict) else None)
        if book_id is None and isinstance(entry, dict):
            book_id = entry.get("calibre_id")
        return display_title(text, book_id)

    def _add_operation(self, kind, label, status=OP_NOT_STARTED, **extra):
        operation = dict(extra, kind=kind, label=label, status=status)
        self.operations.append(operation)
        return operation

    def _finish_operation(self, operation, status, **extra):
        """Den Status am Schritt fortschreiben; gezählt wird am Abschluss."""
        if operation is not None:
            operation.update(extra, status=status)

    def _refresh_counts(self):
        """Ergebnis EINMAL aus allen Operationen neu berechnen."""
        kinds, states = Counter(), Counter()
        for operation in self.operations:
            states[operation["status"]] += 1
            if operation["status"] == OP_SUCCEEDED:
                kinds[operation["kind"]] += int(operation.get("count") or 1)
        self.outcome.update(
            uploaded=kinds["upload"],
            deleted=kinds["delete"],
            metadata=kinds["metadata"],
            read_marked=kinds["read"],
            read_unmarked=kinds["unread"],
            collections=kinds["collection"],
            skipped=states[OP_SKIPPED],
            failed=states[OP_FAILED],
            cancelled=bool(self.cancelled))

    def _fail(self, operation, exc, client=None, fatal=False):
        """Einen Schritt als gescheitert zaehlen; `fatal` beendet den Lauf.

        Ein fehlendes Cover oder eine einzelne Datei darf die uebrigen
        Buecher nicht verhindern. Nur eine beschädigte Anmeldung/Sitzung ist
        fatal: danach liefert jeder weitere Aufruf denselben Fehler. Der
        Grund steht am Schritt (fuer den Abschlussbericht) und im Log -- ohne
        Zugangsdaten.
        """
        detail = sanitize_error(exc, account_secrets(self.account_name)
                                + (getattr(client, "access", None),))
        self._finish_operation(operation, OP_FAILED, error=detail)
        log_warning("Schritt gescheitert (%s): %s"
                    % ((operation or {}).get("label") or "?", detail))
        if fatal and isinstance(exc, TolinoAuthError):
            raise exc

    def _step(self, client, checkpoint, operation, text, action, done, total,
              finish=None):
        """Einen Schritt ausfuehren: Abbruch pruefen, melden, zaehlen.

        `action` laeuft genau einmal. Ein Fehler wird am Schritt gezaehlt
        (fatale Fehler gehen weiter nach oben) und der Aufrufer entscheidet,
        ob der Lauf weitergeht. `finish` verbucht den Erfolg mit dem Ergebnis
        von `action`; ohne Angabe gilt der Schritt als gelungen.
        Rueckgabe: ``(ok, Ergebnis)``.
        """
        checkpoint()
        self.progress.emit(done, total, text)
        try:
            result = action()
        except Exception as exc:
            self._fail(operation, exc, client, fatal=True)
            return False, None
        if finish is None:
            self._finish_operation(operation, OP_SUCCEEDED)
        else:
            finish(result)
        return True, result

    def _persist_state(self):
        """Den Zustand des Kontos sofort sichern.

        Nach jedem Upload ist die Tolino-ID nicht mehr rueckgaengig zu
        machen; deshalb schreibt der Worker den vollen Konto-Zustand, statt
        ein separates Journal zu fuehren.
        """
        if not self.account_name:
            return
        try:
            save_account(self.account_name, {"state": self.state},
                         create=False)
        except Exception as exc:
            raise TolinoError(
                "Der Sync-Zwischenstand konnte nicht gespeichert werden. "
                "Bereits durchgeführte Cloud-Änderungen bleiben bestehen; "
                "bitte vor einem erneuten Lauf den Cloud-Bestand prüfen.",
                short=("Der Sync-Zwischenstand konnte nicht gespeichert werden. "
                       "Cloud-Änderungen bleiben bestehen; bitte den Bestand "
                       "vor einem erneuten Lauf prüfen."),
                detail=sanitize_error(exc, account_secrets(self.account_name))) from exc

    @staticmethod
    def _delete_or_skip(client, deliverable_id):
        """Return False when the delete endpoint explicitly reports HTTP 404."""
        try:
            client.delete(deliverable_id)
            return True
        except TolinoApiError as exc:
            text = (str(exc) + " " + str(getattr(exc, "detail", ""))).casefold()
            if exc.code == "N03" and "http 404" in text:
                return False
            raise

    def _finish_delete(self, operation, deleted):
        """Eine Löschoperation als erfolgreich oder übersprungen verbuchen."""
        self._finish_operation(
            operation, OP_SUCCEEDED if deleted else OP_SKIPPED,
            **({} if deleted else {"reason": "nicht mehr in der Cloud"}))

    def _plan_operations(self):
        """Plan selected actions; collections are added after uploads."""
        meta_ops = []
        for update in self.meta_updates:
            fields = update.fields or {}
            meta_ops.append((update, self._add_operation(
                "metadata", "Metadaten: %s"
                % self._label(update.book_uuid, update.book_id,
                              fields.get("title")),
                book_uuid=update.book_uuid)))
        upload_ops = []
        for job in self.jobs:
            upload_ops.append((job, self._add_operation(
                "upload", "Hochladen: %s (%s)" %
                (job.label(), job.format_name),
                book_uuid=job.book_uuid, format=job.format_name)))
        delete_ops = []
        for book_uuid, tolino_id in self.removals:
            delete_ops.append((book_uuid, tolino_id, self._add_operation(
                "delete", "Aus tolino löschen: %s"
                % self._label(book_uuid, None,
                              self.titles.get(str(tolino_id), "")),
                book_uuid=book_uuid, tolino_id=str(tolino_id))))
        read_op = (self._add_operation(
            "read", "Als gelesen markieren", count=len(self.read_marks))
            if self.read_marks else None)
        unread_op = (self._add_operation(
            "unread", "Als ungelesen markieren",
            count=len(self.read_unmarks)) if self.read_unmarks else None)
        total = sum(map(len, (meta_ops, upload_ops, delete_ops)))
        total += bool(read_op) + bool(unread_op)
        return meta_ops, upload_ops, delete_ops, read_op, unread_op, total

    def _remove_replaced_copy(self, client, job):
        """Delete the explicitly replaced cloud copy without a preflight read."""
        if not job.old_id:
            return
        operation = self._add_operation(
            "delete", "Alte Kopie löschen: %s" % job.label(),
            book_uuid=job.book_uuid)
        try:
            deleted = self._delete_or_skip(client, job.old_id)
        except Exception as exc:
            self._fail(operation, exc, client, fatal=True)
        else:
            self._finish_delete(operation, deleted)

    def _run_deletions(self, client, delete_ops, checkpoint, state, done, total):
        """Execute only the deletions explicitly selected by the user."""
        for book_uuid, tolino_id, operation in delete_ops:
            def finish(deleted, _operation=operation):
                self._finish_delete(_operation, deleted)

            ok, deleted = self._step(
                client, checkpoint, operation,
                "Lösche aus tolino: %s"
                % self._label(book_uuid, None,
                              self.titles.get(str(tolino_id), "")),
                lambda _client=client, _tolino_id=tolino_id:
                self._delete_or_skip(_client, _tolino_id),
                done, total, finish=finish)
            if not ok:
                done += 1
                continue
            entry = state.get(book_uuid)
            recorded = (str(entry.get("tolino_id") or "")
                        if isinstance(entry, dict) else "")
            if not recorded or recorded == str(tolino_id):
                if isinstance(entry, dict) and entry.get("calibre_id") is not None:
                    self.updates.append((entry.get("calibre_id"), ""))
                state.pop(book_uuid, None)
                self._persist_state()
            done += 1
            if deleted:
                self.progress.emit(done, total,
                                   "Erledigt: %s" % self._label(book_uuid))
        return done

    def _run_read_marks(self, client, read_op, unread_op, checkpoint,
                        done, total):
        """Ausgewählten Lesestatus übertragen."""
        actions = (
            (read_op, self.read_marks, True,
             "Als gelesen markieren", "Als gelesen markiert"),
            (unread_op, self.read_unmarks, False,
             "Als ungelesen markieren", "Als ungelesen markiert"),
        )
        for operation, book_ids, finished, start_text, done_text in actions:
            if operation is None:
                continue
            ok, _result = self._step(
                client, checkpoint, operation, start_text,
                lambda _client=client, _book_ids=book_ids,
                       _finished=finished:
                _client.mark_read_many(_book_ids, finished=_finished),
                done, total)
            if ok:
                stored = False
                for _tolino, book_id, state, book_uuid in self.read_changes:
                    if state is not finished:
                        continue
                    if book_id is not None:
                        self.read_updates.append((book_id, finished))
                    stored = self._store_read_state(book_uuid, finished) or stored
                if stored:
                    self._persist_state()
                self.progress.emit(done, total, done_text)
            done += 1
        return done

    def _store_read_state(self, book_uuid, finished):
        """Bestätigten Lesestatus im Account-State dieses Laufs sichern."""
        key = valid_book_uuid(book_uuid)
        if not key:
            return False
        entry = self.state.get(key)
        if not isinstance(entry, dict):
            entry = {}
            self.state[key] = entry
        reading = reading_state(entry)
        if reading.get("read") is bool(finished):
            return False
        reading["read"] = bool(finished)
        return True

    def _load_collection_pairs(self, client, collection_ops, done, total):
        """Cloud-Zuordnungen lesen; ein Fehler zaehlt jede Aktion als beendet."""
        try:
            return client.collection_pairs(), done
        except Exception as exc:
            # Ohne Bestand liesse sich nicht erkennen, was schon zugeordnet
            # ist -- jede Zuordnung gilt als gescheitert.
            for _book, _tolino, _series, operation in collection_ops:
                self._fail(operation, exc, client, fatal=True)
                # Auch ein gescheiterter Schritt ist abgeschlossen; sonst
                # bliebe der Fortschrittsbalken am Beginn der Phase stehen.
                done += 1
                self.progress.emit(
                    done, total, "Fehlgeschlagen: %s"
                    % (operation.get("label") or "Sammlung"))
            return None, done

    def _plan_collection_operations(self):
        """Offene Serienzuordnungen als Operationen vormerken."""
        operations = []
        for book_uuid, tolino_id, series in plan_collection_updates(
                self.series_map, self.state):
            operations.append((
                book_uuid, tolino_id, series,
                self._add_operation(
                    "collection", "Serie in tolino: %s" % series,
                    book_uuid=book_uuid, series=series)))
        return operations

    def _run_collections(self, client, collection_ops, checkpoint, done,
                         total):
        """Serien → Cloud-Sammlungen: nur die offenen Zuordnungen."""
        if not collection_ops:
            return done
        known, done = self._load_collection_pairs(
            client, collection_ops, done, total)
        revision = None
        changed = False
        # Ohne Bestand ist keine Zuordnung möglich: dann startet kein
        # Schritt -- auch kein Checkpoint.
        for book_uuid, tolino_id, series, operation in (
                collection_ops if known is not None else ()):
            checkpoint()
            if (tolino_id, series) in known:
                remember_collection(self.state, book_uuid, series)
                self._finish_operation(operation, OP_SKIPPED,
                                       reason="schon in der Cloud")
                changed = True
                done += 1
                continue

            ok, new_revision = self._step(
                client, checkpoint, operation,
                "Serie in der Cloud: %s" % series,
                lambda _client=client, _tolino_id=tolino_id,
                       _series=series, _revision=revision:
                _client.add_to_collection(
                    _tolino_id, _series, revision=_revision),
                done, total)
            if not ok:
                # Nach einem Fehler ist die Revision nicht mehr verlaesslich.
                revision = None
                done += 1
                continue

            revision = new_revision
            known.add((tolino_id, series))
            remember_collection(self.state, book_uuid, series)
            changed = True
            done += 1
        if changed:
            self._persist_state()
        self.progress.emit(done, total, "Serien geprüft")
        return done

    def _run_metadata_updates(self, client, operations, checkpoint, done, total):
        """Apply selected metadata patches and persist their confirmed state."""
        changed = False
        for record, operation in operations:
            ok, _result = self._step(
                client, checkpoint, operation,
                "Metadaten: %s" % self._label(
                    record.book_uuid, record.book_id,
                    (record.fields or {}).get("title")),
                lambda _client=client, _record=record:
                _client.update_metadata(_record.tolino_id, _record.fields),
                done, total)
            if not ok:
                done += 1
                continue
            entry = self.state.get(record.book_uuid)
            metadata = self.metadata_by_uuid.get(record.book_uuid)
            if isinstance(entry, dict) and metadata:
                entry["meta_verified_fp"] = metadata_fingerprint(metadata)
            self.updates.append((record.book_id, record.tolino_id))
            changed = True
            done += 1
        if changed:
            # Einmal je Phase statt je Buch: derselbe Patch ist idempotent.
            self._persist_state()
        return done

    def _run_uploads(self, client, operations, checkpoint, done, total):
        """Upload selected books and preserve each successful partial result."""
        for job, operation in operations:
            ok, new_id = self._step(
                client, checkpoint, operation,
                "Lade hoch: %s (%s)" % (job.label(), job.format_name),
                lambda _client=client, _job=job: _client.upload(_job.path),
                done, total)
            if not ok:
                done += 1
                self.progress.emit(done, total, "Fehlgeschlagen: %s"
                                   % job.label())
                continue
            entry = dict(state_entry(self.state, job.book_uuid))
            entry.update({"tolino_id": new_id, "calibre_id": job.book_id})
            entry.pop("collections", None)
            self.state[job.book_uuid] = entry
            metadata = self.metadata_by_uuid.get(job.book_uuid)
            if metadata:
                entry["content_fp"] = content_fingerprint(
                    metadata, job.format_name)
                # Ein Upload bestätigt die Metadaten nicht per Read-back.
                entry.pop("meta_verified_fp", None)
            self.updates.append((job.book_id, new_id))
            # Der Upload ist nicht rueckgaengig zu machen: die neue ID muss
            # sofort im Konto stehen, nicht erst am Ende der Phase.
            self._persist_state()
            if job.cover:
                try:
                    client.upload_cover(new_id, job.cover,
                                        image_suffix(job.cover))
                except Exception as exc:
                    cover_op = self._add_operation(
                        "cover", "Buchcover: %s" % job.label(),
                        book_uuid=job.book_uuid)
                    self._fail(cover_op, exc, client, fatal=True)
            # Liefert tolino beim Ersetzen dieselbe ID, ist das die neue
            # Kopie -- sie darf nicht als «alte» gelöscht werden.
            if job.old_id and str(job.old_id) != str(new_id):
                self._remove_replaced_copy(client, job)
            done += 1
            self.progress.emit(done, total, "Hochgeladen: %s" % job.label())
        return done

    def run(self):
        state = self.state
        client = None
        try:
            client = TOKEN_VAULT.login(self.account_name,
                                       self.context.partner_id)
            (meta_ops, upload_ops, delete_ops, read_op, unread_op,
             total) = self._plan_operations()
            done = 0

            def checkpoint():
                if not self.cancelled:
                    return
                # Alle geplanten Operationen, die noch nicht gestartet sind,
                # bleiben «nicht begonnen» -- der Bericht weist sie aus.
                self._refresh_counts()
                self.completed.emit(state, self.updates)
                raise _SyncCancelled()

            done = self._run_metadata_updates(
                client, meta_ops, checkpoint, done, total)
            done = self._run_uploads(
                client, upload_ops, checkpoint, done, total)
            done = self._run_deletions(
                client, delete_ops, checkpoint, state, done, total)
            done = self._run_read_marks(
                client, read_op, unread_op, checkpoint, done, total)
            collection_ops = self._plan_collection_operations()
            total += len(collection_ops)
            done = self._run_collections(
                client, collection_ops, checkpoint, done, total)
            self._refresh_counts()
            self.completed.emit(state, self.updates)
        except _SyncCancelled:
            return
        except Exception as exc:
            # Alle nicht erreichten Operationen bleiben ausdruecklich
            # "not_started" -- der Bericht unterscheidet sie von Fehlern.
            self._refresh_counts()
            message = (CANCEL_MESSAGE if str(exc) == CANCEL_MESSAGE
                       else report_error(exc, "Sync", (
                           account_secrets(self.account_name)
                           + (getattr(client, "access", None),))))
            self.failed.emit(message)


class _SyncCancelled(Exception):
    """Internes Abbruchsignal: kein Fehler, sondern ein regulaerer Halt."""


class LocalMetadataWorker(QObject):
    """Read the Calibre snapshot outside the GUI thread.

    Calibre's ``new_api`` is documented as thread-safe (multiple readers,
    one writer). Only the database object and plain run values cross the
    thread boundary; widgets never do.
    """
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)

    def __init__(self, database, context, state=None):
        QObject.__init__(self)
        self.database = database
        self.context = context
        self.state = state
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def run(self):
        try:
            metadata = local_metadata(self.database, self.context)
        except Exception as exc:
            if self.cancelled:
                self.failed.emit(CANCEL_MESSAGE)
                return
            self.failed.emit(report_error(
                exc, "Bibliothek", self.context.secrets()))
            return
        if self.cancelled:
            self.failed.emit(CANCEL_MESSAGE)
            return
        self.ready.emit({"context": self.context, "metadata": metadata,
                         "state": self.state})


class PrepareWorker(QObject):
    """Netz-Teil der Vorbereitung -- bewusst NICHT im GUI-Thread.

    Der lokale Metadaten-Snapshot kommt aus `LocalMetadataWorker`; hier
    laufen nur Login, Cloud-Inventar, Lese-Status und der Vergleich. Alles
    wird unter dem beim Start erfassten Konto gespeichert.
    """
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, context, metadata, state=None, lock_timeout=5):
        QObject.__init__(self)
        self.context = context
        self.metadata = metadata
        # Ausgangszustand des Kontos; der Worker kopiert ihn vor dem
        # Vergleich, damit nichts Unbestaetigtes in den Einstellungen landet.
        self.state = state
        self.lock_timeout = lock_timeout
        self.account_name = self.context.account_name
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def _stop_requested(self):
        """Abbruch melden und True liefern -- ein Muster für alle Schritte."""
        if not self.cancelled:
            return False
        self.failed.emit(CANCEL_MESSAGE)
        return True

    def _load_comparison_state(self):
        """Load the state and snapshot it before comparison.

        `load_state` may return the preferences mapping unchanged. Only the
        working state is copied; the original snapshot stays untouched.
        """
        original = load_state(self.state)
        working = copy.deepcopy(original)
        adopt_column_tolino_ids(self.metadata, working)
        return working, original

    def _load_optional(self, label, loader):
        """Optionale Cloud-Daten laden; ein Fehler bleibt «unbekannt»."""
        try:
            return loader(), True
        except Exception as exc:
            log_warning("%s konnte nicht geladen werden: %s"
                        % (label, sanitize_error(
                            exc, self.context.secrets())))
            return None, False

    def _load_inventory(self, client):
        """Load inventory, read status and progress without hiding failures."""
        inventory = client.inventory()
        read_ids, read_known = self._load_optional(
            "Lesestatus", client.read_ids)
        progress_by_id, progress_known = None, False
        loader = getattr(client, "progress_by_id", None)
        if read_known and callable(loader):
            progress_by_id, progress_known = self._load_optional(
                "Lesefortschritt", loader)
        return inventory, read_ids, read_known, progress_by_id, progress_known

    def _build_comparison(self, state, inventory, read_ids, progress_by_id):
        """Build comparison rows and drop stale cloud IDs from state."""
        columns = self.context.columns
        comparison = compare_inventory(
            self.metadata, state, inventory, self.context.preferred_formats,
            read_ids=read_ids,
            read_column=bool(columns.get("read")),
            progress_by_id=progress_by_id)
        for row in comparison:
            stale_id = str(row.get("stale_cloud_id") or "").strip()
            book_uuid = str(row.get("uuid") or "").strip()
            if stale_id and book_uuid:
                entry = state.get(book_uuid)
                if isinstance(entry, dict) and str(
                        entry.get("tolino_id") or "").strip() == stale_id:
                    # Cloud-Kopie weg: Zuordnung und alter Lesestand dieses
                    # Kontos dürfen nicht weiter angezeigt werden.
                    entry.pop("tolino_id", None)
                    entry.pop("reading", None)
        return comparison

    def run(self):
        """Vergleich vorbereiten, ohne Keep-alive oder UI zu blockieren."""
        client = None
        try:
            state, original_state = self._load_comparison_state()
            self.progress.emit("Verbinde mit tolino …")
            client = TOKEN_VAULT.login(self.account_name,
                                       self.context.partner_id,
                                       self.lock_timeout)
            if self._stop_requested():
                return
            self.progress.emit("Lade Cloud-Bestand …")
            (inventory, read_ids, read_known,
             progress_by_id, progress_known) = self._load_inventory(client)
            if self._stop_requested():
                return
            self.progress.emit("Vergleiche Bibliothek …")
            comparison = self._build_comparison(
                state, inventory, read_ids, progress_by_id)
            if self._stop_requested():
                return
            store_reading_state(state, comparison)
            if state != original_state:
                # Alle Änderungen des Vergleichs (bestätigte IDs,
                # veraltete Kopien, Lesewerte) in EINEM Schreibvorgang sichern.
                # Nur bestehende Konten aktualisieren: ein während des
                # Vergleichs entferntes Konto darf nicht wieder erscheinen.
                save_account(self.account_name, {"state": state}, create=False)
            self.ready.emit({
                "context": self.context,
                "metadata": self.metadata,
                "state": state,
                "comparison": comparison,
                "read_status_known": read_known,
                "progress_status_known": progress_known,
                "hardware": client.hardware,
            })
        except Exception as exc:
            if self.cancelled:
                self.failed.emit(CANCEL_MESSAGE)
                return
            self.failed.emit(report_error(
                exc, "Vorbereitung",
                self.context.secrets()
                + (getattr(client, "access", None),)))

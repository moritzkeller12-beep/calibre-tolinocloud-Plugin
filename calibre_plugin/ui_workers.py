"""Hintergrund-Worker für Vorbereitung, Synchronisierung und Anmeldung."""
import copy
from collections import Counter
from functools import partial

from qt.core import QObject, pyqtSignal
from .ui_common import (CANCEL_MESSAGE, OP_FAILED, OP_NOT_STARTED, OP_SKIPPED,
                        OP_SUCCEEDED)
from .token_vault import TOKEN_VAULT
from .config import (account_by_name, account_secrets, load_state,
                     save_state)
from .planning import (local_metadata, plan_collection_updates,
                       remember_collection)
from .comparison import (compare_inventory, content_fingerprint,
                         metadata_fingerprint, reading_state,
                         reset_copy_state, store_reading_state)
from .identifiers import display_title, state_entry, valid_book_uuid
from .library import image_suffix
from .tolino import (TolinoApiError, TolinoAuthError, TolinoError, log_warning,
                     report_error, sanitize_error)


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object)
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
        # Änderung; leer heisst: Lesestatus und Spalte bleiben unverändert.
        self.read_changes = [
            (tolino_id, change[1], bool(change[2]),
             valid_book_uuid(change[3]) if len(change) > 3 else "")
            for change in (read_changes or ())
            if isinstance(change, (tuple, list)) and len(change) >= 3
            and (tolino_id := str(change[0] or "").strip())
        ]
        self.titles = {str(key): str(value)
                       for key, value in (titles or {}).items()
                       if value}
        self.metadata_by_uuid = {
            str(item["uuid"]): item
            for item in (metadata or {}).values()
            if isinstance(item, dict) and item.get("uuid")
        }
        self.cancelled = False
        # Der Worker ändert eine private Zustandskopie; `_state_base` ist der
        # Ausgangsstand, gegen den nur Änderungen gespeichert werden.
        snapshot = state if isinstance(state, dict) else {}
        self.state = copy.deepcopy(snapshot)
        self._state_base = copy.deepcopy(snapshot)
        self.updates = []
        # (Calibre-Nummer, gelesen?) je bestätigter Cloud-Änderung;
        # ``None`` leert die Spalte für eine neue Kopie.
        self.read_updates = []
        # (Calibre-Nummer, None) für Kopien ohne alten Lesefortschritt.
        self.progress_updates = []
        self.operations = []
        self.outcome = {
            "uploaded": 0, "deleted": 0,
            "metadata": 0, "read_marked": 0, "read_unmarked": 0,
            "collections": 0, "skipped": 0, "failed": 0,
            "cancelled": False, "operations": self.operations,
        }

    def cancel(self):
        self.cancelled = True

    # Abgeleitet statt gepflegt: `_drop_replaced_read_changes()` kürzt
    # `read_changes`, beide Listen müssen dann nicht nachgezogen werden.
    @property
    def read_marks(self):
        return [change[0] for change in self.read_changes if change[2]]

    @property
    def read_unmarks(self):
        return [change[0] for change in self.read_changes if not change[2]]

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
                                + (client.access if client else None,))
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
        machen; ohne gesicherten Zustand darf der Lauf nicht weitergehen,
        sonst wuerde die ersetzte Kopie geloescht, obwohl die neue ID
        nirgends steht.
        """
        if not self.account_name:
            return
        try:
            stored = save_state(self.account_name, self.state,
                                self._state_base)
            detail = ""
        except Exception as exc:
            stored, detail = False, sanitize_error(
                exc, account_secrets(self.account_name))
        if not stored:
            raise TolinoError(
                "Der Sync-Zwischenstand konnte nicht gespeichert werden. "
                "Bereits durchgeführte Cloud-Änderungen bleiben bestehen; "
                "bitte vor einem erneuten Lauf den Cloud-Bestand prüfen.",
                short=("Der Sync-Zwischenstand konnte nicht gespeichert werden. "
                       "Cloud-Änderungen bleiben bestehen; bitte den Bestand "
                       "vor einem erneuten Lauf prüfen."),
                detail=detail or "Einstellungen konnten nicht geschrieben werden.")
        # Alles, was jetzt im Zustand steht, ist gesichert: der naechste
        # Schreibvorgang uebernimmt nur noch neue Aenderungen und laesst
        # fremde Aenderungen an denselben Feldern stehen.
        self._state_base = copy.deepcopy(self.state)

    def _copy_is_gone(self, client, deliverable_id):
        """Steht die Kopie noch in der Cloud? Unlesbar heisst: Fehler behalten.

        Nach einem mehrdeutigen Löschfehler zaehlt nur ein JETZT gelesener
        Bestand: ein frueher gemerkter Stand koennte die Kopie noch enthalten,
        obwohl sie nicht mehr da ist -- oder umgekehrt. Fehlt sie im lesbaren
        Bestand, war nichts zu loeschen; ist er unlesbar, bleibt der Fehler.
        """
        try:
            cloud_ids = {
                str(record.get("tolino_id")) for record in client.inventory()
                if isinstance(record, dict)}
        except TolinoAuthError:
            # Eine abgelehnte Anmeldung heisst nicht «schon geloescht»: der
            # Lauf muss daran scheitern, statt die Loeschung zu ueberspringen.
            raise
        except TolinoError:
            return False
        return str(deliverable_id) not in cloud_ids

    def _delete_or_skip(self, client, deliverable_id):
        """Return False when the cloud copy is already gone.

        Die Cloud meldet eine unbekannte Kennung je nach Endpunkt mit 404
        oder mit 400 ohne Text. Der Bestand entscheidet: fehlt die Kopie,
        war nichts zu loeschen; steht sie noch da, bleibt es ein Fehler.
        """
        try:
            client.delete(deliverable_id)
        except TolinoApiError as exc:
            text = (str(exc) + " " + str(exc.detail)).casefold()
            if exc.code != "N03":
                raise
            if "http 404" in text or self._copy_is_gone(
                    client, deliverable_id):
                return False
            raise
        return True

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

    def _run_deletions(self, client, delete_ops, checkpoint, done, total):
        """Execute only the deletions explicitly selected by the user."""
        for book_uuid, tolino_id, operation in delete_ops:
            label = self._label(book_uuid, None,
                                self.titles.get(str(tolino_id), ""))
            ok, deleted = self._step(
                client, checkpoint, operation,
                "Lösche aus tolino: %s" % label,
                partial(self._delete_or_skip, client, tolino_id),
                done, total,
                finish=partial(self._finish_delete, operation))
            if not ok:
                done += 1
                continue
            entry = self.state.get(book_uuid)
            recorded = (str(entry.get("tolino_id") or "")
                        if isinstance(entry, dict) else "")
            if not recorded or recorded == str(tolino_id):
                if isinstance(entry, dict) and entry.get("calibre_id") is not None:
                    self.updates.append((entry.get("calibre_id"), ""))
                self.state.pop(book_uuid, None)
                self._persist_state()
            done += 1
            if deleted:
                self.progress.emit(done, total, "Erledigt: %s" % label)
        return done

    def _drop_replaced_read_changes(self):
        """Nur die Kopie markieren, auf die der Lesestatus noch zeigt.

        Ein Ersetzen-Upload vergibt eine neue tolino-ID, eine Löschung
        entfernt den Zustand: der Lesestatus aus dem Vergleich meint dann
        eine Kopie, die es in der Cloud nicht mehr gibt.
        """
        kept = []
        for change in self.read_changes:
            tolino_id, _book_id, _wanted, book_uuid = change
            if book_uuid:
                entry = self.state.get(book_uuid)
                recorded = (str(entry.get("tolino_id") or "").strip()
                            if isinstance(entry, dict) else "")
                if recorded != tolino_id:
                    continue
            kept.append(change)
        self.read_changes = kept

    def _run_read_marks(self, client, read_op, unread_op, checkpoint,
                        done, total):
        """Ausgewählten Lesestatus übertragen."""
        self._drop_replaced_read_changes()
        actions = (
            (read_op, self.read_marks, True,
             "Als gelesen markieren", "Als gelesen markiert"),
            (unread_op, self.read_unmarks, False,
             "Als ungelesen markieren", "Als ungelesen markiert"),
        )
        for operation, book_ids, finished, start_text, done_text in actions:
            if operation is None:
                continue
            operation["count"] = len(book_ids)
            if not book_ids:
                # Die Kopie wurde in diesem Lauf ersetzt oder gelöscht.
                self._finish_operation(operation, OP_SKIPPED,
                                       reason="Kopie ersetzt oder gelöscht")
                done += 1
                continue
            ok, _result = self._step(
                client, checkpoint, operation, start_text,
                partial(client.mark_read_many, book_ids, finished=finished),
                done, total)
            if ok:
                stored = False
                for _tolino, book_id, wanted, book_uuid in self.read_changes:
                    if wanted is not finished:
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
        entry = state_entry(self.state, key, create=True)
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
        try:
            for book_uuid, tolino_id, series, operation in (
                    collection_ops if known is not None else ()):
                if (tolino_id, series) in known:
                    checkpoint()  # Skip-Zweig läuft nicht durch _step().
                    remember_collection(self.state, book_uuid, series)
                    self._finish_operation(operation, OP_SKIPPED,
                                           reason="schon in der Cloud")
                    changed = True
                    done += 1
                    continue

                ok, new_revision = self._step(
                    client, checkpoint, operation,
                    "Serie in der Cloud: %s" % series,
                    partial(client.add_to_collection, tolino_id, series,
                            revision=revision),
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
        finally:
            # Auch ein Abbruch oder Auth-Fehler behaelt die bestaetigten
            # Zuordnungen; sonst stehen sie nur im Speicher.
            if changed:
                self._persist_state()
        self.progress.emit(done, total, "Serien geprüft")
        return done

    def _run_metadata_updates(self, client, operations, checkpoint, done, total):
        """Apply selected metadata patches and persist their confirmed state."""
        changed = False
        try:
            for record, operation in operations:
                ok, _result = self._step(
                    client, checkpoint, operation,
                    "Metadaten: %s" % self._label(
                        record.book_uuid, record.book_id,
                        (record.fields or {}).get("title")),
                    partial(client.update_metadata, record.tolino_id,
                            record.fields),
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
        finally:
            # Einmal je Phase statt je Buch -- und auch nach Abbruch oder
            # Auth-Fehler, damit der Patch nicht nur im Speicher steht.
            if changed:
                self._persist_state()
        return done

    def _run_uploads(self, client, operations, checkpoint, done, total):
        """Upload selected books and preserve each successful partial result."""
        for job, operation in operations:
            ok, new_id = self._step(
                client, checkpoint, operation,
                "Lade hoch: %s (%s)" % (job.label(), job.format_name),
                partial(client.upload, job.path), done, total)
            if not ok:
                done += 1
                self.progress.emit(done, total, "Fehlgeschlagen: %s"
                                   % job.label())
                continue
            # Die neue Cloud-Kopie startet ungelesen und ohne Sammlungen;
            # alles Kopiegebundene der ersetzten Kopie faellt weg.
            entry = reset_copy_state(
                dict(state_entry(self.state, job.book_uuid)))
            entry.update({"tolino_id": new_id, "calibre_id": job.book_id})
            self.state[job.book_uuid] = entry
            metadata = self.metadata_by_uuid.get(job.book_uuid)
            if metadata:
                entry["content_fp"] = content_fingerprint(
                    metadata, job.format_name)
            self.updates.append((job.book_id, new_id))
            # Die neue Kopie hat weder den Lesestatus noch den Fortschritt
            # der ersetzten Kopie; beide Calibre-Spalten werden geleert.
            self.read_updates.append((job.book_id, None))
            self.progress_updates.append((job.book_id, None))
            # Der Upload ist nicht rueckgaengig zu machen: die neue ID muss
            # sofort im Konto stehen, nicht erst am Ende der Phase.
            self._persist_state()
            replaced = bool(job.old_id) and str(job.old_id) != str(new_id)
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
            if replaced:
                self._remove_replaced_copy(client, job)
            done += 1
            self.progress.emit(done, total, "Hochgeladen: %s" % job.label())
        return done

    def run(self):
        client = None
        try:
            client = TOKEN_VAULT.login(self.account_name)
            (meta_ops, upload_ops, delete_ops, read_op, unread_op,
             total) = self._plan_operations()
            done = 0

            def checkpoint():
                # Abbruch nur intern signalisieren: genau EIN terminales
                # Signal sendet `run()`, nachdem der Zustand gesichert ist.
                if self.cancelled:
                    raise _SyncCancelled()

            done = self._run_metadata_updates(
                client, meta_ops, checkpoint, done, total)
            done = self._run_uploads(
                client, upload_ops, checkpoint, done, total)
            done = self._run_deletions(
                client, delete_ops, checkpoint, done, total)
            done = self._run_read_marks(
                client, read_op, unread_op, checkpoint, done, total)
            collection_ops = self._plan_collection_operations()
            total += len(collection_ops)
            done = self._run_collections(
                client, collection_ops, checkpoint, done, total)
            self._refresh_counts()
            self.completed.emit(self.updates)
        except _SyncCancelled:
            # Nicht erreichte Operationen bleiben «nicht begonnen».
            self._refresh_counts()
            self.completed.emit(self.updates)
        except Exception as exc:
            # Alle nicht erreichten Operationen bleiben ausdruecklich
            # "not_started" -- der Bericht unterscheidet sie von Fehlern.
            self._refresh_counts()
            message = (CANCEL_MESSAGE if str(exc) == CANCEL_MESSAGE
                       else report_error(exc, "Sync", (
                           account_secrets(self.account_name)
                           + (client.access if client else None,))))
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
            self.failed.emit(
                CANCEL_MESSAGE if self.cancelled else report_error(
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
        return working, original

    def _load_optional(self, label, loader):
        """Optionale Cloud-Daten laden; ein Fehler bleibt «unbekannt».

        Eine abgelehnte Anmeldung ist kein fehlender Lesewert: der Token
        kann dabei geloescht worden sein, der Nutzer muss sich neu anmelden.
        Sie wird deshalb weitergeworfen und der Vergleich scheitert -- sonst
        meldete die Vorbereitung «bereit» mit einem ungueltigen Zugang.
        """
        try:
            return loader(), True
        except TolinoAuthError:
            raise
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
        if read_known:
            progress_by_id, progress_known = self._load_optional(
                "Lesefortschritt", client.progress_by_id)
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
                    # Cloud-Kopie weg: Zuordnung und alle kopiegebundenen
                    # Werte dieses Kontos duerfen nicht weiter gelten -- sonst
                    # erbte eine spaeter per ISBN gefundene Kopie sie.
                    entry.pop("tolino_id", None)
                    reset_copy_state(entry)
        return comparison

    def run(self):
        """Vergleich vorbereiten, ohne Keep-alive oder UI zu blockieren."""
        client = None
        try:
            state, original_state = self._load_comparison_state()
            self.progress.emit("Verbinde mit tolino …")
            client = TOKEN_VAULT.login(self.account_name,
                                       lock_timeout=self.lock_timeout)
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
                # Alle Vergleichsänderungen (IDs, alte Kopien, Lesewerte) in
                # EINEM Schreibvorgang sichern. Ein entferntes Konto bleibt
                # Sonderfall; ein Schreibfehler gilt nicht als «bereit».
                try:
                    stored = save_state(self.account_name, state,
                                        original_state)
                except Exception:
                    stored = False
                if not stored and account_by_name(self.account_name):
                    raise TolinoError(
                        "Der Zustand des Vergleichs konnte nicht gespeichert "
                        "werden.",
                        short="Der Vergleich konnte nicht gespeichert werden.",
                        detail="Einstellungen konnten nicht geschrieben "
                               "werden.")
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
                + (client.access if client else None,)))

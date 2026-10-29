"""Hintergrund-Worker für Vorbereitung, Synchronisierung und Anmeldung."""
import copy
from .ui_common import (CANCEL_MESSAGE, OP_FAILED, OP_NOT_STARTED, OP_SKIPPED,
                        OP_SUCCEEDED, QObject, plural, pyqtSignal)
from .token_vault import make_client
from .config import CredentialStore, save_account
from .plan import plan_collection_updates, remember_collection
from .comparison import (compare_inventory, content_fingerprint,
                         metadata_fingerprint)
from .identifiers import display_title, migrate_state_keys
from .config import load_state
from .tolino import (TolinoApiError, TolinoAuthError, log_warning,
                     report_error, sanitize_error)


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object, object)
    failed = pyqtSignal(str)

    def __init__(self, settings, jobs, removals, meta_updates=None,
                 series_map=None, read_marks=None, read_unmarks=None,
                 titles=None):
        QObject.__init__(self)
        # Unveraenderlicher Kontokontext: Jede Persistenz (Tokenrotation,
        # Zustand, tolino-ID) gehoert zu dem Konto dieses Laufs.
        self.settings = dict(settings)
        self.account_name = str(self.settings.get("account_name") or "")
        # Der Store aktualisiert denselben Snapshot, den der Lauf verwendet,
        # und speichert unter dem beim Start erfassten Kontonamen.
        self.store = CredentialStore(self.settings, self.account_name)
        self.jobs = list(jobs or ())
        self.removals = list(removals or ())
        self.meta_updates = list(meta_updates or ())
        self.series_map = dict(series_map or {})
        # tolino-IDs, die in der Cloud als gelesen markiert werden sollen.
        # Leer heisst: Lesestatus bleibt unangetastet.
        self.read_marks = [str(item) for item in (read_marks or ())
                           if str(item or "").strip()]
        # tolino-IDs, deren Lesestatus auf ungelesen gesetzt wird. Ebenfalls
        # leer = Lesestatus bleibt unangetastet.
        self.read_unmarks = [str(item) for item in (read_unmarks or ())
                             if str(item or "").strip()]
        # Titel für sichtbare Texte: Schlüssel sind Kennung und tolino-ID.
        # Ohne Titel steht «Buch <Nummer>» -- nie eine UUID.
        self.titles = {str(key): str(value)
                       for key, value in (titles or {}).items()
                       if value}
        self.metadata_by_uuid = {
            str(item["uuid"]): item
            for item in (self.settings.get("metadata") or {}).values()
            if isinstance(item, dict) and item.get("uuid")
        }
        self.cancelled = False
        self.state = load_state(self.settings.get("state"))
        self.updates = []
        self.operations = []
        self.outcome = {
            "uploaded": 0, "updated": 0, "deleted": 0,
            "metadata": 0, "read_marked": 0, "read_unmarked": 0,
            "collections": 0, "skipped": 0, "failed": 0,
            "cancelled": False, "not_started": 0,
            "operations": self.operations,
        }

    def cancel(self):
        self.cancelled = True

    def _label(self, book_uuid, book_id=None, title=""):
        """Sichtbarer Name eines Buchs: Titel, sonst «Buch <Nummer>»."""
        text = str(title or "").strip()
        if not text:
            text = self.titles.get(str(book_uuid), "")
        entry = None
        if isinstance(self.state, dict):
            entry = self.state.get(str(book_uuid))
        if not text and isinstance(entry, dict):
            text = str(entry.get("title") or "")
        if book_id is None and isinstance(entry, dict):
            book_id = entry.get("calibre_id")
        return display_title(text, book_id)

    # --- Ergebnis-/Persistenz-Helfer ------------------------------------

    def _add_operation(self, kind, label, status=OP_NOT_STARTED, **extra):
        operation = dict(extra, kind=kind, label=label, status=status)
        self.operations.append(operation)
        return operation

    @staticmethod
    def _finish_operation(operation, status, **extra):
        if operation is not None:
            operation.update(extra)
            operation["status"] = status

    def _refresh_counts(self):
        counts = {OP_SUCCEEDED: 0, OP_SKIPPED: 0,
                  OP_FAILED: 0, OP_NOT_STARTED: 0}
        kinds = {"upload": 0, "metadata": 0, "read": 0, "unread": 0,
                 "collection": 0, "delete": 0}
        for operation in self.operations:
            status = operation["status"]
            kind = operation["kind"]
            counts[status] = counts.get(status, 0) + 1
            if status == OP_SUCCEEDED and kind in kinds:
                kinds[kind] += int(operation.get("count") or 1)
        self.outcome.update({
            "uploaded": kinds["upload"],
            "updated": kinds["metadata"] + kinds["read"] + kinds["unread"]
                       + kinds["collection"],
            "deleted": kinds["delete"],
            "metadata": kinds["metadata"],
            "read_marked": kinds["read"],
            "read_unmarked": kinds["unread"],
            "collections": kinds["collection"],
            "skipped": counts.get(OP_SKIPPED, 0),
            "failed": counts.get(OP_FAILED, 0),
            "not_started": counts.get(OP_NOT_STARTED, 0),
            "cancelled": bool(self.cancelled),
        })

    def _record_failure(self, operation, exc, client=None):
        """Einen einzelnen Schritt als gescheitert zaehlen.

        Der Lauf macht weiter: ein fehlendes Cover oder eine einzelne Datei
        darf die uebrigen Buecher nicht verhindern. Der Grund steht am
        Schritt (fuer den Abschlussbericht) und im Log -- ohne Zugangsdaten.
        """
        detail = sanitize_error(exc, (self.store.refresh_token,
                                      getattr(client, "access", None)))
        if operation is not None:
            operation["exception"] = exc
        self._finish_operation(operation, OP_FAILED, error=detail)
        log_warning("Schritt gescheitert (%s): %s"
                    % ((operation or {}).get("label") or "?", detail))
        self._refresh_counts()

    def _count_failure(self, operation, exc, client=None):
        """Schritt als gescheitert zählen -- fatal beendet den Lauf.

        Nur eine beschädigte Anmeldung/Sitzung ist fatal: danach liefert
        jeder weitere Aufruf denselben Fehler. Alles andere wird pro Schritt
        gezählt, der Lauf macht weiter.
        """
        self._record_failure(operation, exc, client)
        if self._fatal_error(exc):
            raise exc

    @staticmethod
    def _fatal_error(exc):
        """Fehler, nach denen jeder weitere Aufruf scheitern wuerde.

        Nur die Anmeldung/Sitzung ist fatal: danach liefert jede weitere
        Anfrage denselben Fehler und rotiert nur den Token weiter. Alles
        andere (Netz, ein Buch, ein Cover) wird pro Schritt gezaehlt.
        """
        return isinstance(exc, TolinoAuthError)

    def _persist_state(self):
        """Save verified partial state under the CAPTURED account only.

        Called after every irreversible remote action. If a later cover /
        delete / metadata step fails, the tolino-ID of the finished upload is
        already on disk and the next run does not upload a duplicate.
        """
        if not self.account_name:
            return
        try:
            save_account(self.account_name, {"state": self.state},
                         active=self.account_name)
        except Exception as exc:
            log_warning("Teilstand konnte nicht gespeichert werden: %s"
                        % sanitize_error(exc))

    def run(self):
        self._run()

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
        total = (len(meta_ops) + len(upload_ops) + len(delete_ops)
                 + (1 if read_op else 0)
                 + (1 if unread_op else 0))
        self._refresh_counts()
        self.outcome["total"] = total
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
            self._count_failure(operation, exc, client)
        else:
            self._finish_operation(
                operation, OP_SUCCEEDED if deleted else OP_SKIPPED,
                **({} if deleted else {"reason": "nicht mehr in der Cloud"}))
            if deleted:
                self._persist_state()

    def _run_deletions(self, client, delete_ops, checkpoint, state, done, total):
        """Execute only the deletions explicitly selected by the user."""
        for book_uuid, tolino_id, operation in delete_ops:
            checkpoint()
            self.progress.emit(
                done, total, "Lösche aus tolino: %s"
                % self._label(book_uuid, None,
                              self.titles.get(str(tolino_id), "")))
            try:
                deleted = self._delete_or_skip(client, tolino_id)
            except Exception as exc:
                self._count_failure(operation, exc, client)
                done += 1
                continue
            self._finish_operation(
                operation, OP_SUCCEEDED if deleted else OP_SKIPPED,
                **({} if deleted else {"reason": "nicht mehr in der Cloud"}))
            entry = state.get(book_uuid)
            recorded = (str(entry.get("tolino_id") or "")
                        if isinstance(entry, dict) else "")
            if not recorded or recorded == str(tolino_id):
                if isinstance(entry, dict) and entry.get("calibre_id") is not None:
                    self.updates.append((entry.get("calibre_id"), ""))
                state.pop(book_uuid, None)
            self._persist_state()
            done += 1
            self._refresh_counts()
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
            if not book_ids:
                # Nur für Uploads geplant, die nicht geklappt haben.
                self._finish_operation(operation, OP_SKIPPED,
                                       reason="kein Buch hochgeladen")
                done += 1
                continue
            operation["count"] = len(book_ids)
            checkpoint()
            self.progress.emit(done, total, start_text)
            try:
                client.mark_read_many(book_ids, finished=finished)
            except Exception as exc:
                self._count_failure(operation, exc, client)
            else:
                self._finish_operation(operation, OP_SUCCEEDED)
                self._refresh_counts()
                self.progress.emit(done, total, done_text)
            done += 1
        return done

    def _run_collections(self, client, collection_ops, checkpoint, done,
                         total):
        """Serien → Cloud-Sammlungen: nur die offenen Zuordnungen."""
        if not collection_ops:
            return done
        known = None
        try:
            known = client.collection_pairs()
        except Exception as exc:
            # Ohne Bestand liesse sich nicht erkennen, was schon zugeordnet
            # ist -- jede Zuordnung gilt als gescheitert.
            for _book, _tolino, _series, operation in collection_ops:
                self._record_failure(operation, exc, client)
            if self._fatal_error(exc):
                raise
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
                self._refresh_counts()
                continue

            self.progress.emit(done, total,
                               "Serie in der Cloud: %s" % series)
            try:
                revision = client.add_to_collection(
                    tolino_id, series, revision=revision)
            except Exception as exc:
                self._count_failure(operation, exc, client)
                # Nach einem Fehler ist die Revision nicht mehr verlaesslich.
                revision = None
                done += 1
                continue

            known.add((tolino_id, series))
            remember_collection(self.state, book_uuid, series)
            self._finish_operation(operation, OP_SUCCEEDED)
            changed = True
            done += 1
            self._refresh_counts()
        if changed:
            self._persist_state()
        self.progress.emit(done, total, "Serien geprüft")
        return done

    def _run_metadata_updates(self, client, operations, checkpoint, done, total):
        """Apply selected metadata patches and persist their confirmed state."""
        changed = False
        for record, operation in operations:
            checkpoint()
            self.progress.emit(
                done, total,
                "Metadaten: %s" % self._label(
                    record.book_uuid, record.book_id,
                    (record.fields or {}).get("title")))
            try:
                client.update_metadata(record.tolino_id, record.fields)
            except Exception as exc:
                self._count_failure(operation, exc, client)
                done += 1
                continue
            self._finish_operation(operation, OP_SUCCEEDED)
            self._refresh_counts()
            entry = self.state.get(record.book_uuid)
            metadata = self.metadata_by_uuid.get(record.book_uuid)
            if isinstance(entry, dict) and metadata:
                entry["meta_verified_fp"] = metadata_fingerprint(metadata)
            # Keep the confirmed Tolino-ID mapping current.
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
            checkpoint()
            self.progress.emit(done, total, "Lade hoch: %s (%s)" %
                               (job.label(), job.format_name))
            try:
                new_id = client.upload(job.path)
            except Exception as exc:
                self._count_failure(operation, exc, client)
                done += 1
                self.progress.emit(done, total, "Fehlgeschlagen: %s"
                                   % job.label())
                continue
            entry = dict(self.state.get(job.book_uuid) or {})
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
            self._finish_operation(operation, OP_SUCCEEDED)
            # Persist immediately so a later failure cannot lose this mapping.
            self._persist_state()
            if job.cover_path:
                try:
                    client.upload_cover(new_id, job.cover_path)
                except Exception as exc:
                    cover_op = self._add_operation(
                        "cover", "Buchcover: %s" % job.label(),
                        book_uuid=job.book_uuid)
                    self._count_failure(cover_op, exc, client)
            self._remove_replaced_copy(client, job)
            done += 1
            self._refresh_counts()
            self.progress.emit(done, total, "Hochgeladen: %s" % job.label())
        return done

    def _run(self):
        state = self.state
        client = None
        try:
            client = make_client(self.store)
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

            # Erst Metadaten, dann Uploads; jede Phase hält ihre Persistenzlogik.
            done = self._run_metadata_updates(
                client, meta_ops, checkpoint, done, total)
            done = self._run_uploads(
                client, upload_ops, checkpoint, done, total)
            done = self._run_deletions(
                client, delete_ops, checkpoint, state, done, total)
            # Lesestatus nur für ausdrücklich im Vergleichsfenster
            # ausgewählte Bücher übertragen.
            done = self._run_read_marks(
                client, read_op, unread_op, checkpoint, done, total)
            series_items = plan_collection_updates(self.series_map, state)
            collection_ops = []
            for book_uuid, tolino_id, series in series_items:
                collection_ops.append((
                    book_uuid, tolino_id, series,
                    self._add_operation(
                        "collection", "Serie in tolino: %s" % series,
                        book_uuid=book_uuid, series=series)))
            total += len(collection_ops)
            self.outcome["total"] = total
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
                           self.store.refresh_token,
                           getattr(client, "access", None))))
            self.failed.emit(message)


class _SyncCancelled(Exception):
    """Internes Abbruchsignal: kein Fehler, sondern ein regulaerer Halt."""


class PrepareWorker(QObject):
    """Netz-Teil der Vorbereitung -- bewusst NICHT im GUI-Thread.

    Der lokale Metadaten-Snapshot entsteht im GUI-Thread (Calibre-DB), hier
    laufen nur Login, Cloud-Inventar, Lese-Status und der Vergleich. Alles
    wird unter dem beim Start erfassten Konto gespeichert.
    """
    ready = pyqtSignal(object)
    failed = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, settings, metadata, lock_timeout=5):
        QObject.__init__(self)
        self.settings = dict(settings)
        self.metadata = metadata
        # Wartezeit auf eine laufende Keep-alive-Rotation, bevor der Lauf
        # mit «Anmeldung wird gerade erneuert» abbricht.
        self.lock_timeout = lock_timeout
        self.account_name = str(self.settings.get("account_name") or "")
        # Der Store aktualisiert denselben Snapshot, den Vergleich und Sync
        # weiterverwenden.
        self.store = CredentialStore(self.settings, self.account_name)
        # Der ausgetauschte Token verschwindet nach der Rotation aus
        # settings; fuer die Redaktion alter Fehlertexte bleibt er
        # trotzdem ein Geheimnis.
        self.initial_refresh_token = str(
            self.settings.get("refresh_token") or "")
        self.cancelled = False

    def cancel(self):
        self.cancelled = True

    def _stop_requested(self):
        """Abbruch melden und True liefern -- ein Muster für alle Schritte."""
        if not self.cancelled:
            return False
        self.failed.emit(CANCEL_MESSAGE)
        return True

    def run(self):
        """Vergleich vorbereiten, ohne Keep-alive oder UI zu blockieren."""
        self._run()

    def _run(self):
        client = None
        try:
            # Kopie: `load_state` gibt ein Dict unveraendert zurueck, und der
            # Vergleich schreibt tolino-IDs hinein. Ohne die Kopie landeten
            # diese Schreibvorgaenge im Speicher von PREFERENCES, bevor der
            # Lauf bestaetigt oder gespeichert wurde.
            state = copy.deepcopy(load_state(self.settings.get("state")))
            # Bestehende Cloud-Zuordnungen unter Fallback-Kennungen behalten,
            # wenn Calibre spaeter eine echte UUID liefert.
            migrated = migrate_state_keys(self.metadata, state)
            if migrated:
                log_warning("Zustand auf die echte Kennung umgestellt: %d %s."
                            % (migrated, plural(migrated, "Buch", "Bücher")))
            self.progress.emit("Verbinde mit tolino …")
            client = make_client(self.store, self.lock_timeout)
            if self._stop_requested():
                return
            self.progress.emit("Lade Cloud-Bestand …")
            inventory = client.inventory()

            # Eine unbekannte Hülle darf nie als leere Bibliothek gelten.
            if not getattr(client, "inventory_ok", True):
                raise TolinoApiError(
                    "Die Cloud-Antwort enthält kein PublicationInventory.",
                    code="N02")
            read_known = True
            try:
                read_ids = client.read_ids()
            except Exception as exc:
                # «Unbekannt» ist ein eigener Zustand: niemals als
                # «nichts gelesen» ausgeben.
                read_ids = None
                read_known = False
                secrets = (self.initial_refresh_token,
                           str(self.settings.get("refresh_token") or ""))
                log_warning("Lesestatus konnte nicht geladen werden: "
                            + sanitize_error(exc, secrets))
            if self._stop_requested():
                return
            self.progress.emit("Vergleiche Bibliothek …")
            comparison = compare_inventory(
                self.metadata, state, inventory,
                self.settings.get("preferred_formats", ()),
                use_metadata_ids=self.settings.get("sync_column_enabled", True),
                read_ids=read_ids,
                read_column=bool(self.settings.get("read_column_enabled")))
            for row in comparison:
                stale_id = str(row.get("stale_cloud_id") or "").strip()
                book_uuid = str(row.get("uuid") or "").strip()
                if stale_id and book_uuid:
                    entry = state.get(book_uuid)
                    if isinstance(entry, dict) and str(
                            entry.get("tolino_id") or "").strip() == stale_id:
                        entry.pop("tolino_id", None)
            if self._stop_requested():
                return
            self.ready.emit({
                "settings": self.settings,
                "metadata": self.metadata,
                "state": state,
                "comparison": comparison,
                "read_status_known": read_known,
                "hardware": client.hardware,
                "account_name": self.account_name,
            })
        except Exception as exc:
            if self.cancelled:
                self.failed.emit(CANCEL_MESSAGE)
                return
            self.failed.emit(report_error(
                exc, "Vorbereitung",
                (self.initial_refresh_token,
                 self.settings.get("refresh_token"),
                 getattr(client, "access", None))))

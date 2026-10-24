"""Hintergrund-Worker für Vorbereitung, Synchronisierung und Anmeldung."""
import copy
from .ui_common import (CANCEL_MESSAGE, OP_FAILED, OP_NOT_STARTED, OP_SKIPPED,
                        OP_SUCCEEDED, QObject, plural, pyqtSignal)
from .client import make_client
from .config import CredentialStore, save_account
from .plan import (confirm_fingerprints, plan_collection_updates,
                  remember_collection, unpack_meta_update_record)
from .comparison import compare_inventory
from .identifiers import display_title, migrate_state_keys
from .config import load_state
from .sessions import token_session
from .tolino import (TolinoApiError, TolinoAuthError, log_warning,
                     report_error, sanitize_error)


class SyncWorker(QObject):
    progress = pyqtSignal(int, int, str)
    completed = pyqtSignal(object, object, object)
    failed = pyqtSignal(str, str)

    def __init__(self, settings, jobs, removals, meta_updates=None,
                 series_map=None, read_marks=None, read_unmarks=None,
                 titles=None, fingerprints=None):
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
        # Vom Plan berechnete, aber noch NICHT bestaetigte Fingerabdruecke.
        # Sie landen erst im Zustand, wenn die zugehoerige Aktion gelungen
        # ist.
        self.fingerprints = {
            str(key): dict(value)
            for key, value in (fingerprints or {}).items()
            if isinstance(value, dict)
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
        for operation in self.operations:
            counts[operation["status"]] = counts.get(
                operation["status"], 0) + 1
            if operation["status"] == OP_FAILED:
                error = operation.get("exception")
                if error is not None:
                    code = str(getattr(error, "code", "") or "")
                    short = str(getattr(error, "short", "") or error)
                    operation["error"] = short + (
                        " (Code %s)" % code if code else "")
        self.outcome.update({
            "uploaded": sum(1 for item in self.operations
                            if item["kind"] == "upload"
                            and item["status"] == OP_SUCCEEDED),
            "updated": sum(1 for item in self.operations
                           if item["kind"] in ("metadata", "read", "unread",
                                               "collection")
                           and item["status"] == OP_SUCCEEDED),
            "deleted": sum(1 for item in self.operations
                           if item["kind"] == "delete"
                           and item["status"] == OP_SUCCEEDED),
            "metadata": sum(1 for item in self.operations
                            if item["kind"] == "metadata"
                            and item["status"] == OP_SUCCEEDED),
            "read_marked": sum(int(item.get("count") or 0)
                                for item in self.operations
                                if item["kind"] == "read"
                                and item["status"] == OP_SUCCEEDED),
            "read_unmarked": sum(int(item.get("count") or 0)
                                  for item in self.operations
                                  if item["kind"] == "unread"
                                  and item["status"] == OP_SUCCEEDED),
            "collections": sum(1 for item in self.operations
                                if item["kind"] == "collection"
                                and item["status"] == OP_SUCCEEDED),
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

    @staticmethod
    def _fatal_error(exc):
        """Fehler, nach denen jeder weitere Aufruf scheitern wuerde.

        Nur die Anmeldung/Sitzung ist fatal: danach liefert jede weitere
        Anfrage denselben Fehler und rotiert nur den Token weiter. Alles
        andere (Netz, ein Buch, ein Cover) wird pro Schritt gezaehlt.
        """
        return isinstance(exc, TolinoAuthError)

    def _confirm_fingerprints(self, book_uuid, keys=None):
        """Geplante Fingerabdruecke NACH der erfolgreichen Aktion setzen.

        Der Plan kennt den Zielstand, aber erst die Antwort der Cloud macht
        ihn wahr. Ohne diesen Schritt gaelte ein Buch nach einem
        gescheiterten Upload oder /meta-Update beim naechsten Vergleich als
        aktuell -- der Fehler waere unsichtbar und nicht wiederholbar
.
        """
        values = self.fingerprints.get(str(book_uuid))
        entry = self.state.get(book_uuid)
        if not values or not isinstance(entry, dict):
            return
        if keys:
            confirm_fingerprints(entry, values, keys)
        else:
            confirm_fingerprints(entry, values)

    def _unverified_deletion_error(self):
        """Kopie nicht gefunden, Bestand aber unvollstaendig.

        Ohne vollständige Cloud-Kennungen lässt sich «gelöscht» von
        «nicht lesbar» nicht unterscheiden. Die Zuordnung bleibt stehen.
        """
        return TolinoApiError(
            "Die Kopie wurde im Bestand nicht gefunden; einzelne Datensätze "
            "sind nicht vollständig lesbar. Die Zuordnung bleibt erhalten.",
            code="N02",
            short="Cloud-Bestand ohne vollständige Kennungen – diese Kopie "
            "wurde nicht gelöscht.")

    def _deletions_blocked_error(self):
        """Grund, aus dem in diesem Lauf nicht geloescht wird."""
        return TolinoApiError(
            "Der Cloud-Bestand liess sich nicht vollständig lesen. Es wurde "
            "nichts gelöscht oder geändert. %s"
            % (self.deletions_reason or "unbekannte Antwort"),
            code="N02",
            short="Cloud-Bestand nicht lesbar – diese Kopie wurde nicht "
            "gelöscht.")

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
        # Ein laufender Sync sperrt den Keep-alive aus: sonst loesen beide
        # denselben Refresh-Token ein (Keycloak-Reuse-Schutz) und die
        # Sitzung stirbt. Der Zaehler wird in JEDEM Fall wieder
        # freigegeben -- auch bei Abbruch oder Fehler, sonst bliebe der
        # Keep-alive fuer den Rest der Sitzung gesperrt.
        with token_session():
            self._run()

    def _load_remote_ids(self, client):
        """Cloud-Kennungen nur laden, wenn ein Schritt sie wirklich braucht."""
        needs_replacement_cleanup = any(job.old_id for job in self.jobs)
        # Ausdrücklich markierte Löschungen brauchen den Cloud-Bestand;
        # sonst würden sie fälschlich als «nicht mehr in der Cloud» gelten.
        needs_inventory = bool(needs_replacement_cleanup or self.removals)
        remote_ids = set()
        self.deletions_reason = ""
        inventory_incomplete = False
        if not needs_inventory:
            return remote_ids, inventory_incomplete
        try:
            remote_ids = client.inventory_ids()
            # Nicht eindeutig lesbare Kopien beweisen nicht, dass eine
            # gespeicherte ID gelöscht wurde; sie bleibt dann erhalten.
            inventory_incomplete = not getattr(
                client, "inventory_ids_complete", True)
            if inventory_incomplete:
                log_warning(
                    "Cloud-Bestand enthält %d %s ohne Cloud-ID – nicht "
                    "gefundene Zuordnungen bleiben erhalten."
                    % (int(getattr(client, "inventory_unidentified", 0) or 0),
                       plural(int(getattr(client, "inventory_unidentified", 0) or 0),
                              "Kopie", "Kopien")))
        except Exception as exc:
            # Ein nur teilweise lesbarer Bestand ist KEIN Beweis, dass eine
            # Kopie fehlt. Dann wird in diesem Lauf nichts geloescht und
            # kein Zustand aufgeraeumt; die betroffenen Schritte stehen als
            # Fehler im Bericht.
            self.deletions_reason = sanitize_error(exc, (
                self.store.refresh_token,
                getattr(client, "access", None)))
            log_warning("Cloud-Bestand nicht vollstaendig lesbar – "
                        "es wird nichts geloescht: %s"
                        % self.deletions_reason)
        return remote_ids, inventory_incomplete

    def _plan_operations(self, series_items):
        """Alle geplanten Schritte von Anfang an fuehren (auch «nicht begonnen»)."""
        meta_ops = []
        for update in self.meta_updates:
            record = unpack_meta_update_record(update)
            meta_ops.append((record, self._add_operation(
                "metadata", "Metadaten: %s"
                % self._label(record["book_uuid"], record["book_id"],
                              (record.get("fields") or {}).get("title")),
                book_uuid=record["book_uuid"])))
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
        collection_ops = []
        for book_uuid, tolino_id, series in series_items:
            collection_ops.append((book_uuid, tolino_id, series,
                                   self._add_operation(
                                       "collection",
                                       "Serie in der Cloud: %s" % series,
                                       book_uuid=book_uuid,
                                       series=series)))
        total = (len(meta_ops) + len(upload_ops) + len(delete_ops)
                 + len(collection_ops) + (1 if read_op else 0)
                 + (1 if unread_op else 0))
        self._refresh_counts()
        self.outcome["total"] = total
        return (meta_ops, upload_ops, delete_ops, read_op, unread_op,
                collection_ops, total)

    def _remove_replaced_copy(self, client, job, remote_ids,
                              inventory_incomplete):
        """Alte Cloud-Kopie nach einem Ersetzungs-Upload aufraeumen."""
        if not job.old_id:
            return
        if self.deletions_reason:
            # Sie ersetzen war moeglich, das Aufraeumen nicht: sichtbar als
            # eigener, gescheiterter Schritt. Fehlt die alte Kopie wirklich
            # im Bestand, ist das kein Fehler und wird nicht gemeldet.
            replacement_op = self._add_operation(
                "delete", "Alte Kopie löschen: %s" % job.label(),
                book_uuid=job.book_uuid)
            self._record_failure(
                replacement_op, self._deletions_blocked_error(), client)
        elif str(job.old_id) in remote_ids:
            try:
                client.delete(job.old_id)
            except Exception as exc:
                replacement_op = self._add_operation(
                    "delete", "Alte Kopie löschen: %s" % job.label(),
                    book_uuid=job.book_uuid)
                self._record_failure(replacement_op, exc, client)
                if self._fatal_error(exc):
                    raise
        elif inventory_incomplete:
            # Nicht gefunden, aber der Bestand ist unvollstaendig: die alte
            # Kopie koennte die kennungslose sein. Nicht loeschen und nicht
            # behaupten, sie sei weg.
            replacement_op = self._add_operation(
                "delete", "Alte Kopie löschen: %s" % job.label(),
                book_uuid=job.book_uuid)
            self._record_failure(
                replacement_op, self._unverified_deletion_error(), client)

    def _run_deletions(self, client, delete_ops, remote_ids,
                       inventory_incomplete, checkpoint, state, done, total):
        """Ausdrueckliche Loeschungen -- mit Loeschschutz."""
        for book_uuid, tolino_id, operation in delete_ops:
            checkpoint()
            if self.deletions_reason:
                # Nichts loeschen und den Zustand NICHT entfernen: die
                # Kopie kann weiterhin existieren. Sichtbar als Fehler
                # im Bericht, damit niemand ein stilles Nichts sieht.
                self._record_failure(
                    operation, self._deletions_blocked_error(), client)
                done += 1
                continue
            if str(tolino_id) in remote_ids:
                self.progress.emit(
                    done, total, "Lösche aus tolino: %s"
                    % self._label(book_uuid, None,
                                  self.titles.get(str(tolino_id), "")))
                try:
                    client.delete(tolino_id)
                except Exception as exc:
                    self._record_failure(operation, exc, client)
                    if self._fatal_error(exc):
                        raise
                    # Zustand bleibt: der naechste Lauf versucht es erneut.
                    done += 1
                    continue
                self._finish_operation(operation, OP_SUCCEEDED)
            else:
                if inventory_incomplete:
                    # Ohne vollstaendige Kennungen ist «nicht gefunden»
                    # kein Beweis. Zustand und Calibre-Spalte bleiben --
                    # sonst zeigte die Spalte ins Leere, waehrend die
                    # Kopie weiter in der Cloud steht.
                    self._record_failure(
                        operation, self._unverified_deletion_error(),
                        client)
                    done += 1
                    self._refresh_counts()
                    continue
                self._finish_operation(operation, OP_SKIPPED,
                                       reason="nicht mehr in der Cloud")
            entry = state.get(book_uuid)
            recorded = (str(entry.get("tolino_id") or "")
                        if isinstance(entry, dict) else "")
            # Nur die Zuordnung loeschen, die wirklich zu dieser
            # Cloud-Kopie gehoert. Bei einer ausdruecklich markierten
            # Kopie kann der Zustand auf eine andere zeigen.
            if not recorded or recorded == str(tolino_id):
                if isinstance(entry, dict) and entry.get("calibre_id") is not None:
                    # Die tolino-Spalte in Calibre darf nicht auf eine
                    # geloeschte Kopie zeigen; leerer Wert loescht sie.
                    self.updates.append((entry.get("calibre_id"), ""))
                state.pop(book_uuid, None)
            self._persist_state()
            done += 1
            self._refresh_counts()
            self.progress.emit(done, total,
                               "Erledigt: %s" % self._label(book_uuid))
        return done

    def _run_read_marks(self, client, read_op, unread_op, checkpoint,
                        done, total):
        """Ausgewählten Lesestatus übertragen."""
        if read_op is not None:
            checkpoint()
            self.progress.emit(done, total, "Als gelesen markieren")
            try:
                client.mark_read_many(self.read_marks)
            except Exception as exc:
                self._record_failure(read_op, exc, client)
                if self._fatal_error(exc):
                    raise
            else:
                self._finish_operation(read_op, OP_SUCCEEDED)
                self._refresh_counts()
                self.progress.emit(done, total, "Als gelesen markiert")
            done += 1
        if unread_op is not None:
            checkpoint()
            self.progress.emit(done, total, "Als ungelesen markieren")
            try:
                client.mark_read_many(self.read_unmarks, finished=False)
            except Exception as exc:
                self._record_failure(unread_op, exc, client)
                if self._fatal_error(exc):
                    raise
            else:
                self._finish_operation(unread_op, OP_SUCCEEDED)
                self._refresh_counts()
                self.progress.emit(done, total, "Als ungelesen markiert")
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
        for book_uuid, tolino_id, series, operation in collection_ops:
            if known is None:
                break
            checkpoint()
            operation["needs_write"] = (tolino_id, series) not in known
            if not operation["needs_write"]:
                remember_collection(self.state, book_uuid, series)
                self._finish_operation(operation, OP_SKIPPED,
                                       reason="schon in der Cloud")
                self._persist_state()
                done += 1
                self._refresh_counts()
                continue
            if operation["needs_write"]:
                self.progress.emit(done, total,
                                   "Serie in der Cloud: %s" % series)
                try:
                    revision = client.add_to_collection(
                        tolino_id, series, revision=revision)
                except Exception as exc:
                    self._record_failure(operation, exc, client)
                    if self._fatal_error(exc):
                        raise
                    # Revision ist nach einem Fehler nicht mehr
                    # verlaesslich: der naechste Aufruf liest neu.
                    revision = None
                    done += 1
                    continue
                known.add((tolino_id, series))
            if operation.get("needs_write"):
                remember_collection(self.state, book_uuid, series)
            self._finish_operation(operation, OP_SUCCEEDED)
            self._persist_state()
            done += 1
            self._refresh_counts()
        self.progress.emit(done, total, "Serien geprüft")
        return done

    def _run(self):
        state = self.state
        client = None
        try:
            client = make_client(self.store)
            remote_ids, inventory_incomplete = self._load_remote_ids(client)
            # Nur bereits mit Cloud-ID planbare Zuordnungen zählen initial;
            # neue Uploads ergänzen die Liste erst nach ihrem Erfolg.
            initial_collections = plan_collection_updates(self.series_map, state)
            (meta_ops, upload_ops, delete_ops, read_op, unread_op,
             initial_collection_ops, total) = self._plan_operations(initial_collections)
            initial_collection_keys = {(book_uuid, series)
                                       for book_uuid, _tolino_id, series
                                       in initial_collections}
            done = 0

            def checkpoint():
                if not self.cancelled:
                    return
                # Alle geplanten Operationen, die noch nicht gestartet sind,
                # bleiben «nicht begonnen» -- der Bericht weist sie aus.
                self._refresh_counts()
                self.completed.emit(state, client.refresh, self.updates)
                raise _SyncCancelled()

            # Erst die in-place Metadaten-Updates, dann die Uploads.
            for record, operation in meta_ops:
                checkpoint()
                self.progress.emit(
                    done, total,
                    "Metadaten: %s" % self._label(
                        record["book_uuid"], record["book_id"],
                        (record.get("fields") or {}).get("title")))
                try:
                    client.update_metadata(record["tolino_id"], record["fields"])
                except Exception as exc:
                    self._record_failure(operation, exc, client)
                    if self._fatal_error(exc):
                        raise
                    done += 1
                    continue
                self._finish_operation(operation, OP_SUCCEEDED)
                self._refresh_counts()
                # /meta bestätigt Titel, Autor und ISBN, nicht den Dateiinhalt.
                self._confirm_fingerprints(
                    record["book_uuid"], ("meta_fp", "fingerprint"))
                self._persist_state()
                done += 1
            for job, operation in upload_ops:
                checkpoint()
                # Der Laufzeitplan kann diese Serien erst nach dem Upload
                # ergänzen; offene Sammlungen vorab für den Zähler merken.
                self.progress.emit(done, total, "Lade hoch: %s (%s)" %
                                   (job.label(), job.format_name))
                try:
                    new_id = client.upload(job.path)
                except Exception as exc:
                    self._record_failure(operation, exc, client)
                    if self._fatal_error(exc):
                        raise
                    done += 1
                    self.progress.emit(done, total, "Fehlgeschlagen: %s"
                                       % job.label())
                    continue
                # Plan-Eintrag kopieren: fingerprints bleiben erhalten,
                # nur die tolino-ID wird neu.
                entry = dict(state.get(job.book_uuid) or {})
                entry.update({"tolino_id": new_id, "calibre_id": job.book_id})
                # Neue Cloud-Kopie: alte Serien-Zuordnungen gelten nicht mehr.
                entry.pop("collections", None)
                state[job.book_uuid] = entry
                # Der Upload traegt Datei UND Metadaten: alle drei Werte
                # sind jetzt belegt.
                self._confirm_fingerprints(job.book_uuid)
                self.updates.append((job.book_id, new_id))
                self._finish_operation(operation, OP_SUCCEEDED)
                # SOFORT sichern: Cover, Loeschen oder ein spaeterer Fehler
                # duerfen die Zuordnung nicht mehr verlieren.
                self._persist_state()
                if job.cover_path:
                    try:
                        client.upload_cover(new_id, job.cover_path)
                    except Exception as exc:
                        # Der Upload selbst ist erfolgreich -- Teilstand
                        # bleibt, der Fehler wird gezaehlt, das naechste Buch
                        # laeuft weiter.
                        cover_op = self._add_operation(
                            "cover", "Buchcover: %s" % job.label(),
                            book_uuid=job.book_uuid)
                        self._record_failure(cover_op, exc, client)
                        if self._fatal_error(exc):
                            raise
                self._remove_replaced_copy(
                    client, job, remote_ids, inventory_incomplete)
                done += 1
                self._refresh_counts()
                self.progress.emit(done, total,
                                   "Hochgeladen: %s" % job.label())
            done = self._run_deletions(
                client, delete_ops, remote_ids, inventory_incomplete,
                checkpoint, state, done, total)
            # Lesestatus nur für ausgewählte Bücher übertragen. Sammlungen
            # und andere Angaben bleiben unverändert.
            done = self._run_read_marks(
                client, read_op, unread_op, checkpoint, done, total)
            series_items = plan_collection_updates(self.series_map, state)
            collection_ops = []
            initial_by_key = {(book_uuid, series): operation
                              for book_uuid, _tolino_id, series, operation
                              in initial_collection_ops}
            for book_uuid, tolino_id, series in series_items:
                operation = initial_by_key.get((book_uuid, series))
                if operation is None:
                    operation = self._add_operation(
                        "collection", "Serie in tolino: %s" % series,
                        book_uuid=book_uuid, series=series)
                collection_ops.append((book_uuid, tolino_id, series, operation))
            # Das Total zählt Operationen, die durch Uploads neu hinzukamen.
            total += sum((book_uuid, series) not in initial_collection_keys
                         for book_uuid, _tolino_id, series in series_items)
            self.outcome["total"] = total
            done = self._run_collections(
                client, collection_ops, checkpoint, done, total)
            self._refresh_counts()
            self.completed.emit(state, client.refresh, self.updates)
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
            self.failed.emit(
                message,
                client.refresh if client else self.store.refresh_token)


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
        with token_session():
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
            # Die Antwort hatte gar nicht die Form eines Bestands: das darf
            # nie als leere Cloud durchgehen -- sonst saehe jedes Buch wie
            # «noch nicht in tolino» aus. Die Meldung nennt die Felder,
            # ohne die vollständige Antwort preiszugeben.
            if not getattr(client, "inventory_envelope_ok", True):
                raise TolinoApiError(
                    "; ".join(getattr(client, "inventory_problems", ())
                              or ("unlesbare Antwort",)),
                    code="N02")
            problems = list(getattr(client, "inventory_problems", ()) or ())
            if problems:
                # Einzelne undeutbare Datensaetze: der Vergleich laeuft mit
                # den lesbaren weiter, Loeschungen verwirft der SyncWorker
                # spaeter und meldet sie.
                log_warning("Cloud-Bestand nur teilweise deutbar: %s"
                            % "; ".join(problems[:5]))
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
                read_ids=read_ids)
            inventory_complete = (
                getattr(client, "inventory_ids_complete", True)
                and not getattr(client, "inventory_problems", ()))
            if inventory_complete:
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
                "refresh": client.refresh,
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

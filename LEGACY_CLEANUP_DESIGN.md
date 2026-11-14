# Entwurf: sichere Altdaten-Aufräumung

**Status:** Implementiert. Die Produktionslogik liegt in `calibre_plugin/config.py`, die UI-Anbindung in `calibre_plugin/ui_dashboard_parts.py`. Die 13 neuen Sicherheitstests stehen in `planning_tests.py:2561-2770`; die vollständige Suite läuft mit 596 Tests, 1 Skip und 0 Fehlern.

## 1. Geprüfte Fakten und Korrekturen

### 1.1 Symbole und Konstanten

Alle folgenden Namen existieren in `calibre_plugin/config.py` auf Modulebene. Ein `__all__` gibt es in `config.py` nicht; die Namen sind über `config.<Name>` erreichbar. Namen mit führendem Unterstrich sind bewusst privat, können aber von neuen Funktionen **innerhalb derselben Datei** verwendet werden.

| Name | Tatsächlicher Wert / Bedeutung | Beleg |
|---|---|---|
| `SCHEMA_VERSION` | `2` | `config.py:12` |
| `PREFERENCE_NAMESPACE` | `"plugins/Tolino Cloud Sync"` | `config.py:13` |
| `LEGACY_NAMESPACE` | `"plugins/Tolino Cloud Sync Lite"` | `config.py:14` |
| `BACKUP_NAMESPACE` | `"plugins/Tolino Cloud Sync Migration Backup 0.1.08"` | `config.py:15` |
| `PREFERENCES` | aktiver `JSONConfig(PREFERENCE_NAMESPACE)` | `config.py:16` |
| `LEGACY_PREFERENCES` | `JSONConfig(LEGACY_NAMESPACE)` | `config.py:17` |
| `LEGACY_BACKUP` | `JSONConfig(BACKUP_NAMESPACE)` | `config.py:18` |
| `CLEANUP_BACKUP_NAMESPACE` | `"plugins/Tolino Cloud Sync Cleanup Backup"` | `config.py:19` |
| `CLEANUP_BACKUP_VERSION` | `1` | `config.py:20` |
| `CLEANUP_BACKUP` | `JSONConfig(CLEANUP_BACKUP_NAMESPACE)` | `config.py:21` |
| `LEGACY_PREFERENCE_KEYS` | 10er-Tuple, siehe unten | `config.py:22-26` |
| `_SETTINGS_LOCK` | `threading.RLock()` | `config.py:37` |
| `_USING_LEGACY_PREFERENCES` | Modul-Flag, initial `False` | `config.py:39` |

Die tatsächliche Liste der alten Preferences-Keys ist:

```python
LEGACY_PREFERENCE_KEYS = (
    "refresh_token", "partner_id", "hardware_id", "state",
    "enable_deletions", "compare_authors", "compare_title",
    "compare_isbn", "sync_collections", "upload_covers",
)
```

Beleg: `config.py:22-26`.

**Wichtig:** `accounts`, `active_account`, `preferred_formats` und `schema_version` stehen **nicht** in dieser Liste. Der Entwurf darf sie daher nicht als „alte Preferences-Keys“ bezeichnen und im sicheren Standardmodus nicht löschen.

### 1.2 Laufzeit-Umbiegung von `PREFERENCES`

`ensure_preferences_migrated()` kann `PREFERENCES` auf `LEGACY_PREFERENCES` umbiegen und `_USING_LEGACY_PREFERENCES` auf `True` setzen, wenn die Migration fehlschlägt: `config.py:184-193`.

Daraus folgen zwei Korrekturen am Entwurf:

- Cleanup-Funktionen dürfen `PREFERENCES` nicht als Default-Argument binden. Sie müssen `current=None` verwenden und den globalen Namen erst im Funktionskörper auflösen. Das ist dasselbe Muster wie bei `migrate_preferences()` (`config.py:149`) und `legacy_report()` (`config.py:250`).
- Vor jedem Löschen muss geprüft werden: `_USING_LEGACY_PREFERENCES` ist `False` **und** `PREFERENCES is not LEGACY_PREFERENCES`. Andernfalls ist der alte Store aktiv und darf nicht geleert werden.

`_SETTINGS_LOCK` wird bereits von `migrate_preferences()` (`config.py:152`) und `legacy_report()` (`config.py:252`) verwendet und ist für die neuen Funktionen wiederverwendbar.

### 1.3 Vorhandenes Migrationsbackup

Der bestehende Backup-Namespace lautet buchstäblich:

```text
plugins/Tolino Cloud Sync Migration Backup 0.1.08
```

Beleg: `config.py:15`.

Das vorhandene Backup hat **keine Version, keinen Digest und keinen separaten `current`-/`legacy`-Payload**. `migrate_preferences()` macht:

```python
old = _snapshot(legacy)
...
if old and not _snapshot(backup):
    backup.update(old)
    _commit(backup)
```

Beleg: `config.py:159-164`.

Das Backup ist damit ein **flacher Snapshot der alten Store-Schlüssel**. Es wird nur angelegt, wenn es noch leer ist, und danach nicht aktualisiert. `legacy_report()` prüft lediglich, ob der Snapshot nicht leer ist (`config.py:253`, `config.py:275`).

**Korrektur:** Der Cleanup-Backup ist kein „Version 1 des vorhandenen Backups“, sondern ein **neuer, separater Backup-Store** mit `CLEANUP_BACKUP_NAMESPACE` (`config.py:19`). Der vorhandene `BACKUP_NAMESPACE`/`LEGACY_BACKUP` bleibt unangetastet. Im Repository gab es keinen freien vorhandenen Namespace; die Implementierung legt deshalb die neue Konstante `CLEANUP_BACKUP_NAMESPACE` an, die vorher nicht existierte.

### 1.4 State-Key-Formate

Aktuelle State-Keys werden aus der Buchkennung gebildet:

- echte Calibre-UUID, oder
- `calibre-<12-hex-scope>-<zahl>`.

Belege:

- `valid_book_uuid()` akzeptiert nur UUID oder `calibre-[0-9a-f]{12}-\d+`: `identifiers.py:8`, `identifiers.py:16-25`.
- `calibre_key()` erzeugt `calibre-<scope>-<number>`: `identifiers.py:36-42`.
- `local_metadata()` verwendet `book_uuid(item) or calibre_key(book_id, context.library_scope)`: `dashboard_plan.py:29-30`.
- Der Scope wird aus der Bibliotheks-ID gehasht: `library.py:24-35`.
- Gelesen werden State-Einträge über diese aktuellen Keys, z. B. in `comparison.py:228-242`, `plan.py:34/42`, `plan.py:164/167` und `ui_workers.py:314-320`.

Die alte ungescopte Form ist `calibre-<zahl>`, zum Beispiel `calibre-7`. Sie ist **kein gültiger aktueller Key**: `valid_book_uuid("calibre-7")` liefert leer (`identifiers.py:16-25`; Test `identifiers_tests.py:91`).

Der Test `FallbackKeyPlanTests.test_fallback_key_stays_untouched_by_the_sync_planner` zeigt zusätzlich, dass der Sync-Planer einen alten `calibre-7`-Key nicht auflöst oder löscht: `planning_tests.py:2165-2185`.

**Korrektur:** „Alte State-Keys“ sind ausschließlich die ungescopten `calibre-<zahl>`-Keys, nicht jeder Key, der mit `calibre-` beginnt. Gültige `calibre-<12-hex>-<zahl>`-Keys sind aktuelle Keys und dürfen nie gelöscht werden.

**Eine Ausnahme muss der Entwurf ausdrücklich absichern:** `comparison.py:463` kann für die Anzeige `book_uuid or str(metadata.get("uuid") or "")` weiterreichen. In der Produktion ist `metadata["uuid"]` bereits durch `dashboard_plan.py:29-30` auf UUID oder Scoped-Key normalisiert. Für absolute Sicherheit darf ein alter Key trotzdem nicht gelöscht werden, wenn er als Rohwert in einem aktuellen `uuid`-Feld vorkommt; dieser Fall bleibt im Plan ein Orphan und wird nur gemeldet. Dafür muss der Plan optional die Bibliotheksdaten lesen; ohne Bibliotheksprüfung werden State-Keys nicht automatisch gelöscht.

**Migrationspfad:** `migrate_preferences()` kopiert die Konten samt `state` unverändert per `copy.deepcopy` (`config.py:166-170`). Es normalisiert oder entfernt alte State-Keys nicht. Der Cleanup muss deshalb davon ausgehen, dass alte Keys nach der Migration noch vorhanden sein können.

### 1.5 Bericht ab `config.py:248`

Die frühere Behauptung ist **korrekt**: `legacy_report()` zählt jeden State-Key, der mit `calibre-` beginnt, als Fallback:

```python
fallback_keys += sum(
    1 for key in state if str(key).startswith("calibre-"))
```

Beleg: `config.py:264-265`.

Der Test `test_legacy_report_counts_without_exposing_values` verwendet den gültigen Scoped-Key `calibre-abcdef123456-7` und erwartet trotzdem `fallback_state_keys == 1`: `planning_tests.py:2543-2555`.

**Korrektur:** `fallback_state_keys` ist für eine Löschmenge zu breit und darf nicht als Cleanup-Plan verwendet werden. Der Bericht kann unverändert bleiben (er ist read-only), aber der Cleanup braucht eigene Zähler:

- `legacy_unscoped_state_keys`: nur `^calibre-\d+$`
- `duplicate_legacy_state_keys`: davon exakt dupliziert und löschbar
- `orphan_legacy_state_keys`: davon ohne sichere Zuordnung, also behalten

## 2. Korrigierte Löschregeln

### 2.1 Alte Preferences-Keys

Im Standardmodus werden ausschließlich die Schlüssel aus `LEGACY_PREFERENCE_KEYS` (`config.py:22-26`) aus `LEGACY_PREFERENCES` entfernt.

Nicht entfernt werden:

- `accounts`, `active_account`, `preferred_formats` im Legacy-Store,
- der aktive `PREFERENCES`-Store,
- der bestehende `LEGACY_BACKUP`.

Diese Zurückhaltung ist Absicht: Die alten Konten können bei einem Downgrade oder als manuelle Wiederherstellung noch gebraucht werden. Ein separater „Komplett löschen“-Modus wäre eine eigene Entscheidung und ist nicht Teil dieses Entwurfs.

### 2.2 Alte State-Keys

Ein alter Key `calibre-<zahl>` wird nur gelöscht, wenn **innerhalb desselben Kontos** alle folgenden Bedingungen erfüllt sind:

1. Der Key matcht `^calibre-\d+$`.
2. Es existiert genau **ein** gültiger aktueller Zielschlüssel im selben `state`-Dict, dessen Eintrag denselben `calibre_id` trägt.
3. Der Eintrag unter dem alten Key ist **exakt gleich** dem Eintrag unter dem Zielschlüssel.
4. Der Zielschlüssel ist eine echte UUID oder ein gültiger `calibre-<12-hex>-<zahl>`-Key.
5. Es gibt keinen zweiten gültigen Zielschlüssel mit demselben `calibre_id`; bei Ambiguität wird nichts gelöscht.

Nicht gelöscht werden:

- alte Keys ohne Ziel oder mit abweichendem Eintrag,
- alte Keys ohne `calibre_id`,
- gültige UUID-/Scoped-Keys,
- Keys in anderen Konten.

Die Regel ist mit der Migration vereinbar, weil die Migration alte State-Keys unverändert kopiert (`config.py:166-170`) und die aktuellen Lesepfade State-Einträge über `valid_book_uuid()` bzw. die daraus gebildeten Scoped-Keys lesen (`comparison.py:228-242`, `plan.py:34/42`, `ui_workers.py:314-320`). Die Anzeige-Ausnahme in `comparison.py:463` wird durch die zusätzliche Rohwert-Prüfung abgedeckt; ohne diese Prüfung bleibt der Key stehen. Nur bei exakter Duplikation und ohne Rohwert-Kollision ist das Löschen ohne Informationsverlust.

## 3. Implementierte API

Die drei Funktionen sind in `config.py` implementiert:

- `plan_legacy_cleanup(current=None, legacy=None, database=None)` – `config.py:281`
- `create_legacy_cleanup_backup(plan, current=None, legacy=None, backup=None)` – `config.py:343`
- `apply_legacy_cleanup(plan, confirmed_backup, current=None, legacy=None, database=None, backup=None)` – `config.py:384`

Alle drei Funktionen gehören in `config.py`, damit sie `_SETTINGS_LOCK`, `_snapshot`, `_commit`, `PREFERENCES`, `LEGACY_PREFERENCES` und `LEGACY_BACKUP` ohne neue Schicht nutzen können. `database` ist optional: Ohne Datenbank werden State-Keys nur gezählt, nicht gelöscht; mit Datenbank wird die Rohwert-Kollision aus `comparison.py:463` zusätzlich ausgeschlossen.

`plan` enthält keine Werte und keine Tokens:

```python
{
    "preference_keys": ("refresh_token", ...),
    "state_keys": (
        ("konto-a", "calibre-7", "calibre-abcdef123456-7"),
        ...
    ),
    "counts": {...},
    "digest": "sha256:...",
}
```

### 3.1 Backup

Der neue Backup-Store nutzt den neuen Namespace `"plugins/Tolino Cloud Sync Cleanup Backup"` (`config.py:19`) und kollidiert nicht mit `BACKUP_NAMESPACE` (`config.py:15`).

Der neue Backup-Payload ist bewusst anders als das bestehende Migrationsbackup:

```python
{
    "version": 1,
    "created_at": "...",
    "current": {...},   # vollständiger PREFERENCES-Snapshot
    "legacy": {...},    # vollständiger LEGACY_PREFERENCES-Snapshot
}
```

Ablauf:

1. Snapshot schreiben.
2. Zurücklesen.
3. SHA-256 über kanonisches JSON bilden.
4. Digest separat speichern.
5. Nur bei erfolgreicher Rücklese- und Digest-Prüfung einen `confirmed_backup`-Token zurückgeben.

Der Token bindet den Backup-Digest an den Plan-Digest. `apply_legacy_cleanup()` akzeptiert keinen bloßen `confirmed=True`-Boolean.

### 3.2 Apply-Schutz

`apply_legacy_cleanup()` muss unter `_SETTINGS_LOCK` prüfen:

- `_USING_LEGACY_PREFERENCES is False`
- `PREFERENCES is not LEGACY_PREFERENCES`
- `schema_version >= SCHEMA_VERSION`
- Backup existiert, ist lesbar und der Digest stimmt
- der neu berechnete Plan entspricht dem bestätigten Plan
- der Plan-Digest im Token stimmt
- es gibt keine Ambiguität bei den Zielschlüsseln

Bei einem Fehlschlag wird nichts gelöscht. Bei Erfolg werden nur die im Plan genannten Schlüssel entfernt; es wird kein Store geleert und kein `clear()` verwendet.

## 4. UI

Die bestehende read-only Ansicht `show_legacy_report()` in `ui_dashboard_parts.py:247` bleibt unverändert. Daneben ist der separate Ablauf `show_legacy_cleanup()` in `ui_dashboard_parts.py:277` implementiert:

1. **„Altdaten prüfen …“** – Plan und Zähler anzeigen, keine Werte/Tokens.
2. **„Sicherung erstellen und prüfen“** – neues Cleanup-Backup schreiben, zurücklesen und Digest anzeigen.
3. **„Jetzt aufräumen“** – erst nach verifiziertem Backup aktiv; zusätzlicher Bestätigungsdialog, Default „Nein“.

Nach dem Lauf zeigt die UI die entfernten Zähler; der bestehende Bericht kann anschließend erneut geöffnet werden. Es werden keine Tokenwerte geloggt oder angezeigt.

## 5. Testliste

Bestehende Tests bleiben erhalten, insbesondere:

- `test_legacy_report_counts_without_exposing_values` (`planning_tests.py:2543-2558`) – belegt die zu breite Fallback-Zählung.
- `test_fallback_key_stays_untouched_by_the_sync_planner` (`planning_tests.py:2180-2185`) – belegt, dass der alte `calibre-7`-Key vom Planer nicht aufgelöst wird.
- `test_old_accounts_are_moved_to_the_new_namespace_and_backed_up` (`planning_tests.py:2471-2488`) – belegt das flache Migrationsbackup.
- `test_a_failed_migration_keeps_the_legacy_store_active` (`planning_tests.py:2516-2531`) – belegt die `PREFERENCES`-Umbiegung.

Neu abgesichert (`LegacyCleanupTests`, `planning_tests.py:2561-2770`):

1. Kein verifiziertes Backup → `apply` verweigert, Store unverändert.
2. Backup fehlt, ist beschädigt oder nicht zurücklesbar → Verweigerung.
3. Plan/Backup veraltet, weil sich State oder Preferences geändert haben → Verweigerung.
4. `_USING_LEGACY_PREFERENCES is True` oder `PREFERENCES is LEGACY_PREFERENCES` → Verweigerung.
5. Nur ungescopte `calibre-<zahl>`-Keys werden als Cleanup-Kandidaten gezählt; `calibre-abcdef123456-7` und UUIDs bleiben unangetastet.
6. Exaktes Duplikat unter genau einem gültigen Zielschlüssel wird entfernt.
7. Abweichender Eintrag, fehlender `calibre_id`, fehlender Zielschlüssel oder mehrere Zielschlüssel → nichts löschen.
8. Zwei Konten mit demselben alten Key: Nur das Konto mit dem exakten Duplikat wird bereinigt; das andere bleibt unverändert.
9. Legacy-Preferences: nur die 10 Keys aus `LEGACY_PREFERENCE_KEYS` verschwinden; `accounts`, `active_account` und `preferred_formats` bleiben.
10. Das bestehende `LEGACY_BACKUP` wird nicht überschrieben und bleibt lesbar.
11. Zweiter Lauf ist idempotent und meldet „nichts zu tun“.
12. Plan, Bericht und Log enthalten keine Tokens oder State-Werte.
13. Ein alter `calibre-<zahl>`-Key, der zugleich als Rohwert in einem aktuellen `uuid`-Feld vorkommt, wird nicht gelöscht und bleibt als Orphan im Plan.

## 6. Bewusste Grenzen

- Kein automatisches Löschen von Orphan-State-Keys; sie können die einzige Buchzuordnung enthalten.
- Kein automatisches Löschen des Legacy-Kontobestands; das wäre ein separater Komplett-Modus.
- Ein Downgrade nach dem Entfernen der Legacy-Preferences kann den alten Namespace nicht mehr als Fallback nutzen; das Cleanup-Backup bleibt als Wiederherstellungsquelle.
- Keine neue Architektur: Die Funktionen bleiben in `config.py`; die UI-Anbindung bleibt in `ui_dashboard_parts.py`; die State-Key-Prüfung nutzt `valid_book_uuid()` aus `identifiers.py`. Der neue Cleanup-Backup-Namespace ist die einzige zusätzliche Persistenzfläche.

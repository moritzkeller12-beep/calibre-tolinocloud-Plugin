# Änderungen

## 0.1.07

- Cover-Uploads senden den von tolino erwarteten `control`-Block; ohne ihn antwortet der Cover-Endpunkt mit HTTP 400.
- Das Vergleichsfenster trägt den Titel „Bibliothek vergleichen“ und breitere Aktionsspalten (80/100 px), damit die Kopfzeilen vollständig sichtbar bleiben.
- Die Browser-Anmeldung räumt das Chromium-Fenster auch bei Fehlern auf; redundante interne Fehler-Wrapper wurden entfernt.

- Beim Konto-Wechsel bleiben Lesestatus und Fortschritt auch für Bücher ohne echte Calibre-UUID erhalten; die Zuordnung nutzt einen bibliotheksbezogenen Fallback aus Bibliothekskennung und Calibre-Nummer. Eine echte UUID hat Vorrang; alte Einträge werden nicht mehr fälschlich darübergelegt.
- Alte ungescopte `calibre-<id>`-Einträge werden nicht mehr übernommen und nicht neu erzeugt; Zuordnungen laufen über echte UUIDs bzw. den bibliotheksbezogenen Fallback.
- Fehlt PyCryptodome für verschlüsselte Web-Reader-Tokens, meldet die Anmeldung einen klaren Fehler mit Code A01 statt eines stillen Fehlschlags.
- Kontobezogene `#tolino_id`-Zuordnungen werden beim ersten Vergleich in den State übernommen; danach ist der State maßgeblich.
- Die frühere Einzel-ID in `#tolino_id` wird beim ersten kontobezogenen Schreiben ersetzt.
- Der alte `/registerhw`-Fallback entfällt.
- Der lokale Metadaten-Snapshot läuft in einem eigenen Thread; Calibres thread-sichere `new_api` wird dabei nur lesend genutzt.
- Die Auswahlkästchen im Vergleichsfenster werden linksbündig statt mittig gezeichnet.
- Aktionskästchen im Vergleichsfenster lassen sich unter Qt6 wieder abwählen; zuvor blieben bereits gesetzte Upload-, Metadaten- und Lesestatus-Häkchen gesetzt.
- Debug-Durchlauf: Callback-Bindungen im Sync-Worker korrigiert, doppelte Rundungs-Casts und ungenutzte Testreste entfernt. Ein bestehender Abbruchtest sichert die Reihenfolge von Checkpoint und Cloud-Aufruf ab.
- Die Live-Grab-Normalisierung gibt bereits normalisierte Daten unverändert zurück; damit bleibt der Python-3.11-Testlauf grün.
- HTTP-Fehlerantworten werden nach dem Auslesen geschlossen; Python 3.14 meldet damit keine offenen `HTTPError`-Ressourcen mehr, auch bei fehlgeschlagenen Downloads.
- Zwei doppelte Tests entfernt und die README auf Installation, Benutzung und Hinweise gestrafft.
- Legacy-Prüfung: In diesem Patch wird keine Altlast entfernt; die bewussten Kompatibilitätspfade (historischer Config-Namespace, `adopt_column_tolino_ids`, alte Einzel-`#tolino_id`-Migration, `dbpath`-Fallback) bleiben bestehen.

## 0.1.06

- Lesestatus und Lesefortschritt werden kontobezogen gespeichert; `#tolino_read` und `#tolino_progress` zeigen nur den Stand des aktiven Kontos, fehlende Werte bleiben leer. Die neue Spalte `#tolino_progress` (0–100, optional mit Position) zeigt auch Cloud-Lesestände und Gelesen-Marken; unbekannte Abrufe lassen sie unverändert, ein erfolgreicher Abruf ohne Bookmark leert sie.
- Der Vergleich extrahiert ISBNs aus tolino-Produktkennungen, ordnet unsichere Treffer konservativ zu und entfernt veraltete Cloud-IDs samt altem Lesestand.
- Der Prepare-Zustand wird vor der Migration gesichert und nur bei Änderung einmal geschrieben. Parallele Konto-Wechsel bleiben getrennt; Token-Rotation und Keep-alive sind abgesichert.
- Der Release-Build entfernt Kommentare und Docstrings; Download-, Browser- und Qt6-Randfälle sind gehärtet.
- Behebt doppelt gezeichnete Auswahlkästchen im Vergleichsfenster; der Indikator wird einmal mittig gezeichnet.

## 0.1.05

- Der Build entfernt Kommentare, behält aber Code, Kodierung und Zeilennummern.
- Zustands-, Speicher- und Anmeldefehler werden sauber gemeldet; beschädigte Zustandseinträge stoppen den Sync nicht.
- Browser-/Token-Verwaltung, Qt6-Enums, Transport und Konto-Kontext wurden gestrafft.

## 0.1.04

- `#tolino_read` ist nur noch informativ; Lesestatusänderungen werden ausschließlich im Vergleichsfenster ausgewählt.
- Behebt Qt6-Probleme mit Spaltenbreiten und Dialogcodes; ergänzt einen PyQt6-Offscreen-Test.

## 0.1.03

- Neue Ignorieren-Spalte `#tolino_ignore` und je Calibre-Spalte ein eigener Anlegen-Knopf.
- Zugangsdaten liegen nur noch im Konto; das Token-Feld zeigt keine gespeicherten Werte an.

## 0.1.02

- Mehrkonten-Unterstützung für `#tolino_id`, gestraffte Browser-/Token-Verwaltung und Serien-Sammlungen.

## 0.1.01

- Buchdownloads im Hintergrund, zuverlässigeres Füllen von `#tolino_id` und gehärtete Download-Weiterleitungen.

## 0.1.00

- Erste Version: Abgleich von Büchern, Metadaten, Lesestatus und Serien sowie kontobezogene Anmeldung und Cloud-Downloads.

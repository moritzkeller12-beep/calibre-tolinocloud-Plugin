# Änderungen

## 0.1.08

- Nach einem abgelehnten Zugang (A01/B01/B02) steht in der Statuszeile «Anmeldung abgelehnt – bitte neu anmelden.» statt weiter «Angemeldet.».
- «Speichern und schliessen» übernimmt einen eingetippten Token; Enter speichert, statt die Browser-Anmeldung zu starten.
- Jeder Spalten-Knopf prüft und meldet nur seine eigene Calibre-Spalte.
- Nach einem Upload werden Lesestatus und Fortschritt der ersetzten Kopie geleert; beide werden getrennt geschrieben, damit ein Fehler den anderen nicht blockiert.
- Alte `#tolino_id`- und `dbpath`-Migrationspfade entfallen; Config-Speicher, Konten und Tokens bleiben unverändert.
- Änderungen an der Einstellungsdatei (zweites Calibre-Fenster, anderes Konto) sind im laufenden Programm sofort sichtbar; halb geschriebene oder leere Stände werden nie übernommen, und ein Schreibvorgang bricht dann ab, statt Daten zu verlieren.
- Der Code ist gebündelt und gestrafft: eine Planungsdatei statt drei, gemeinsame Helfer statt kopierter Stubs in Produktion und Tests. Verhalten unverändert; die Suite prüft dasselbe Verhalten mit 115 Tests (die frühere Angabe «624» war falsch).
- Pyflakes-Lint ist wieder grün: die Schleifenvariable in `plan_collection_updates` überschattet nicht mehr `book_uuid`, und der doppelte `progress_by_id`-Stub im Test entfällt.
- Die Live-Tests gegen die echte tolino-Cloud liegen nur noch lokal und werden von Git ignoriert; sie laufen nicht mehr in der GitHub-Suite.
- Neue Calibre-Spalten entstehen nur noch über Calibres `create_custom_column`. Die Nachbildung der Calibre-internen Spalteninitialisierung ist entfernt; nach dem Anlegen fordert die Oberfläche zum Neustart auf, und ein zweiter Klick legt in derselben Bibliothek nichts doppelt an.
- `library_scope()` unterscheidet Bibliotheken ohne `library_id` wieder über den `dbpath`; nur ohne beide Kennungen gilt der feste Ersatz-Scope.
- `#tolino_id` ist eindeutig eine informative Spiegelspalte: führend für den Abgleich ist allein der Konto-State, der Leseweg aus der Spalte ist entfernt.
- Die Testsuite ist kleiner: die echten Calibre-Prozess-Tests für das Schreiben ohne Neustart entfallen, Einmal-Helfer liegen bei ihrem Testmodul, und doppelt abgesicherte Prüfungen wurden zusammengeführt. Gespiegelte Varianten desselben Vertrags (gelesen/ungelesen, eigenes/fremdes Konto, Auswahl/Automatik, angelegte/vorhandene Spalte, Lesestatus/Fortschritt) laufen jetzt als ein Test mit `subTest`; unterschiedliche Fehlerfälle bleiben getrennt.
- Die Dialogtests laufen mit echtem PyQt6 (offscreen) statt gegen einen handgeschriebenen Qt-Nachbau; Doubles bleiben nur an modalen Grenzen (QMessageBox, QMenu, QFileDialog) und beim QThread. Ohne PyQt6 werden diese Tests übersprungen.
- Die Testsuite ist auf das wichtigste Verhalten reduziert: 115 Tests in neun Modulen (Konten, Token-Rotation und Schreibpfade des tolino-Clients über echte Sockets – Upload-Multipart, Metadaten mit Read-back, Gelesen-Marken, Sammlungen, Löschen, Download-Prüfungen –, sync-data-Patches für Lesestatus und Tags, Calibre-Spalten und Bibliothekszuordnung, Planung und Bestandsabgleich, Sync-Worker, Dashboard-Aktionen, Vergleichsfenster mit echter Qt-Tabelle samt Klick auf die Auswahlzelle, Browser-Login-Fallbacks) plus Aufbau-, Transport- und Verschlüsselungstests. Entfallen sind Tests für interne Helfer, Aufrufzählungen und die frühere Prozess-/Stub-Infrastruktur; die gemeinsame Testhilfe schrumpfte entsprechend.

## 0.1.07

- Fehlerbehebungen und Härtung für Anmeldung, Cloud-Sync, Lesestatus und UI; keine Legacy-Pfade entfernt.

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

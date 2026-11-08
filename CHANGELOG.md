# Änderungen

## 0.1.07

- Beim Konto-Wechsel bleiben Lesestatus und Fortschritt auch für Bücher ohne echte Calibre-UUID erhalten; die Zuordnung nutzt einen bibliotheksbezogenen Fallback aus Bibliothekskennung und Calibre-Nummer. Eine echte UUID hat Vorrang; alte Einträge werden nicht mehr fälschlich darübergelegt.
- Alte `calibre-<id>`-Einträge ohne Bibliothekszuordnung werden nur nach Bestätigung und nur von einer Bibliothek übernommen; die Entscheidung wird kontobezogen gespeichert, auch wenn aktuell kein Buch dazu passt. Ungescopte Schlüssel werden für neue Einträge nicht mehr erzeugt, und gelöschte Einträge werden nicht wiederhergestellt.
- Fehlt PyCryptodome für verschlüsselte Web-Reader-Tokens, meldet die Anmeldung einen klaren Fehler mit Code A01 statt eines stillen Fehlschlags.
- Alte State-Schlüssel werden nicht neu erzeugt; die frühere Einzel-ID in `#tolino_id` wird beim ersten kontobezogenen Schreiben ersetzt, der alte `/registerhw`-Fallback entfällt. Zuordnungen laufen über echte UUIDs bzw. den neuen bibliotheksbezogenen Fallback.
- Der lokale Metadaten-Snapshot läuft in einem eigenen Thread; Calibres thread-sichere `new_api` wird dabei nur lesend genutzt.
- Die Auswahlkästchen im Vergleichsfenster werden linksbündig statt mittig gezeichnet.

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

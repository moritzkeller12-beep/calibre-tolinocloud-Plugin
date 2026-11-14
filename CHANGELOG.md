# Änderungen

## 0.1.08

- State-Release: Die alten `#tolino_id`-Spalten- und `dbpath`-Migrationspfade entfallen; der Config-Speicherkey bleibt als Datenanker unverändert.
- Nach einem Upload werden alter Lesestatus und Fortschritt der ersetzten Kopie aus den Calibre-Spalten entfernt.
- Lesestatus und Fortschritt werden getrennt geschrieben; ein Fehler in einer Spalte blockiert die andere nicht mehr.
- Der Sammlungs-Sync prüft den Abbruch im Skip-Zweig nur noch einmal; Schreibschritte nutzen den Checkpoint von `_step()`.
- Der interne Konten-Schreibpfad wurde gestrafft, ohne die Regeln für Anlage, Aktivierung oder Kontentrennung zu ändern.
- Doppelte Buch-IDs werden vor einer Gelesen-Markierung entfernt, wobei die Reihenfolge erhalten bleibt.

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

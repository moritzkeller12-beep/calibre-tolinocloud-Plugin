# Änderungen

## 0.1.08

- Nach einem abgelehnten Zugang (A01/B01/B02) steht in der Statuszeile «Anmeldung abgelehnt – bitte neu anmelden.» statt weiter «Angemeldet.»; dasselbe gilt, wenn beim optionalen Lesestatus die Anmeldung abgelehnt wird. Gewöhnliche Fehler dieser Abrufe bleiben «unbekannt».
- «Speichern und schliessen» übernimmt einen eingetippten Token; Enter speichert, statt die Browser-Anmeldung zu starten. Ist kein Buchhändler erkennbar, bleibt das Fenster offen, damit die Eingabe nicht verloren geht.
- Jeder Spalten-Knopf prüft und meldet nur seine eigene Calibre-Spalte. Neue Spalten entstehen ausschliesslich über Calibres `create_custom_column`; danach fordert die Oberfläche zum Neustart auf, und ein zweiter Klick legt nichts doppelt an. `library_scope()` unterscheidet Bibliotheken ohne `library_id` über den `dbpath`, und `#tolino_id` bleibt eine rein informative Spiegelspalte.
- Nach einem Upload werden Lesestatus und Fortschritt der ersetzten Kopie geleert; ein gleichzeitig gewählter Lesestatus geht nicht mehr an die alte, dabei gelöschte Kopie. Beide Spalten werden getrennt geschrieben, damit ein Fehler den anderen nicht blockiert.
- Ein nicht gespeicherter Zwischenstand bricht den Sync ab, statt die ersetzte Cloud-Kopie zu löschen; ein nicht gespeicherter Token, eine nicht gespeicherte Anmeldung, nicht gespeicherte Einstellungen und ein fehlgeschlagener Sync-Zustand melden jetzt einen Fehler, statt Erfolg vorzutäuschen. Ein eingetippter Token bleibt bei einem Schreibfehler im Feld stehen.
- Alte `#tolino_id`- und `dbpath`-Migrationspfade entfallen; Config-Speicher, Konten und Tokens bleiben unverändert.
- Änderungen an der Einstellungsdatei (zweites Calibre-Fenster, anderes Konto) sind im laufenden Programm sofort sichtbar; halb geschriebene oder nicht lesbare Stände werden nie übernommen, und ein Schreibvorgang bricht dann ab, statt Daten zu verlieren. Gleichzeitige Schreibvorgänge zweier Calibre-Prozesse sind nicht serialisiert: der Lock ist prozesslokal, ein echter Schreibwettlauf bleibt damit möglich.
- Endet der Flatpak-/Snap-Wrapper eines Anmeldefensters vor seinem Browserkind, gilt der DevTools-Port weiterhin als belegt, bis er nicht mehr antwortet. Der DevTools-WebSocket prüft beim Verbindungsaufbau `Sec-WebSocket-Accept` sowie `Upgrade`/`Connection`; eine fremde HTTP-Antwort gilt nicht mehr als offene Verbindung.
- Datenkonsistenz: Ein unvollständiger Cloud-Bestand bricht den Vergleich mit einem Hinweis ab, statt Zuordnungen zu löschen; ein unbekannter Fortschritt leert `#tolino_progress` nicht; ein Sicherungsfehler der Vorbereitung meldet einen Fehler statt «bereit»; parallele Läufe überschreiben keine fremden Zuordnungen; eine neue Cloud-ID erbt weder Lesestatus noch Sammlungen der alten Kopie. Beim Download überschreibt eine nachträglich angepasste Dateiendung keine vorhandene Datei ohne Rückfrage.
- Der Code ist gebündelt und gestrafft: eine Planungsdatei statt drei, zwei UUID-Helfer statt vier, ein Job-Typ statt zweier, ein eigenes `jwt.py` statt einer privaten Funktion über Modulgrenzen und direkte `qt.core`-Importe statt einer Re-Export-Liste. Verhalten unverändert.

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

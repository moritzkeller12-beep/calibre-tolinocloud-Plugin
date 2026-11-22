# Änderungen

## 0.1.09

- **Anmeldung bleibt auch bei parallelen Calibre-Instanzen stabil.** Login, Relogin und Token-Erneuerung teilen sich eine Dateisperre; unter Windows wird auch eine längere Sperre abgewartet, statt die Anmeldung abzubrechen.
- **Sitzungen halten länger und erneuern sparsamer.** Ein gültiger Access-Token wird mit Ablaufzeit gespeichert und wiederverwendet. Keep-alive und Gültigkeitsprüfung richten sich nach der echten Token-Laufzeit; bei starker Uhrabweichung gilt die relative Laufzeit. Beim Erneuern wird der Scope des Tokens mitgeschickt; nach einer Ablehnung wird einmal der inzwischen gespeicherte Token versucht, und nur der abgelehnte Token verschwindet.
- **Zugangsdaten und Hintergrund-Refresh sind abgesichert.** Einstellungs- und Sperrdatei sind unter POSIX nur für den Benutzer lesbar. `tools/token_refresh.py` erneuert fällige Konten auch ohne geöffnetes Calibre.
- **Cloud-Antworten ohne Patch gelten als leerer Stand.** Antwortet tolino mit gültiger Revision, aber ohne `patches`, wertet das Plugin das als leere Markierungsliste statt als Fehler. Fehlende Revision, `patches: null` oder ein falscher Typ brechen weiterhin mit einem Hinweis ab.
- **Das Dashboard schliesst kontrolliert.** Esc nutzt denselben Weg wie das Fensterkreuz: Ein laufender Sync wird abgebrochen, sichtbare Einstellungen werden gesichert. Scheitert das Speichern, bleibt das Fenster mit einer Meldung offen.
- **Intern aufgeräumt.** Private Helfer mit nur einem Aufrufer und doppelte Testschritte wurden zusammengeführt, die README auf Installation, Benutzung und Entwicklung reduziert; die CI prüft zusätzlich Python 3.10.

## 0.1.08

- **Anmeldung und Token-Eingabe sind klarer.** Abgelehnte Zugänge zeigen «Anmeldung abgelehnt – bitte neu anmelden.» statt «Angemeldet.». Ein eingetippter Token wird mit «Speichern und schliessen» übernommen; Enter speichert, und ohne erkennbaren Buchhändler bleibt das Fenster offen, damit nichts verloren geht.
- **Calibre-Spalten werden einzeln verwaltet.** Jeder Anlegen-Knopf prüft nur seine eigene Spalte und legt sie über Calibres `create_custom_column` an; danach ist ein Neustart nötig, ein zweiter Klick erzeugt keine Dublette. Bibliotheken ohne `library_id` werden über den `dbpath` getrennt; `#tolino_id` bleibt eine informative Spalte. Alte `#tolino_id`- und `dbpath`-Migrationspfade entfallen, Konten und Tokens bleiben erhalten.
- **Ersetzte Cloud-Kopien hinterlassen keinen alten Lesestand.** Nach dem Upload werden Lesestatus und Fortschritt der alten Kopie geleert; eine neue Lesemarke geht nicht an die gelöschte Kopie. Beide Spalten werden getrennt geschrieben, damit ein Fehler die andere nicht blockiert.
- **Speicherfehler werden nicht mehr als Erfolg gemeldet.** Token, Anmeldung, Einstellungen, Sync-Zustand, Geräte-ID, Buchhändler, Abmeldung und Kontoänderungen brechen sichtbar ab. Ein nicht gesicherter Zwischenstand verhindert das Löschen der alten Cloud-Kopie; ein Token bleibt im Feld, das Einstellungsfenster bleibt offen.
- **Teilstände und Einstellungen bleiben konsistent.** Abgebrochene Metadaten- oder Serienänderungen behalten bereits bestätigte Änderungen. Änderungen aus einem zweiten Calibre-Fenster sind sofort sichtbar; halbe oder unlesbare Stände werden nicht übernommen. Eine Dateisperre serialisiert Schreibvorgänge, mit lokalem Lock als Fallback.
- **Sicherheit und Datenkonsistenz verbessert.** Der CDP-Handshake prüft `Sec-WebSocket-Accept`, `Upgrade` und `Connection`; ein beendeter Flatpak-/Snap-Wrapper hält den Port nicht fälschlich frei. Feldweise Sicherungen verhindern, dass parallele Läufe fremde Zuordnungen überschreiben; unvollständige Cloud-Antworten brechen ab, neue Cloud-IDs erben keinen alten Lesestand, und Downloads überschreiben keine Datei ohne Rückfrage.

## 0.1.07

- **Cover-Uploads funktionieren wieder.** Der Client sendet den von tolino erwarteten `control`-Block; ohne ihn antwortete der Cover-Endpunkt mit HTTP 400.
- **Das Vergleichsfenster ist aufgeräumt.** Es heisst «Bibliothek vergleichen», hat breitere Aktionsspalten und linksbündige Auswahlkästchen. Häkchen lassen sich unter Qt6 wieder abwählen, und ein Doppelklick schaltet nicht doppelt um.
- **Die Browser-Anmeldung räumt zuverlässig auf.** Das Chromium-Fenster wird auch bei Fehlern geschlossen; fehlt PyCryptodome für verschlüsselte Token, gibt es einen klaren Hinweis mit Code A01 statt eines stillen Fehlschlags.
- **Kontowechsel behalten den Lesestand.** Für Bücher ohne echte Calibre-UUID wird die Zuordnung über Bibliothek und Calibre-Nummer fortgeführt; echte UUIDs haben Vorrang. Alte ungescopte `calibre-<id>`-Einträge werden nicht übernommen, kontobezogene `#tolino_id`-Zuordnungen beim ersten Vergleich migriert; der alte `/registerhw`-Fallback entfällt.
- **Hintergrund und Release sind stabiler.** Ein lokaler Metadaten-Snapshot läuft in einem eigenen Thread; defekte Einstellungen beenden den Keep-alive nicht mehr still, ignorierte Bücher zählen nicht als ungeplante Aktionen, und HTTP-Fehler werden geschlossen.
- **Der Release-Build bleibt dokumentiert und reproduzierbar.** Das ZIP enthält README und CHANGELOG; beim Entfernen von Docstrings bleibt die Textlänge erhalten. Die bewussten Kompatibilitätspfade für Config und IDs bleiben bestehen.

## 0.1.06

- **Lesestatus und Fortschritt sind kontobezogen.** `#tolino_read` und die neue Spalte `#tolino_progress` (0–100, optional mit Position) zeigen nur den Stand des aktiven Kontos; fehlende Werte bleiben leer. Unbekannte Cloud-Werte bleiben unverändert, ein erfolgreicher Abruf ohne Bookmark leert den Fortschritt.
- **Die Zuordnung wird vorsichtiger.** ISBNs werden aus tolino-Produktkennungen erkannt, unsichere Treffer nur konservativ zugeordnet; veraltete Cloud-IDs und deren alter Lesestand werden entfernt.
- **Der Prepare-Zustand überlebt Migrationen und parallele Kontowechsel.** Er wird vor der Migration gesichert und nur bei Änderung geschrieben; Token-Rotation und Keep-alive bleiben abgesichert.
- **Build und Randfälle sind gehärtet.** Der Release-Build entfernt Kommentare und Docstrings; Downloads, Browser-Anmeldung und Qt6-Sonderfälle wurden stabilisiert.
- **Das Vergleichsfenster zeichnet Auswahlkästchen nur noch einmal.** Der doppelte Indikator ist behoben.

## 0.1.05

- **Der Build entfernt Kommentare, aber keinen Code.** Docstrings, Kodierung und Zeilennummern bleiben erhalten.
- **Fehler werden sauber gemeldet.** Zustands-, Speicher- und Anmeldefehler sind sichtbar; beschädigte Zustandseinträge stoppen den Sync nicht.
- **Intern gestrafft.** Browser-/Token-Verwaltung, Qt6-Enums, Transport und Konto-Kontext wurden vereinfacht, ohne das Verhalten zu ändern.

## 0.1.04

- **`#tolino_read` ist nur noch informativ.** Lesestatusänderungen werden ausschliesslich im Vergleichsfenster ausgewählt und an das aktive tolino-Konto übertragen.
- **Der automatische bidirektionale Abgleich entfällt.** So werden unterschiedliche Calibre- und Cloud-Stände nicht unbemerkt übertragen; der Sync führt Metadaten- und Upload-Schritte getrennt aus.
- **Qt6-Probleme sind behoben.** Spaltenbreiten und Dialogcodes funktionieren unter Qt6; ein Offscreen-Test prüft den Vergleichsdialog mit echten Qt-Widgets.

## 0.1.03

- **Ignorierte Bücher bleiben lokal.** Die optionale Spalte `#tolino_ignore` verhindert Upload und Änderungen; die Bücher bleiben sichtbar und ihre Cloud-Kopie wird weiterhin zugeordnet. Solange die Spalte fehlt, weist der Vergleich darauf hin.
- **Jede Calibre-Spalte bekommt einen eigenen Anlegen-Knopf.** Er legt nur die fehlende Spalte an und lässt vorhandene unberührt; danach ist ein Calibre-Neustart nötig.
- **Zugangsdaten liegen nur im Konto.** Das Token-Feld zeigt keine gespeicherten Werte; ein leeres Feld lässt den Zugang unberührt, «Abmelden» löscht ihn.

## 0.1.02

- **Mehrere Konten teilen sich `#tolino_id`.** Die Zuordnung wird pro Konto gespeichert, ohne die anderen Konten zu überschreiben.
- **Browser- und Token-Verwaltung sind einfacher.** Rotierte Refresh-Tokens lassen sich weiterhin nicht wiederverwenden.
- **Cloud-Bestand, Sync-Planung und Datenbankzugriffe wurden gestrafft.** Der Serienabgleich bleibt erhalten; die Browser-Anmeldung wartet bis zum Timeout auf einen neuen Zugang.

## 0.1.01

- **`#tolino_id` wird bei einem einzelnen Konto wieder zuverlässig gefüllt.**
- **Downloads laufen im Hintergrund.** EPUB und PDF werden passend gespeichert.
- **Weiterleitungen geben keine Download-Zugangsdaten preis.** Refresh-Timeouts lösen Einmal-Tokens nicht erneut ein.
- **Der Abschlussbericht zählt nur neu geschriebene Sammlungen.**

## 0.1.00

- **Erste Version für den Abgleich zwischen Calibre und tolino.** Bücher und Metadaten werden übertragen, Lesestatus und Serien-Sammlungen synchronisiert sowie Cloud-Bücher heruntergeladen.
- **Änderungen werden vor dem Start ausgewählt.** Cloud-Löschungen und das Ersetzen vorhandener Kopien werden ausdrücklich angezeigt und bestätigt.
- **Konten bleiben getrennt.** Refresh-Tokens sind gegen parallele Verwendung geschützt; die Anmeldung läuft über den tolino Web Reader im eigenen Browserprofil.
- **Unsaubere Fälle werden abgefangen.** Fehlende Upload-Formate, unsichere Cloud-Zuordnungen und unvollständige Bestände führen nicht zu stillem Datenverlust.
- **Installation und Build sind reproduzierbar.** Transport, Fehlercodes, Datenschutz- und Installationshinweise sind dokumentiert.

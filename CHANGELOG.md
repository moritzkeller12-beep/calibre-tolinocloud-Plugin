# Änderungen

## Unveröffentlicht

- Neue optionale Calibre-Spalte `#tolino_ignore` (Ja/Nein): so markierte Bücher lädt der Abgleich nicht hoch und ändert sie nicht — gekaufte Bücher erzeugen damit keine zweite Kopie in der Cloud. Sie bleiben im Vergleich sichtbar (Gruppe «Ignoriert») und ihre Cloud-Kopie wird weiterhin zugeordnet.
- Der Vergleich zeigt einen Hinweis, solange die Spalte `#tolino_ignore` fehlt.

## 0.1.02

- Browser- und Token-Verwaltung vereinfacht, ohne den Schutz gegen Wiederverwendung rotierter Refresh-Tokens aufzugeben.
- Cloud-Inventar, Sync-Planung und Calibre-Datenbankzugriffe auf die tatsächlichen Produktionspfade reduziert.
- `#tolino_id` unterstützt mehrere Konten durch kontoabhängige IDs in derselben Spalte.
- Serien-Sammlungsabgleich bleibt Teil des Syncs; Browser-Login wartet bis zum Timeout auf einen neuen Zugang.

## 0.1.01

- Die `#tolino_id`-Spalte wird bei einem einzelnen Konto wieder gelesen und gefüllt.
- Buchdownloads laufen im Hintergrund; EPUB und PDF werden passend gespeichert.
- Der Abschlussbericht zählt nur neu geschriebene Sammlungen.
- Die GitHub-Tests starten wieder und bauen das Plugin vorab.
- Download-Zugangsdaten werden bei Weiterleitungen nicht weitergegeben; Refresh-Timeouts lösen Einmal-Tokens nicht erneut ein.

## 0.1.00

- Calibre-Bibliotheken mit tolino abgleichen: Bücher und Metadaten übertragen,
  Lesestatus und Serien-Sammlungen synchronisieren sowie Cloud-Bücher herunterladen.
- Änderungen vor dem Start auswählen; Cloud-Löschungen und das Ersetzen einer
  vorhandenen Kopie werden ausdrücklich angezeigt und bestätigt.
- Konten getrennt verwalten, Refresh-Tokens gegen parallele Verwendung schützen
  und die Anmeldung über den tolino Web Reader im eigenen Browserprofil führen.
- Fehlende Upload-Formate, unsichere Cloud-Zuordnungen und unvollständige Bestände
  ohne stillen Datenverlust behandeln.
- Transport, Fehlercodes, Datenschutz- und Installationshinweise sowie den
  reproduzierbaren Plugin-Build an den Neustart angepasst.

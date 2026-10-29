# Änderungen

## Unveröffentlicht

- Der Bibliotheksvergleich bricht unter Calibre mit Qt 6 nicht mehr mit «QHeaderView has no attribute 'Stretch'» (E99) ab: Qt-Enums werden in der vollständigen Form (`QHeaderView.ResizeMode.Stretch` usw.) angesprochen, die Qt 5 und Qt 6 verstehen. Ein Test verhindert neue Kurzformen.

## 0.1.03

- Neue optionale Calibre-Spalte `#tolino_ignore` (Ja/Nein): so markierte Bücher lädt der Abgleich nicht hoch und ändert sie nicht — gekaufte Bücher erzeugen damit keine zweite Kopie in der Cloud. Sie bleiben im Vergleich sichtbar (Gruppe «Ignoriert») und ihre Cloud-Kopie wird weiterhin zugeordnet.
- Der Vergleich zeigt einen Hinweis, solange die Spalte `#tolino_ignore` fehlt.
- Die Einstellungen haben je Calibre-Spalte einen Knopf («#tolino_id anlegen …», «#tolino_read anlegen …», «#tolino_ignore anlegen …»): jeder legt nur seine Spalte an, falls sie fehlt, und lässt vorhandene unberührt. Calibre muss danach neu gestartet werden.
- Lesestatus wird mit der Calibre-Spalte `#tolino_read` (Ja/Nein) in beide Richtungen abgeglichen; widersprüchliche Stände werden im Vergleich zur Entscheidung angezeigt.
- In Calibre gelesene Bücher werden nach dem Upload in tolino als gelesen markiert; beim Ersetzen einer Cloud-Kopie geht die Gelesen-Markierung auf die neue Kopie über.
- Zugangsdaten liegen nur noch im Konto: das Token-Feld zeigt den Token nicht mehr an, ein leeres Feld lässt den gespeicherten Zugang unberührt, und «Abmelden» löscht ihn. Entfernt wurden die nie einstellbare Option `use_tolino_column`, tote Voreinstellungsfelder und zwei ungenutzte Vergleichsfelder; Tests nach der Vereinfachung des Dashboards repariert, ein reihenfolgeabhängiger Test löst keine echte Anmeldung mehr aus.

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

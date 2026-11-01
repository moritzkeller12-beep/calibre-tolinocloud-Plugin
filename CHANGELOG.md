# Änderungen

Aktuelle Version: **0.1.06** (Quelle: [`__init__.py`](__init__.py)).

## 0.1.06

- Neue informative Calibre-Spalte `#tolino_progress` (Anzeigename „tolino gelesen %“, Zahl 0–100): Sie zeigt den Lesefortschritt aus den `bookmark`-Patches der tolino-Cloud, im Vergleichsfenster zusätzlich mit Position (z. B. `33 % · 192/590`). Der Wert wird nie nach tolino zurückgeschrieben; bei unbekanntem Cloud-Stand bleibt die Spalte unverändert.
- Der Bestandsvergleich liest Lesestatus und Fortschritt aus demselben `sync-data`-Snapshot. Ein fehlender Bookmark bedeutet «kein Fortschritt», nicht «0 Prozent»; ein fehlgeschlagener Abruf wird als «unbekannt» angezeigt.
- Für den Abgleich extrahiert das Plugin eine gültige ISBN aus tolino-Produktkennungen wie `DT0400.9783832170141_A38978050`. Die Kennung bleibt weiterhin keine Deliverable-ID für Download oder Löschen.
- Nicht bestätigte Zuordnungen (widersprechende ISBN oder Autor) werden nicht mehr im Zustand gespeichert; Serien folgen damit nicht automatisch dem falschen Cloud-Buch.
- Der Titel+Autor-Index prüft den Autorennamen zusätzlich ohne ae/oe/ue-Ersatz («Michael» ≠ «Michal»).
- Download-Callbacks sind GUI-geschützt, Cloud-Antworten werden vor dem Speichern als EPUB/PDF geprüft, und EPUB/PDF erhalten feste MIME-Typen.
- Ein später Refresh-Token-Callback legt kein entferntes Konto neu an; Relogin wartet länger auf den Keep-alive-Lock. WebSocket-Nachrichten sind auf 16 MiB begrenzt, der IndexedDB-Scan liest per Cursor statt `getAll()`.
- Der Release-Build entfernt jetzt zusätzlich Docstrings aus den Plugin-Modulen; Code, Kodierung und Zeilennummern bleiben erhalten. Das verkleinert das ZIP, ohne das Laufzeitverhalten zu ändern.

## 0.1.05

- Der Plugin-Build entfernt Kommentare aus den Modulen, behält aber Code, Docstrings, Kodierung und Zeilennummern; `__init__.py` bleibt unverändert.
- Beschädigte Zustandseinträge zählen wie fehlende: Vergleich und Planung brechen nicht ab, und ein bestätigter Upload wird wieder gespeichert.
- Bei Speicherfehlern nach Cloud-Änderungen stoppt der Sync und meldet den bestätigten Teilstand; fehlgeschlagene abschließende Calibre-Schreibvorgänge werden angezeigt.
- Ein nicht gespeicherter rotierter Zugang beendet die Anmeldung mit bereinigter Fehlermeldung. CI prüft auch Pushes auf Versionsbranches.
- Browser-Anmeldung und Token-Verarbeitung vereinfacht: abgelehnte Refresh-Tokens werden entfernt, unerwartete Fehler sichtbar gemeldet und Windows-Browser über die Registry gefunden.
- Der WebSocket-Transport beantwortet Ping-Nachrichten; Dashboard, Inventarverarbeitung und Calibre-Zugriffe sind gestrafft. Bestehende Zustandsmigrationen und Löschbestätigungen bleiben erhalten.
- Doppelprüfungen in Tests zusammengeführt und zusätzliche Fehlerfälle für Anmeldung, Token-Dekodierung und Geräte-Registrierung abgesichert.
- Qt6-Enums werden durchgängig in der skopierten Schreibweise gesetzt (`Qt.SortOrder.AscendingOrder`, `QTableWidget.SelectionBehavior.SelectRows`, `QLineEdit.EchoMode.Password`); die Test-Stubs kennen keine unscharfen Aliase mehr, die solche Fehler verdeckt hätten.
- Konto und Optionen eines Laufs stehen in einem expliziten `RunContext`; Metadaten und Sync-Zustand sind eigene Parameter. Die Planungsschicht ist Qt-frei und wird durch einen eigenen Test davor bewahrt, `qt.core` zu laden.
- Nach jedem Upload wird der vollständige Konto-Zustand sofort gespeichert; ein separates Zustandsjournal entfällt. Das verhindert doppelte Uploads nach einem Abbruch mit einer einzigen Persistenzschicht.
- Der `SyncWorker` bündelt Abbruchprüfung, Fortschritt, Fehlerzählung und Erfolg in `_step()` und berechnet seine Zähler am Ende einmal aus allen Operationen (`_refresh_counts`), statt sie an jeder Stelle mitzuführen.
- Bulk-«Upload aktivieren» respektiert das bevorzugte Format; die Web-Reader-Erkennung verlangt mindestens 80 Zeichen, die Autoren-Normalisierung trennt «Joe» und «Jo», und die Inventar-Erkennung nutzt kein generisches `id` als Löschkennung.
- Fehlercodes sind auf acht zusammengeführt; UUID-Ersatzformen und die manuelle Zugangseingabe wurden gestrafft, während `calibre-<Nr>` als Zustandsschlüssel erhalten bleibt.
- Buchdownloads laden die Cloud-Kopie über eine base64-kodierte Kennung; die rohe Kennung beantwortete der Server mit 404.
- Behebt den Absturz beim Schließen des Vergleichsfensters unter Calibre 9: ein Item-Delegate ersetzt das ProxyStyle.
- Die Tolino-Action hängt am dokumentierten `shutting_down()`-Rückruf, zusätzlich an `aboutToQuit`; der Token-Keep-alive endet damit beim Beenden wirklich.

## 0.1.04

- `#tolino_read` ist nur noch eine informative Calibre-Spalte; Lesestatusänderungen werden ausschließlich im Vergleichsfenster ausgewählt und an das aktuell gewählte Tolino-Konto übertragen.
- Entfernt wurde der automatische bidirektionale Lesestatus-Abgleich, damit unterschiedliche Calibre- und Cloud-Stände nicht unbemerkt übertragen werden.
- Der Vergleichsdialog und die Synchronisierung wurden vereinfacht; der Worker führt Metadaten- und Upload-Schritte jetzt in getrennten Phasen aus.
- CI ergänzt einen PyQt6-Offscreen-Smoke-Test für den Vergleichsdialog mit echten Qt-Widgets.
- Behebt «QHeaderView has no attribute Stretch» beim Öffnen des Vergleichsfensters unter Calibre 9: Spaltenbreiten, Meldungsfenster und Dialogcodes werden in der Qt6-Schreibweise gesetzt und funktionieren weiterhin mit Qt5.
- Token-Verwaltung gestrafft: der Vault meldet sich einmal unter seinem Lock an, der Client schützt nur seinen eigenen Login; `_request` ist in Aufbau, Fehlerbehandlung und Wiederholung getrennt.

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

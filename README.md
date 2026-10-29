# tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.1.05-blue)](__init__.py)

Synchronisiert Bücher, Metadaten und Lesestatus zwischen Calibre und der tolino Cloud.

## Installation

1. [Plugin-ZIP herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 5 oder neuer. Für automatische Anmeldung wird Chrome, Chromium, Brave oder Edge benötigt.

## Benutzung

Plugin öffnen, Buchhändler wählen und **Bei tolino anmelden**. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

## Calibre-Spalten

Die Spalten lassen sich einzeln unter **Einstellungen …** des Plugins anlegen; Calibre danach neu starten.

- `#tolino_id` (Text): speichert die tolino-Zuordnung in der Bibliothek.
- `#tolino_read` (Ja/Nein): zeigt den Calibre-Lesestatus nur informativ; Änderungen an tolino sind ausschließlich im Vergleichsfenster auswählbar.
- `#tolino_ignore` (Ja/Nein): bei «Ja» wird ein Buch vom Abgleich ausgeschlossen, etwa wenn es bereits gekauft in tolino liegt.

## Hinweise

Das Plugin nutzt eine inoffizielle tolino-Schnittstelle; deren Verfügbarkeit kann sich ändern. Zugangsdaten werden kontobezogen gespeichert. Fehlermeldungen enthalten einen Code mit Abhilfe in der [Fehlercodeliste](docs/fehlercodes.md).

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

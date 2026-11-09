# tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.1.07-blue)](__init__.py)

Aktuelle Version: **0.1.07** (Quelle: [`__init__.py`](__init__.py)).

Synchronisiert Bücher, Metadaten, Lesestatus und Fortschritt zwischen Calibre und der tolino Cloud; mehrere Konten bleiben getrennt.

## Installation

1. [Plugin-ZIP für 0.1.07 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.07/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Die automatische Anmeldung benötigt Chrome, Chromium, Brave oder Edge; verschlüsselte Web-Reader-Tokens zusätzlich PyCryptodome.

## Benutzung

Plugin öffnen, Buchhändler wählen und **Bei tolino anmelden**. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**. Ein laufender Vergleich oder Sync bleibt beim Startkonto.

## Konten und Spalten

Jedes Konto hat eigene Lesewerte. Beim Wechsel ordnet das Plugin die Werte über die Calibre-UUID zu; ohne gültige UUID über Bibliothekskennung und Buchnummer. So bleiben Konten getrennt und der vorherige Stand kehrt beim Zurückwechseln zurück.

Die Spalten lassen sich einzeln unter **Einstellungen …** anlegen; Calibre danach neu starten.

- `#tolino_id` (Text): kontobezogene tolino-Zuordnung.
- `#tolino_read` (Ja/Nein): bestätigter Lesestatus des aktiven Kontos, nur zur Anzeige.
- `#tolino_progress` (Zahl 0–100): Lesefortschritt; unbekannte Abrufe lassen sie unverändert, ein erfolgreicher Abruf ohne Bookmark leert sie. Kein Rückschreiben nach tolino.
- `#tolino_ignore` (Ja/Nein): schließt ein Buch vom Abgleich aus.

Nach einem erfolgreichen Vergleich und bestätigten Änderungen werden die Lesewerte im aktiven Konto gespeichert. Für den Abgleich liest das Plugin ISBNs auch aus tolino-Produktkennungen; die Produktkennung ist keine Deliverable-ID.

## Entwicklung

Tests: `python3 -X dev -m unittest calibre_plugin.test_sync`. Release-ZIP: `python3 build_plugin.py`.

## Hinweise

- Das Plugin nutzt eine inoffizielle tolino-Schnittstelle. Sie kann sich jederzeit ändern.
- Zugangsdaten und Lesewerte liegen pro Konto in den Calibre-Einstellungen; der Refresh-Token steht dort im Klartext in der JSON-Datei.
- Alte Zustandseinträge ohne Bibliothekszuordnung (`calibre-<id>`) werden nicht übernommen.
- Fehlermeldungen enthalten einen Code; die Abhilfe steht in der [Fehlercodeliste](docs/fehlercodes.md).

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

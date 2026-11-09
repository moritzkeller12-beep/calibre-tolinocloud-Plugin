# tolino Cloud Sync für Calibre

Synchronisiert Bücher, Metadaten, Lesestatus und Fortschritt zwischen Calibre und der tolino Cloud.

Aktuelle Version: **0.1.07** (Quelle: [`__init__.py`](__init__.py)).

## Installation

1. [Plugin-ZIP für 0.1.07 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.07/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Die automatische Anmeldung benötigt Chrome, Chromium, Brave oder Edge; verschlüsselte Web-Reader-Tokens zusätzlich PyCryptodome.

## Benutzung

Plugin öffnen, Buchhändler wählen und **Bei tolino anmelden**. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

Fehlermeldungen enthalten einen Code; die Abhilfe steht in der [Fehlercodeliste](docs/fehlercodes.md).

## Entwicklung

Tests: `python3 -X dev -m unittest calibre_plugin.test_sync`

Release-ZIP: `python3 build_plugin.py`

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

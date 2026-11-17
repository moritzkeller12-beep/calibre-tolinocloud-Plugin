# tolino Cloud Sync für Calibre

Synchronisiert Bücher, Metadaten, Lesestatus und Fortschritt zwischen Calibre und der tolino Cloud.

Aktuelle Version: **0.1.08** (Quelle: [`__init__.py`](__init__.py)).

0.1.08 ist der neue State-Release: frühere Spalten- und `dbpath`-Migrationen werden nicht mehr ausgeführt. Der bisherige Config-Speicher bleibt unverändert aktiv; bestehende Konten und Tokens bleiben nach dem Upgrade erhalten.

## Installation

1. [Plugin-ZIP für 0.1.08 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.08/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Die automatische Anmeldung benötigt Chrome, Chromium, Brave oder Edge; verschlüsselte Web-Reader-Tokens zusätzlich PyCryptodome.

## Benutzung

Plugin öffnen und **Bei tolino anmelden** – den Buchhändler erkennt das
Plugin am Zugang. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

Fehlermeldungen enthalten einen Code; die Abhilfe steht in der [Fehlercodeliste](docs/fehlercodes.md).

## Entwicklung

Tests: `python3 -X dev -m unittest calibre_plugin.test_sync`

Die Dialogtests laufen mit echtem PyQt6 im Offscreen-Modus
(`QT_QPA_PLATFORM=offscreen`); ohne PyQt6 werden sie übersprungen.
Testabhängigkeiten: `pycryptodome`, `PyQt6`.

Live-Tests gegen die echte Cloud liegen nur lokal: `calibre_plugin/tests/live_tests.py`
wird nicht im Repo geführt und ist von Git ignoriert. Datei lokal anlegen,
`refresh_token` und `hardware_id` in der Umgebung setzen und `python3 -m
unittest calibre_plugin.tests.live_tests` ausführen. Ohne diese Werte werden
die Tests übersprungen. Der Refresh-Token ist einmalig – nach einem Live-Lauf ist er
verbraucht und muss neu hinterlegt werden; das Plugin selbst speichert den
rotierten Token beim Anmelden im Konto.

Release-ZIP: `python3 build_plugin.py`

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

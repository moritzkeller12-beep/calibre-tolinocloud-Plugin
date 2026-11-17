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
(`QT_QPA_PLATFORM=offscreen`); ohne PyQt6 werden sie übersprungen. Echte
Qt-Threads laufen nicht in der Suite: Widgets und Tabellen sind echt, der
QThread und die modalen Dialoge bleiben kontrollierte Doppel.
Testabhängigkeiten: `pycryptodome`, `PyQt6`.

Die Suite ist bewusst klein und prüft Nutzerabläufe statt Interna:
Konten, Token-Rotation und externe Änderungen an der Einstellungsdatei
(`config_tests`), Calibre-Spalten und Bibliothekszuordnung
(`library_tests`), Auswahlplanung mit echten Job-Pfaden,
Bestandsnormalisierung und Vergleichslogik (`planning_tests`), die
Vorbereitung des Vergleichs samt optionalen Lesewerten (`prepare_tests`),
den tolino-Client gegen einen lokalen HTTP-Server – Anmeldung,
Token-Rotation und alle Schreibpfade wie Upload, Metadaten,
Gelesen-Marken, Sammlungen, Löschen und Download (`tolino_client_tests`) –,
die sync-data-Patches für Lesestatus und Tags (`sync_data_tests`), den
Sync-Worker inklusive Löschen, Ersetzen und sofort gespeichertem
Kontostand (`workers_tests`), die Dashboard-Aktionen
(`dashboard_tests`), die Thread-Referenzen der Oberfläche
(`ui_thread_tests`) und das Vergleichsfenster mit echter Qt-Tabelle samt
Löschmarkierung (`ui_tests`). Dazu kommen Browser-Erkennung
(`browser_finder_tests`), der CDP-Lebenszyklus ohne echten Browser
(`cdp_tests`), die Token-Auswahl der Browser-Anmeldung
(`browser_login_tests`), die Entschlüsselung echter CryptoJS-Blöcke mit
festen, unabhängig erzeugten Fixtures (`browser_crypto_tests`) sowie der
gemeinsame Token-Vault mit Rotation, Keep-alive und Relogin
(`token_vault_tests`).

Live-Tests gegen die echte Cloud liegen nur lokal: `calibre_plugin/tests/live_tests.py`
wird nicht im Repo geführt und ist von Git ignoriert. Datei lokal anlegen,
`refresh_token` und `hardware_id` in der Umgebung setzen und `python3 -m
unittest calibre_plugin.tests.live_tests` ausführen. Ohne diese Werte werden
die Tests übersprungen. Der Refresh-Token ist einmalig – nach einem Live-Lauf ist er
verbraucht und muss neu hinterlegt werden; das Plugin selbst speichert den
rotierten Token beim Anmelden im Konto.

Release-ZIP: `python3 build_plugin.py`

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

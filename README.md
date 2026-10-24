# tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.1.01-blue)](__init__.py)

Gleicht die eigene Calibre-Bibliothek mit der **tolino Cloud** ab: Bücher hoch-
und herunterladen, gelesen markieren, Titelbilder mitnehmen.

## Installation

1. [**tolino_cloud_sync.zip** herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip)
2. Calibre → **Einstellungen → Plugins → Plugin aus Datei laden** → Zip auswählen
3. Calibre neu starten

Voraussetzung: **Calibre 5.0** oder neuer; für die automatische Anmeldung
zusätzlich **Chrome, Chromium, Brave oder Edge**. Für den Upload muss mindestens
eines der in den Einstellungen gewählten Formate (EPUB oder PDF) vorhanden sein.

## Benutzung

1. **tolino Cloud Sync** öffnen, Buchhändler wählen, **«Bei tolino anmelden»**
   und im Anmeldefenster einloggen. Danach **«Bibliothek vergleichen …»**.
2. Im Bibliotheksabgleich die gewünschten Aktionen wählen und
   **«Synchronisierung starten»**. Einstellungen und Konten findest du unter
   **«Einstellungen …»**.

Cover und Calibre-Serien werden standardmässig mit übertragen; Serien werden
automatisch als Sammlungen in tolino angelegt. Einzelne Cloud-Bücher lassen
sich im Vergleich gezielt zum Löschen markieren. Eine Cloud-Kopie wird erst
nach der Bestätigung vor dem Start gelöscht.

## Hinweise und Datenschutz

- Lege in Calibre unter **Einstellungen → Spalten hinzufügen** eine
  benutzerdefinierte Spalte **`#tolino_id`** vom Typ **Text** an. Ohne diese
  Spalte bleibt die Zuordnung im Plugin-Zustand gespeichert und wird nicht in
  der Bibliotheksspalte angezeigt.
- Das Plugin verwendet eine **inoffizielle Schnittstelle**. Es gibt keine
  Stabilitäts- oder Verfügbarkeitsgarantie; Endpunkte und Verhalten können
  sich ohne Vorwarnung ändern.
- Für die Anmeldung startet das Plugin ein separates Browserprofil. Es liest
  den Zugang aus dem Speicher des tolino Web Readers über einen lokalen
  Debug-Port. Nach dem Schliessen werden das Anmeldefenster und das temporäre
  Profil entfernt.
- Wenn eine geänderte Datei eine vorhandene Cloud-Kopie ersetzt, wird die
  alte Kopie gelöscht. Ihr Lesestatus, Lesefortschritt und ihre Sammlungen
  gehen dabei verloren; der Bestätigungsdialog weist darauf hin.
- Mehrere tolino-Konten werden getrennt verwaltet. Geräte-ID und Zugangstoken
  können unter **Einstellungen → Konten und Verbindung** eingesehen oder bei
  Bedarf geändert werden.

Jede Fehlermeldung endet mit einem Code, z. B. «(Code A02)»; alle Codes samt
Abhilfe: [`docs/fehlercodes.md`](docs/fehlercodes.md).

## Änderungen

Die Änderungen je Version stehen im [CHANGELOG](CHANGELOG.md).

## Entwicklung

```bash
python3 -m unittest calibre_plugin.test_sync   # alle Tests
python3 -m unittest calibre_plugin.tests.workers_tests   # eine Gruppe
python3 -m pyflakes calibre_plugin/ __init__.py build_plugin.py
python3 build_plugin.py                    # schreibt tolino_cloud_sync.zip
python3 build_plugin.py out.zip            # optionaler Zielpfad
```

Die Tests liegen thematisch in [`calibre_plugin/tests/`](calibre_plugin/tests/)
(Planung, Client, Browser, Dialoge, Dashboard, Worker, Kennungen);
`test_sync.py` sammelt sie per `load_tests` ein.

## Lizenz

GPL-3.0-or-later – vollständiger Text in [`LICENSE`](LICENSE).

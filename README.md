# tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.1.06-blue)](__init__.py)

Aktuelle Version: **0.1.06** (Quelle: [`__init__.py`](__init__.py)).

Synchronisiert Bücher, Metadaten und Lesestatus zwischen Calibre und der tolino Cloud, zeigt den Lesefortschritt aus tolino in Calibre an und hält mehrere Konten getrennt.

## Installation

1. [Plugin-ZIP für 0.1.06 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.06/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Für automatische Anmeldung wird Chrome, Chromium, Brave oder Edge benötigt.

## Benutzung

Plugin öffnen, Buchhändler wählen und **Bei tolino anmelden**. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

## Mehrere Konten

Jedes Konto speichert seinen Lesestatus und Lesefortschritt getrennt im vorhandenen Konto-Zustand. Die Spalten `#tolino_read` und `#tolino_progress` zeigen immer den Stand des aktuell ausgewählten Kontos. Beim Konto-Wechsel werden die gespeicherten Werte anhand der Calibre-Buch-UUID in die Spalten übernommen; beim Zurückwechseln erscheinen wieder die Werte des vorherigen Kontos.

Das funktioniert auch, wenn während eines Vergleichs oder einer Synchronisierung das Konto gewechselt wird: Ein Lauf bleibt seinem Startkonto zugeordnet und überschreibt die Anzeige eines anderen Kontos nicht. Eine laufende Anmeldeprüfung blockiert den Konto-Wechsel dagegen weiterhin.

## Calibre-Spalten

Die Spalten lassen sich einzeln unter **Einstellungen …** des Plugins anlegen; Calibre danach neu starten.

- `#tolino_id` (Text): speichert die tolino-Zuordnung kontobezogen in der Bibliothek.
- `#tolino_read` (Ja/Nein): zeigt den bestätigten Lesestatus des aktuell ausgewählten Kontos nur informativ; Änderungen an tolino sind ausschließlich im Vergleichsfenster auswählbar. Lesewerte werden nach einem erfolgreichen Vergleich sowie nach bestätigten manuellen Änderungen im jeweiligen Konto gespeichert.
- `#tolino_progress` (Anzeigename „tolino gelesen %“, Zahl 0–100): zeigt den Lesefortschritt des aktuell ausgewählten Kontos, im Vergleichsfenster zusätzlich mit Position (z. B. `33 % · 192/590`). Der Wert wird nie nach tolino zurückgeschrieben. Ein unbekannter Abruf lässt die Spalte unverändert; ein erfolgreicher Abruf ohne Bookmark leert sie ausdrücklich (kein `0 %`). Bei einer bereits angelegten Spalte bleibt der bisherige Anzeigename erhalten und lässt sich in Calibre über den Spaltenkopf ändern.
- `#tolino_ignore` (Ja/Nein): bei «Ja» wird ein Buch vom Abgleich ausgeschlossen, etwa wenn es bereits gekauft in tolino liegt.

Für den Abgleich liest das Plugin eine ISBN auch aus tolino-Produktkennungen wie `DT0400.9783832170141_…`; die Kennung selbst bleibt weiterhin keine Deliverable-ID.

## Hinweise

Das Plugin nutzt eine inoffizielle tolino-Schnittstelle; deren Verfügbarkeit kann sich ändern. Zugangsdaten und Lesewerte werden kontobezogen in Calibres Einstellungen gespeichert; der Refresh-Token liegt dort im Klartext in der JSON-Datei. Alte gemeinsame Werte der Lese-Spalten werden keinem Konto automatisch zugeschrieben; beim ersten erfolgreichen Vergleich je Konto werden die kontobezogenen Werte neu ermittelt. Fehlermeldungen enthalten einen Code mit Abhilfe in der [Fehlercodeliste](docs/fehlercodes.md).

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

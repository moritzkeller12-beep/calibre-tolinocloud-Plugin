# tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.1.07-blue)](__init__.py)

Aktuelle Version: **0.1.07** (Quelle: [`__init__.py`](__init__.py)).

Synchronisiert Bücher, Metadaten und Lesestatus zwischen Calibre und der tolino Cloud, zeigt den Lesefortschritt aus tolino in Calibre an und hält mehrere Konten getrennt.

## Installation

1. [Plugin-ZIP für 0.1.07 herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/0.1.07/tolino_cloud_sync.zip).
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** und ZIP auswählen.
3. Calibre neu starten.

Voraussetzung: Calibre 6 oder neuer. Für die automatische Anmeldung wird Chrome, Chromium, Brave oder Edge benötigt; verschlüsselte Web-Reader-Tokens benötigen zusätzlich PyCryptodome.

## Benutzung

Plugin öffnen, Buchhändler wählen und **Bei tolino anmelden**. Danach **Bibliothek vergleichen …** wählen, gewünschte Aktionen markieren und **Synchronisierung starten**.

## Mehrere Konten

Jedes Konto hat eigene Lesewerte. Die Spalten `#tolino_read` und `#tolino_progress` zeigen immer den Stand des aktiven Kontos. Beim Wechsel ordnet das Plugin die Werte zuerst über die Calibre-UUID zu. Fehlt eine echte UUID, nutzt es einen bibliotheksbezogenen Fallback aus Bibliothekskennung und Buchnummer. So bleiben die Werte auch dann erhalten, wenn das UUID-Feld keinen gültigen Wert enthält, und zwei Bibliotheken mit derselben Buchnummer überschreiben sich nicht. Beim Zurückwechseln erscheinen wieder die Werte des vorherigen Kontos.

Ein laufender Vergleich oder Sync bleibt beim Startkonto und überschreibt die Anzeige eines anderen Kontos nicht. Während einer Anmeldeprüfung ist der Konto-Wechsel gesperrt.

## Calibre-Spalten

Die Spalten lassen sich einzeln unter **Einstellungen …** des Plugins anlegen; Calibre danach neu starten.

- `#tolino_id` (Text): speichert die tolino-Zuordnung kontobezogen in der Bibliothek.
- `#tolino_read` (Ja/Nein): zeigt den bestätigten Lesestatus des aktiven Kontos. Die Spalte ist nur zur Anzeige; Änderungen werden im Vergleichsfenster ausgewählt.
- `#tolino_progress` (Anzeigename „tolino gelesen %“, Zahl 0–100): zeigt den Lesefortschritt des aktiven Kontos, im Vergleichsfenster zusätzlich mit Position (z. B. `33 % · 192/590`). Der Wert wird nie nach tolino zurückgeschrieben. Ist der Abruf unbekannt, bleibt die Spalte unverändert. Ein erfolgreicher Abruf ohne Bookmark leert sie ausdrücklich (kein `0 %`). Bei einer bereits angelegten Spalte bleibt der bisherige Anzeigename erhalten und lässt sich in Calibre über den Spaltenkopf ändern.
- `#tolino_ignore` (Ja/Nein): bei «Ja» wird ein Buch vom Abgleich ausgeschlossen, etwa wenn es bereits gekauft in tolino liegt.

Nach einem erfolgreichen Vergleich und nach bestätigten manuellen Änderungen speichert das Plugin die Lesewerte im jeweiligen Konto.

Für den Abgleich liest das Plugin die ISBN auch aus tolino-Produktkennungen. Die Produktkennung selbst ist keine Deliverable-ID.

## Hinweise

- Das Plugin nutzt eine inoffizielle tolino-Schnittstelle. Sie kann sich jederzeit ändern.
- Zugangsdaten und Lesewerte werden pro Konto in den Calibre-Einstellungen gespeichert. Der Refresh-Token steht dort im Klartext in der JSON-Datei.
- Ältere Zustandseinträge ohne Bibliothekszuordnung (`calibre-<id>`) werden nicht übernommen. Neue Fallback-Schlüssel tragen immer eine Bibliothekszuordnung.
- Kontobezogene `#tolino_id`-Zuordnungen werden beim ersten Vergleich einmalig in den State übernommen; danach ist der State maßgeblich.
- Fehlermeldungen enthalten einen Code; die Abhilfe steht in der [Fehlercodeliste](docs/fehlercodes.md).

Lizenz: [GPL-3.0-or-later](LICENSE). Änderungen: [CHANGELOG](CHANGELOG.md).

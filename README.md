# Tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.13.0-blue)](__init__.py)
[![Calibre](https://img.shields.io/badge/Calibre-%E2%89%A5%205.0-informational)](https://calibre-ebook.com/)
[![Plattformen](https://img.shields.io/badge/Plattformen-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey)](#voraussetzungen)
[![Lizenz](https://img.shields.io/badge/Lizenz-GPL--3.0--or--later-blue)](LICENSE)

Calibre-Plugin, das die eigene Bibliothek mit der Tolino Cloud abgleicht:
Bücher hoch- und herunterladen, **Serien** in der Cloud anlegen, Titelbilder
mitnehmen und gelesen markieren. Läuft unter Windows, Linux und macOS – auch
mit Calibre als Flatpak.

## Inhalt

- [Funktionen](#funktionen)
- [Installation](#installation)
- [Benutzung](#benutzung)
- [Was automatisch passiert](#was-automatisch-passiert)
- [Anmeldung von Hand (ohne Chromium)](#anmeldung-von-hand-ohne-chromium)
- [Problembehandlung](#problembehandlung)
- [Entwicklung](#entwicklung)
- [Mitwirken](#mitwirken)
- [Lizenz](#lizenz)

## Funktionen

| Funktion | Beschreibung |
|---|---|
| **Bestandsvergleich** | Ein Fenster zeigt, welche Bücher nur in Calibre, nur in der Cloud oder in beiden liegen (gleich bzw. geändert). Pro Buch wird ausgewählt, was passieren soll: **Hochladen** (Buchdatei) und/oder **Metadaten** (nur Titel/Autor(en)/ISBN in der Cloud). Sortierbar per Spaltenkopf. |
| **Serien in der Cloud** | Jede Calibre-Serie wird der Tolino Cloud zugeordnet; der Tolino Web Reader zeigt diese Zuordnung als „Sammlung“ an. |
| **Titelbilder** | Das Titelbild eines Buches kann mit hochgeladen werden. |
| **Herunterladen** | Cloud-Bücher als EPUB direkt auf den Rechner holen. |
| **Metadaten** | Geänderte Titel/Autor(en)/ISBN lassen sich ohne erneuten Upload in der Cloud aktualisieren – pro Buch im Bestandsvergleich auswählen. |
| **Mehrere Konten** | Getrennte Zugangsdaten und Zuordnungen pro Buchhändler-Konto. |

## Installation

### Voraussetzungen

- **Calibre 5.0** oder neuer
- Für die automatische Anmeldung zusätzlich ein **Chromium-Browser**:
  Chrome, Chromium, Brave oder Edge (der Standardbrowser genügt nicht)

### Plugin installieren

1. **Plugin herunterladen:**
   [**tolino_cloud_sync.zip** herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip)
   *(oder im Repository die Datei `tolino_cloud_sync.zip` anklicken → „Download“)*
2. In Calibre öffnen: **Einstellungen → Plugins → Plugin aus Datei laden**
   und die heruntergeladene `tolino_cloud_sync.zip` auswählen.
3. **Calibre neu starten.** In der Symbolleiste erscheint dann
   **Tolino Cloud Sync**.

> [!NOTE]
> Es ist kein Bauen nötig – die Zip im Repository ist direkt installierbar.
> Wer den Quellcode geändert hat, baut sie unter [Entwicklung](#entwicklung) neu.

## Benutzung

### 1. Anmelden

Symbolleiste **Tolino Cloud Sync** öffnen → Buchhändler wählen →
**„Jetzt bei Tolino anmelden“**. Im Anmeldefenster im Web Reader einloggen
und die Bücherliste öffnen – den Zugang holt das Plugin automatisch und
schließt das Fenster wieder.

### 2. Synchronisieren

**„Vergleichen und hochladen …“** drücken. Das Plugin meldet sich an, liest
die Cloud und öffnet den Bestandsvergleich.

### 3. Bestandsvergleich

Die Tabelle zeigt je Buch zwei Aktionen zum Auswählen und zwei Spalten mit
einem ✓, wo das Buch liegt (Stand vor dem Lauf):

| Spalte | Bedeutung |
|---|---|
| **Hochladen** | Buchdatei in die Tolino Cloud hochladen (ersetzt dort die vorhandene Kopie). |
| **Metadaten** | Nur Titel/Autor(en)/ISBN in der Cloud abgleichen – die Buchdatei bleibt, wie sie ist. Nur möglich, wenn das Buch schon in der Cloud liegt. |
| **In Calibre** ✓ | Das Buch liegt in Ihrer Calibre-Bibliothek. |
| **In der Cloud** ✓ | Das Buch liegt in der Tolino Cloud. |

Darunter stehen Titel, Autor(en), der **Status** (z. B. „Nur in Calibre“,
„In beiden, geändert“) und ein Hinweis in Alltagssprache. Keine Auswahl → das
Buch bleibt unangetastet. `✓` in beiden Spalten und ein Status „In beiden,
gleich“ heißt: nichts zu tun.

- **„Empfehlung auswählen“** wählt für alle Bücher die passende Aktion aus
  (neu → Hochladen, nur geänderte Metadaten → Metadaten), **„Auswahl
  aufheben“** hebt die gesamte Auswahl auf. Knopf und Zeile darüber zeigen laufend, wie
  viele Aktionen ausgewählt sind.
- **Spaltenkopf anklicken** sortiert; ausgewählte Bücher stehen oben.
- **„Technische Spalten anzeigen“** blendet ISBN und Tolino-ID ein.
- **„Weitere Aktionen anzeigen“** klappt die Cloud-Einzelaktionen aus:
  herunterladen, einer Sammlung zuordnen, gelesen markieren.
- **„Auswahl übernehmen“** führt genau die ausgewählten Aktionen aus.

Alle weiteren Einstellungen (Formate, Titelbilder, mehrere Konten,
Zugangsdaten) liegen hinter **„Erweiterte Einstellungen anzeigen“** und sind
standardmäßig richtig eingestellt.

## Was automatisch passiert

**Hochladen und Metadaten sind keine Automatik mehr:** Beide werden pro Buch
im Bestandsvergleich ausgewählt. Nur wer dort nichts auswählt, ändert auch nichts.

Zusätzlich zu den ausgewählten Aktionen läuft beim Klick auf **„Auswahl
übernehmen“** noch folgendes mit. Jeder Punkt steht auch im Bestandsvergleich
über der Liste und lässt sich unter **Erweiterte Einstellungen** abschalten:

| Automatisch | Standard | Bedeutung |
|---|---|---|
| **Serien in der Cloud anlegen** | an | Jedes Buch einer Calibre-Serie wird der Cloud zugeordnet (im Web Reader als „Sammlung“ sichtbar). Vorhandene Zuordnungen werden nicht doppelt geschrieben. |
| **Löschungen übernehmen** | aus | Bücher, die aus Calibre entfernt wurden, verschwinden auch aus der Cloud. |

## Anmeldung von Hand (ohne Chromium)

Steht kein Chromium-Browser zur Verfügung, öffnet „Jetzt bei Tolino anmelden“
kein Fenster und sagt das direkt. Der Zugang lässt sich dann von Hand
eintragen – das geht immer:

1. Im Web Reader anmelden und die Bücherliste öffnen.
2. **F12 → Netzwerk → Aufnahme starten**, dann die Seite neu laden.
3. Im Netzwerk-Protokoll `hardware_id` (Filter `registerhw`) und
   `refresh_token` (Filter `token`) kopieren.
4. Unter **Erweiterte Einstellungen** in „Hardware-ID“ und
   „Zugangsschlüssel (Refresh-Token)“ einfügen.

> [!WARNING]
> Ein Tolino-Token ist nach einmaliger Nutzung ungültig. Kein
> Inkognito-Fenster verwenden und die Kopie nicht parallel in einem anderen
> Browserfenster weiterverwenden.

## Problembehandlung

| Meldung | Tun |
|---|---|
| Flatpak: kein Browser / „Freigabe erteilen“ | `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre` (System-Installation: dasselbe mit `sudo` ohne `--user`) — danach Calibre **vollständig** neu starten. Nachweis: `flatpak info --show-permissions com.calibre_ebook.calibre` muss `org.freedesktop.Flatpak` zeigen. |
| „Fenster ist noch offen“ / verbrauchter Token | Im offenen Anmeldefenster **dort** neu anmelden (Bücherliste laden) und den Knopf erneut drücken. Ist das Fenster zu, die Anmeldung neu starten und im **neuen** Fenster anmelden. |
| HTTP 403 „Zugriff geblockt“ | Bot-Schutz: das Plugin wartet und wiederholt automatisch; bleibt die 403, später erneut versuchen. |
| „Kein Chromium-Browser gefunden“ | Browser installieren **oder** den Zugang [von Hand eintragen](#anmeldung-von-hand-ohne-chromium). |

Genauere Diagnose: Knopf **„Diagnose“** im Dashboard (führt ohne Anmeldung
keine Tolino-Anfrage aus). Hintergrund der Anmeldung:
[`docs/browser-login-diagnose.md`](docs/browser-login-diagnose.md).

## Entwicklung

```bash
python3 -m pip install pycryptodome        # nur für die AES-Tests nötig
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py                    # schreibt tolino_cloud_sync.zip
```

Die Zip im Repository muss immer zur Version in `__init__.py`
(`PLUGIN_VERSION`) passen – `build_plugin.py` erzeugt genau das von Calibre
erwartete Layout. Der Testlauf läuft zusätzlich in GitHub Actions
(`.github/workflows/ci.yml`) bei jedem Push und Pull Request.

| Datei | Inhalt |
|---|---|
| `__init__.py` | Plugin-Metadaten und Version |
| `calibre_plugin/ui.py` | Dashboard, Bestandsvergleich, Synchronisierung |
| `calibre_plugin/sync.py` | Bestandsvergleich, Upload-/Metadaten-Planung |
| `calibre_plugin/tolino.py` | Tolino-API, Anmeldung, Tokens |
| `calibre_plugin/cdp.py` | Chromium-Anmeldefenster (DevTools-Protokoll) |
| `calibre_plugin/config.py` | Einstellungen und Konten |

## Mitwirken

Issues und Pull Requests sind willkommen. Bitte vor dem PR
`python3 -m unittest calibre_plugin.test_sync` laufen lassen und die Zip neu
bauen, wenn Quellcode geändert wurde.

## Lizenz

Copyright (C) 2026 moritzkeller12-beep

Dieses Programm ist freie Software: Sie können es unter den Bedingungen der
**GNU General Public License, Version 3 oder später** (GPL-3.0-or-later)
weitergeben und/oder verändern, wie sie von der Free Software Foundation
veröffentlicht wurde. Es wird **ohne jegliche Gewährleistung** bereitgestellt.

Der vollständige Lizenztext liegt in [`LICENSE`](LICENSE); Kurzfassung:
<https://www.gnu.org/licenses/gpl-3.0.txt>. Die Lizenz ist mit Calibre
vereinbar, das selbst unter GPL-3 steht.

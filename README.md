# Tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.15.12-blue)](__init__.py)
[![Lizenz](https://img.shields.io/badge/Lizenz-GPL--3.0--or--later-blue)](LICENSE)

Gleicht die eigene Calibre-Bibliothek mit der **Tolino Cloud** ab: Bücher hoch-
und herunterladen, gelesen markieren, Titelbilder mitnehmen.

## Installation

1. [**tolino_cloud_sync.zip** herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip)
2. Calibre → **Einstellungen → Plugins → Plugin aus Datei laden** → Zip auswählen
3. Calibre neu starten

Voraussetzung: **Calibre 5.0** oder neuer; für die automatische Anmeldung
zusätzlich **Chrome, Chromium, Brave oder Edge**.

## Benutzung

1. **Tolino Cloud Sync** öffnen, Buchhändler wählen, **«Jetzt bei Tolino
   anmelden»** → im Anmeldefenster im Web Reader einloggen. Die Anmelde-Schaltfläche
   bleibt immer sichtbar (auch zum Neuanmelden), darunter wird
   **«Vergleichen und übernehmen …»** aktiv. Bei mehreren Konten steht die
   Kontoauswahl direkt daneben. Die Schaltfläche prüft **zuerst den gespeicherten
   Zugang**: funktioniert er noch, wird er still erneuert und es öffnet
   **kein** Anmeldefenster – das kommt nur, wenn Tolino den Zugang ablehnt.
2. Im **Bestandsvergleich** je Buch auswählen:

| Spalte | Bedeutung |
|---|---|
| **Hochladen** | Buchdatei in die Cloud laden (ersetzt die Kopie dort). |
| **Metadaten** | Nur Titel/Autor(en)/ISBN abgleichen, kein Upload. |
| **Gelesen** | Im Tolino-Konto als gelesen markieren – oder wieder auf ungelesen setzen: bei einem schon gelesenen Buch löst der erste Klick die Sperre, der zweite setzt es wieder auf ungelesen. |
| **In Calibre** / **In der Cloud** | Ein ✓ zeigt, wo das Buch liegt. |

**«Empfehlung auswählen»** wählt Hochladen/Metadaten für die passenden Bücher
aus, **«Auswahl übernehmen»** führt genau diese Aktionen aus – ohne Auswahl
passiert nichts. Für viele Bücher auf einmal Zeilen markieren
(Strg-/Shift-Klick, Strg+A) und **«Für markierte Zeilen …»** benutzen: dort
lassen sich Hochladen, Metadaten und «Gelesen» setzen oder zurücknehmen,
Bücher herunterladen und Sammlungen pflegen. Alles Seltene steht unter
**«Einstellungen …»**.

> Standard: **nur EPUB** hochladen, Serien **nicht** in der Cloud anlegen,
> Löschungen **aus**.

## Anmeldung von Hand (ohne Chromium)

Im Web Reader anmelden, Bücherliste öffnen, **F12 → Netzwerk**, Seite neu laden,
dann `hardware_id` (Filter `registerhw`) und `refresh_token` (Filter `token`)
unter **«Einstellungen …» → Konten** eintragen.

> Ein Tolino-Token gilt nur einmal – kein Inkognito-Fenster verwenden und die
> Kopie nicht parallel in einem anderen Browserfenster nutzen.

## Problembehandlung

- **Flatpak, kein Browser:** `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre`, danach Calibre neu starten.
- **«Kein Chromium-Browser gefunden»:** Browser installieren oder den Zugang von Hand eintragen.
- **Verbrauchter Zugang:** im Anmeldefenster neu anmelden und erneut auf «Jetzt bei Tolino anmelden» klicken.
- **HTTP 403:** Bot-Schutz – später erneut versuchen.
- **Sonst:** **«Diagnose»** in den Einstellungen sammelt Details ohne Tolino-Anfrage.

## Fehlercodes

Jede Fehlermeldung endet mit einem Code, z. B. «(Code A02)»; die technischen
Details stehen im Calibre-Log (**Hilfe → Im Debug-Modus starten**) und in der
**Diagnose** unter «Letzte Fehler». Alle Codes samt Abhilfe:
[`docs/fehlercodes.md`](docs/fehlercodes.md).

## Entwicklung

```bash
python3 -m pip install pycryptodome        # nur für die AES-Tests
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py                    # schreibt tolino_cloud_sync.zip
```

## Lizenz

GPL-3.0-or-later, Copyright (C) 2026 moritzkeller12-beep – vollständiger Text in
[`LICENSE`](LICENSE).

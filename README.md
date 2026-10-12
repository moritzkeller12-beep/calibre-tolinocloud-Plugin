# Tolino Cloud Sync für Calibre

[![Tests](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml/badge.svg)](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/actions/workflows/ci.yml)
[![Version](https://img.shields.io/badge/Version-0.15.1-blue)](__init__.py)
[![Lizenz](https://img.shields.io/badge/Lizenz-GPL--3.0--or--later-blue)](LICENSE)

Calibre-Plugin, das die eigene Bibliothek mit der **Tolino Cloud** abgleicht:
Bücher hoch- und herunterladen, Serien in der Cloud anlegen, Titelbilder
mitnehmen, gelesen markieren. Windows, Linux, macOS – auch als Flatpak.

## Installation

Voraussetzung: **Calibre 5.0** oder neuer; für die automatische Anmeldung
zusätzlich **Chrome, Chromium, Brave oder Edge** (der Standardbrowser genügt
nicht).

1. [**tolino_cloud_sync.zip** herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip)
2. In Calibre: **Einstellungen → Plugins → Plugin aus Datei laden** → die Zip
   auswählen.
3. **Calibre neu starten.** In der Symbolleiste steht dann **Tolino Cloud Sync**.

> [!NOTE]
> Die Zip im Repository ist direkt installierbar – Bauen ist nicht nötig.

## Benutzung

1. **Tolino Cloud Sync** öffnen → Buchhändler wählen → **„Jetzt bei Tolino
   anmelden“**. Im Anmeldefenster im Web Reader einloggen und die Bücherliste
   öffnen – den Zugang holt das Plugin automatisch.
2. **„Vergleichen und übernehmen …“** drücken.
3. Im **Bestandsvergleich** je Buch auswählen, was passieren soll:

| Spalte | Bedeutung |
|---|---|
| **Hochladen** | Buchdatei in die Cloud hochladen (ersetzt die Kopie dort). |
| **Metadaten** | Nur Titel/Autor(en)/ISBN in der Cloud abgleichen, kein Upload. |
| **Gelesen** | Buch im Tolino-Konto als gelesen markieren (nur Cloud, die Buchdatei bleibt unverändert). Ist das Buch dort schon gelesen, ist die Spalte angehakt und gesperrt. |
| **In Calibre** ✓ / **In der Cloud** ✓ | Wo das Buch liegt. |

Ohne Auswahl passiert nichts – der **Lese-Status in der Cloud bleibt, wie er
ist**, bis Sie die Spalte „Gelesen“ anhaken. **„Empfehlung auswählen“** setzt
für jedes Buch Hochladen und/oder Metadaten (die Lese-Marke nie), **„Auswahl
übernehmen“** führt genau die ausgewählten Aktionen aus.

**Viele Bücher auf einmal als gelesen markieren:** in der Tabelle mehrere Zeilen
markieren (Strg- oder Shift-Klick, alle mit **Strg+A**) und **„Gelesen für
markierte Zeilen“** drücken – angehakt wird nur, was in der Cloud noch nicht
gelesen ist. Sortieren per Spaltenkopf; ISBN und Tolino-ID über **„Technische
Spalten anzeigen“**, Herunterladen und Sammlungen über **„Weitere Aktionen
anzeigen“**. Alles Übrige liegt unter **„Erweiterte Einstellungen anzeigen“**.

> [!NOTE]
> Zusätzlich zu den ausgewählten Aktionen werden **Serien** in der Cloud
> angelegt (abschaltbar). Einmal zugeordnete Serien werden gemerkt und nicht
> bei jedem Sync neu geschrieben; Löschungen sind standardmäßig aus.

## Anmeldung von Hand (ohne Chromium)

Im Web Reader anmelden, Bücherliste öffnen, **F12 → Netzwerk**, Seite neu laden,
dann `hardware_id` (Filter `registerhw`) und `refresh_token` (Filter `token`)
kopieren und unter **Erweiterte Einstellungen** eintragen.

> [!WARNING]
> Ein Tolino-Token ist nach einmaliger Nutzung ungültig – kein Inkognito-Fenster
> verwenden und die Kopie nicht parallel in einem anderen Browserfenster nutzen.

## Problembehandlung

- **Flatpak, kein Browser:** `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre` – danach Calibre vollständig neu starten.
- **„Kein Chromium-Browser gefunden“:** Browser installieren oder den Zugang von Hand eintragen (siehe oben).
- **Verbrauchter Token / „Fenster ist noch offen“:** im offenen Anmeldefenster die Bücherliste neu laden und den Knopf erneut drücken.
- **HTTP 403:** Bot-Schutz – das Plugin wartet und wiederholt automatisch, sonst später erneut versuchen.

Der Knopf **„Diagnose“** sammelt Details ohne Tolino-Anfrage; im Bericht prüft
**„Tolino-Antwort testen“** die Anmeldung und zeigt, wie viele Sammlungen und
Gelesen-Marken die Cloud meldet. Hintergrund:
[`docs/browser-login-diagnose.md`](docs/browser-login-diagnose.md).

## Entwicklung

```bash
python3 -m pip install pycryptodome        # nur für die AES-Tests nötig
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py                    # schreibt tolino_cloud_sync.zip
```

`PLUGIN_VERSION` in `__init__.py` und die Zip müssen zusammenpassen; die Tests
laufen in GitHub Actions (`.github/workflows/ci.yml`). Issues und Pull Requests
sind willkommen.

## Lizenz

GPL-3.0-or-later, Copyright (C) 2026 moritzkeller12-beep – vollständiger Text in
[`LICENSE`](LICENSE). Ohne Gewährleistung.

# Tolino Cloud Sync für Calibre

**0.9.45** — Upload, Download, Sammlungen und Gelesen-Markierung zwischen Calibre und der Tolino Cloud.
**Neu:** Calibre-Serien werden automatisch als Sammlungen in der Cloud angelegt; reine
Metadatenänderungen (Titel/Autoren/ISBN) werden in-place aktualisiert statt das Buch neu
hochzuladen (nur wenn die Datei unverändert ist).
**Läuft auf:** Windows 11 · natives Linux · Calibre als Flatpak — alle getestet.

## Installation

```text
python3 build_plugin.py
```

`tolino_cloud_sync.zip` in Calibre laden (**Einstellungen > Plugins > Plugin aus Datei
laden**), Calibre neu starten. Danach im Dialog Buchhändler, Refresh-Token und Formate
prüfen → **Vergleichen und hochladen**.

## Refresh-Token

**Empfohlen: „Im Browser anmelden (frischen Token holen)“** — das eigene Chromium-Fenster
öffnet den Web Reader, dort bis zur **Bücherliste** anmelden; das Plugin liest den Token
live und schließt das Fenster selbst.
**Ohne Chromium:** Knopf drücken → Web Reader im System-Browser öffnen → **alle
Browserfenster schließen** → Knopf erneut. **Manuell:** F12 → Netzwerk → Aufnahme an →
im Web Reader anmelden → `hardware_id` (Filter `registerhw`) und `refresh_token`
(Filter `token`) kopieren.
Ein Tolino-Token ist nach einmaliger Nutzung ungültig (`invalid_grant`), kein
Inkognito-Fenster.

## Die häufigsten Probleme

| Meldung | Tun |
|---|---|
| Flatpak: kein Browser / „Freigabe erteilen“ | `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre` (System-Installation: dasselbe mit `sudo` ohne `--user`) — danach Calibre **vollständig** neu starten. Nachweis: `flatpak info --show-permissions com.calibre_ebook.calibre` muss `org.freedesktop.Flatpak` zeigen. |
| „Fenster ist noch offen“ / verbrauchter Token | Fenster zeigt die Anmeldeseite: **dort** neu anmelden (Bücherliste laden), Knopf erneut. Fenster zu: Browser-Anmeldung neu starten und im **neuen** Fenster anmelden. |
| HTTP 403 „Zugriff geblockt“ | Bot-Schutz: das Plugin wartet und wiederholt automatisch; bleibt die 403, später erneut versuchen. |

## Validierung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```

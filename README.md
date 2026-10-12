# Tolino Cloud Sync für Calibre

**0.10.0** — Bücher zwischen Calibre und der Tolino Cloud abgleichen:
hochladen, herunterladen, Sammlungen und Gelesen-Markierung.
**Läuft auf:** Windows 11 · natives Linux · Calibre als Flatpak — alle getestet.

## In drei Schritten loslegen

1. **Installieren:** `python3 build_plugin.py` ausführen, dann
   `tolino_cloud_sync.zip` in Calibre laden
   (**Einstellungen → Plugins → Plugin aus Datei laden**) und Calibre neu starten.
2. **Anmelden:** In der Symbolleiste **Tolino Cloud Sync** öffnen und
   **„Jetzt bei Tolino anmelden“** drücken. Im Anmeldefenster im Web Reader
   einloggen und die Bücherliste öffnen — das Fenster schließt sich selbst.
3. **Synchronisieren:** **„Vergleichen und hochladen …“** drücken, im
   Bestandsvergleich die gewünschten Bücher anhaken und **„Auswahl hochladen“**.

Weitere Einstellungen (Formate, mehrere Konten, Zugangsdaten) sind hinter
**„Erweiterte Einstellungen anzeigen“** versteckt und müssen nicht angepasst
werden.

## Anmeldung ohne den Automatik-Knopf

- **Empfohlen:** den Knopf **„Jetzt bei Tolino anmelden“** nutzen.
- **Ohne Chromium:** Knopf drücken, im System-Browser im Web Reader anmelden,
  **alle Browserfenster schließen** und den Knopf erneut drücken.
- **Manuell (nur wenn nötig):** in den erweiterten Einstellungen Hardware-ID
  und Refresh-Token eintragen. F12 → Netzwerk → Aufnahme an → im Web Reader
  anmelden → `hardware_id` (Filter `registerhw`) und `refresh_token`
  (Filter `token`) kopieren. Ein Tolino-Token ist nach einmaliger Nutzung
  ungültig — kein Inkognito-Fenster verwenden.

## Häufige Probleme

| Meldung | Tun |
|---|---|
| Flatpak: kein Browser / „Freigabe erteilen“ | `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre` (System-Installation: dasselbe mit `sudo` ohne `--user`) — danach Calibre **vollständig** neu starten. Nachweis: `flatpak info --show-permissions com.calibre_ebook.calibre` muss `org.freedesktop.Flatpak` zeigen. |
| „Fenster ist noch offen“ / verbrauchter Token | Im offenen Anmeldefenster **dort** neu anmelden (Bücherliste laden) und den Knopf erneut drücken. Ist das Fenster zu, die Anmeldung neu starten und im **neuen** Fenster anmelden. |
| HTTP 403 „Zugriff geblockt“ | Bot-Schutz: das Plugin wartet und wiederholt automatisch; bleibt die 403, später erneut versuchen. |

## Entwicklung

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```

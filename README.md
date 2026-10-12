# Tolino Cloud Sync für Calibre

Bücher zwischen Calibre und der Tolino Cloud abgleichen: hochladen,
herunterladen, Sammlungen anlegen und gelesen markieren.
Läuft unter Windows, Linux und mit Calibre als Flatpak.

## Installation (in 2 Minuten)

1. **Plugin herunterladen:**
   [**tolino_cloud_sync.zip** herunterladen](https://github.com/moritzkeller12-beep/calibre-tolinocloud-Plugin/raw/main/tolino_cloud_sync.zip)
   *(oder im Repo oben: die Datei `tolino_cloud_sync.zip` anklicken → „Download“)*
2. In Calibre öffnen: **Einstellungen → Plugins → Plugin aus Datei laden**
   und die heruntergeladene `tolino_cloud_sync.zip` auswählen.
3. **Calibre neu starten.** In der Symbolleiste erscheint dann
   **Tolino Cloud Sync**.

Es ist kein Bauen nötig – die Zip ist direkt installierbar.

## Benutzung

1. **Anmelden:** Symbolleiste **Tolino Cloud Sync** öffnen → Buchhändler
   wählen → **„Jetzt bei Tolino anmelden“**. Im Anmeldefenster im Web Reader
   einloggen und die Bücherliste öffnen; das Fenster schließt sich selbst.
2. **Synchronisieren:** **„Vergleichen und hochladen …“** drücken.
3. Im Bestandsvergleich die gewünschten Bücher anhaken → **„Auswahl
   hochladen“**.

Alle weiteren Einstellungen (Formate, mehrere Konten, Zugangsdaten) liegen
hinter **„Erweiterte Einstellungen anzeigen“** und sind standardmäßig richtig
eingestellt.

## Wenn die Anmeldung nicht automatisch klappt

- **Ohne Chromium installiert:** „Jetzt bei Tolino anmelden“ drücken, im
  System-Browser im Web Reader anmelden, **alle Browserfenster schließen**
  und den Knopf erneut drücken.
- **Nur im Notfall manuell:** In den erweiterten Einstellungen Hardware-ID und
  Refresh-Token von Hand eintragen. F12 → Netzwerk → Aufnahme an → im Web
  Reader anmelden → `hardware_id` (Filter `registerhw`) und `refresh_token`
  (Filter `token`) kopieren. Ein Tolino-Token ist nach einmaliger Nutzung
  ungültig – kein Inkognito-Fenster verwenden.

## Häufige Probleme

| Meldung | Tun |
|---|---|
| Flatpak: kein Browser / „Freigabe erteilen“ | `flatpak override --user --talk-name=org.freedesktop.Flatpak com.calibre_ebook.calibre` (System-Installation: dasselbe mit `sudo` ohne `--user`) — danach Calibre **vollständig** neu starten. Nachweis: `flatpak info --show-permissions com.calibre_ebook.calibre` muss `org.freedesktop.Flatpak` zeigen. |
| „Fenster ist noch offen“ / verbrauchter Token | Im offenen Anmeldefenster **dort** neu anmelden (Bücherliste laden) und den Knopf erneut drücken. Ist das Fenster zu, die Anmeldung neu starten und im **neuen** Fenster anmelden. |
| HTTP 403 „Zugriff geblockt“ | Bot-Schutz: das Plugin wartet und wiederholt automatisch; bleibt die 403, später erneut versuchen. |

## Für Entwickler

Nach Änderungen am Quellcode die Zip neu bauen – die im Repo abgelegte Zip
muss dazu mit der Version in `__init__.py` übereinstimmen:

```text
python3 -m unittest calibre_plugin.test_sync
python3 build_plugin.py
```

Der Testlauf läuft auch automatisch in GitHub Actions bei jedem Push/Pull
Request.

## Version

Aktuell: **0.10.0** — siehe `__init__.py`.

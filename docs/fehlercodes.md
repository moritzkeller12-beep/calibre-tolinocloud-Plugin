# Fehlercodes

Jede Fehlermeldung des Plugins endet mit einem Code, z. B. «(Code A02)».
Der Code steht im Dialog, die technischen Details stehen im Calibre-Log
(**Hilfe → Im Debug-Modus starten**) und in der **Diagnose** unter
«Letzte Fehler». Zugangsschlüssel (Refresh-Token) werden nie protokolliert.

| Code | Bedeutung | Abhilfe |
|------|-----------|---------|
| A01 | Tolino-Sitzung beendet | im Web Reader neu anmelden, dann «Jetzt bei Tolino anmelden» |
| A02 | Zugang abgelehnt (verbraucht oder widerrufen) | neu anmelden |
| A03 | noch nicht angemeldet | «Jetzt bei Tolino anmelden» |
| A04 | Anmeldung mit Passwort nicht unterstützt | anmelden oder Zugang von Hand eintragen |
| A05 | Buchhändler ohne automatische Anmeldung | Zugang von Hand eintragen |
| A06 | Anmeldung bei Tolino fehlgeschlagen | Details im Log, später erneut |
| A07 | ungültige Anmelde-Antwort | erneut versuchen |
| A08 | Anmeldung abgelaufen | erneut anmelden |
| B01 | Anmeldefenster offen, aber nicht angemeldet | im Fenster anmelden |
| B02 | kein gültiger Zugang im Anmeldefenster | neu anmelden (oft genügt F5) |
| B03 | Anmeldefenster zu früh geschlossen | erneut, Fenster offen lassen |
| B04 | Zeitüberschreitung beim Anmelden | erneut versuchen |
| B05 | kein Chromium-Browser gefunden | installieren oder Zugang von Hand eintragen |
| B06 | Flatpak-Freigabe fehlt | Befehl aus der Meldung ausführen |
| B07 | Chromium startet oder reagiert nicht | alle Chromium-Fenster schliessen |
| N01 | keine Verbindung zu Tolino | Netzwerk prüfen |
| N02 | ungültige Antwort von Tolino | später erneut |
| N03 | Tolino meldet einen Fehler (HTTP) | später erneut; 403 = Bot-Schutz |
| N04 | Upload nicht bestätigt | erneut hochladen |
| N05 | Download fehlgeschlagen | erneut versuchen |
| N06 | Daten von Tolino unvollständig | Vergleich neu laden |
| S90 | interner Fehler | Log an den Fehlerbericht anhängen |
| E99 | unerwarteter Fehler | Log (mit Traceback) an den Fehlerbericht anhängen |

Die Codes vergibt der Quelltext an genau einer Stelle je Meldung
(`code="A02"` usw.). Ein Test hält Tabelle und Quelltext zusammen: steht im
Code ein Code, der hier fehlt, schlägt die Suite fehl.

Hintergrund zur Anmeldung und zum Zugang: [browser-login-diagnose.md](browser-login-diagnose.md).

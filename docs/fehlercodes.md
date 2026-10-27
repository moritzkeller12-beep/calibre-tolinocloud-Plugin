# Fehlercodes

Jede Fehlermeldung des Plugins endet mit einem Code, z. B. «(Code A02)».
Der Code steht im Dialog; technische Details stehen im Calibre-Log.
Das Plugin protokolliert weder Zugangstoken noch andere Anmeldedaten.

| Code | Bedeutung | Abhilfe |
|------|-----------|---------|
| A01 | tolino-Sitzung beendet | im Web Reader neu anmelden, dann «Bei tolino anmelden» |
| A02 | Zugangstoken verbraucht oder widerrufen | neu bei tolino anmelden |
| A03 | noch nicht angemeldet | «Bei tolino anmelden» |
| A06 | Anmeldung bei tolino fehlgeschlagen | Details im Log, später erneut |
| A07 | ungültige Anmelde-Antwort | erneut versuchen |
| B01 | Anmeldefenster offen, aber nicht angemeldet | im Fenster anmelden |
| B02 | kein gültiger Zugang im Anmeldefenster | neu anmelden (oft genügt F5) |
| B03 | Anmeldefenster zu früh geschlossen | erneut, Fenster offen lassen |
| B04 | Zeitüberschreitung beim Anmelden | erneut versuchen |
| B05 | kein unterstützter Browser gefunden | Chrome, Chromium, Brave oder Edge installieren oder Zugangstoken unter «Einstellungen …» → «Konten und Verbindung» eintragen |
| B06 | Flatpak-Hilfsprogramm fehlt | «flatpak update» ausführen und Calibre vollständig neu starten |
| B07 | Chromium startet oder reagiert nicht | alle Chromium-Fenster schließen |
| B08 | Flatpak-Freigabe für Host-Browser fehlt | den im Log gezeigten `flatpak override`-Befehl nur für Calibre ausführen und Calibre neu starten |
| N01 | Verbindung fehlgeschlagen oder Zeitüberschreitung | Netzwerk prüfen; bei einem Zeitlimit erneut anmelden, falls der Zugang abgelehnt wird |
| N02 | ungültige Antwort von tolino oder Bestand ohne vollständige Kennungen | später erneut versuchen |
| N03 | tolino meldet einen Fehler (HTTP) | später erneut; 403 = Bot-Schutz |
| N04 | Upload nicht bestätigt | erneut hochladen |
| N05 | Download fehlgeschlagen | erneut versuchen |
| N06 | Daten von tolino unvollständig | Vergleich neu laden |
| S90 | interner Fehler | Log an den Fehlerbericht anhängen |
| S91 | Zugang wird gerade erneuert (Refresh-Token in Benutzung) | kurz warten, dann erneut versuchen |
| E99 | unerwarteter Fehler | Log (mit Traceback) an den Fehlerbericht anhängen |

Die Codes vergibt der Quelltext an genau einer Stelle je Meldung
(`code="A02"` usw.). Ein Test hält Tabelle und Quelltext zusammen: steht im
Code ein Code, der hier fehlt, schlägt die Suite fehl.

Eine fehlgeschlagene Verbindung kann nach einem Timeout bereits den einmalig
nutzbaren Zugangstoken verbraucht haben. Wenn danach A02 erscheint, einmal neu bei
tolino anmelden. Bei B08 den im Dialog gezeigten Freigabebefehl nur für die
Calibre-Flatpak-Anwendung ausführen; er erweitert nicht die Freigaben des
Plugins selbst. Details und der vollständige Befehl stehen im Calibre-Log.

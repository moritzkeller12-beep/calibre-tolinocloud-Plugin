# Fehlercodes

Jede Fehlermeldung des Plugins endet mit einem Code, z. B. «(Code A02)».
Der Code steht im Dialog; technische Details stehen im Calibre-Log.
Das Plugin protokolliert weder Zugangstoken noch andere Anmeldedaten.

| Code | Bedeutung |
|------|-----------|
| A01 | tolino-Sitzung beendet |
| A02 | Zugangstoken verbraucht oder widerrufen |
| A03 | noch nicht angemeldet |
| A06 | Anmeldung bei tolino fehlgeschlagen |
| A07 | ungültige Anmelde-Antwort |
| B01 | Anmeldefenster offen, aber nicht angemeldet |
| B02 | kein gültiger Zugang im Anmeldefenster |
| B03 | Anmeldefenster zu früh geschlossen |
| B04 | Zeitüberschreitung beim Anmelden |
| B05 | kein unterstützter Browser gefunden |
| B06 | Flatpak-Hilfsprogramm fehlt |
| B07 | Chromium startet oder reagiert nicht |
| B08 | Flatpak-Freigabe für Host-Browser fehlt |
| N01 | Verbindung fehlgeschlagen oder Zeitüberschreitung |
| N02 | ungültige Antwort von tolino oder Bestand ohne vollständige Kennungen |
| N03 | tolino meldet einen Fehler (HTTP), u. a. 403 (Bot-Schutz) |
| N04 | Upload nicht bestätigt |
| N05 | Download fehlgeschlagen |
| N06 | Daten von tolino unvollständig |
| S90 | interner Fehler |
| S91 | Zugang wird gerade erneuert (Refresh-Token in Benutzung) |
| E99 | unerwarteter Fehler |

Die Codes vergibt der Quelltext an genau einer Stelle je Meldung
(`code="A02"` usw.). Ein Test hält Tabelle und Quelltext zusammen: steht im
Code ein Code, der hier fehlt, schlägt die Suite fehl. Den nächsten Schritt
zu jedem Anmeldecode nennt der Dialog selbst; er steht nur in
`ui_common._LOGIN_GUIDANCE`, damit derselbe Satz nicht zweimal gepflegt wird.

Eine fehlgeschlagene Verbindung kann nach einem Timeout bereits den einmalig
nutzbaren Zugangstoken verbraucht haben. Wenn danach A02 erscheint, einmal neu bei
tolino anmelden. Bei B08 den im Dialog gezeigten Freigabebefehl nur für die
Calibre-Flatpak-Anwendung ausführen; er erweitert nicht die Freigaben des
Plugins selbst. Details und der vollständige Befehl stehen im Calibre-Log.

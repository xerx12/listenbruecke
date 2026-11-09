# Lidl-Anmeldung für Listenbrücke

Für Chrome, Edge und andere Chromium-Browser. Der Helfer fängt die einmalige
Anmeldeweiterleitung der Monsieur-Cuisine-App ab. Er liest keine Passwörter oder
Cookies und überträgt die Anmeldedaten an keinen Server.

1. Diesen Ordner auf dem eigenen Rechner speichern.
2. `chrome://extensions` beziehungsweise `edge://extensions` öffnen.
3. Entwicklermodus aktivieren und **Entpackte Erweiterung laden** wählen.
4. Den Ordner `browser-helper` auswählen.
5. In Listenbrücke **Mit Lidl anmelden** starten und die Anmeldung einschließlich
   CAPTCHA direkt auf der Lidl-Seite abschließen.
6. Auf der geöffneten Helferseite **Anmeldedaten kopieren** wählen und die Daten
   in den Monsieur-Cuisine-Dialog der Listenbrücke einfügen.

Der Einmal-Code bleibt vorübergehend im Sitzungsspeicher der Erweiterung und
wird nach dem Kopieren gelöscht. Die Anzeige läuft nach fünf Minuten ab.
Keine Rückrufadresse oder Home-Assistant-Ingress-Sitzung wird an Lidl gesendet.
Firefox wird von diesem Manifest nicht unterstützt.

Die Zugangsdaten niemals in Chats oder Fehlerberichte kopieren. Falls die
Anmeldung abläuft, in Listenbrücke eine neue Anmeldung starten.

Adaptierter Code aus MrVain74/ha-monsieur-cuisine, MIT-Lizenz. Den vollständigen
Lizenztext enthält `../THIRD_PARTY_LICENSES.md`.

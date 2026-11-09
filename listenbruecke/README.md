# Listenbrücke

Einkaufslisten von Bring!, Monsieur Cuisine, Amazon Alexa und Home Assistant mit einer deutschen
Oberfläche zusammenführen. Verbindungstests, Listenauswahl, automatischer Abgleich
und Protokoll sind enthalten. Zugangsdaten werden im privaten Datenverzeichnis
verschlüsselt gespeichert.

**Version 0.3.0 ist experimentell.** Bei Monsieur-Cuisine-Rezeptzutaten ist nur der
Erledigt-Status schreibbar; Name, Menge und einzelne Löschungen bleiben geschützt.
Bring!-Listen, die bereits in Home Assistant erscheinen, werden nicht doppelt
eingebunden.

Alexa lässt sich direkt in dieser Oberfläche mit Amazon-E-Mail, Passwort und
aktuellem Authenticator-Code verbinden. Gespeichert wird nur die verschlüsselte
Sitzung. Beliebige zwei bis vier Anbieterlisten können gemeinsam abgeglichen werden.

Die optionale HACS-Integration aus demselben Repository ergänzt das Gerät
Listenbrücke mit gemeinsamer Einkaufsliste, Verbindungsstatus und Steuerung.
Mit dieser lokalen Liste genügt ein externer Anbieter. Die App enthält eigene
Grafiken für App-Store und Integration sowie ein Brückensymbol für die Seitenleiste.

Einrichtung und Betrieb: siehe [Dokumentation](DOCS.md). Die App ist inoffiziell und
steht in keiner Verbindung zu den Anbietern.

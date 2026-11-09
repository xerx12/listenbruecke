# Einrichtung

## Home Assistant

Die App installieren und starten, anschließend **Weboberfläche öffnen** wählen.
Die Anmeldung erfolgt über Home Assistant. Dort ist keine zusätzliche Eingabe des
Home-Assistant-Tokens nötig. Eine native, getrennte HA-Einkaufsliste ist optional.

## Eigenständiger Docker-Betrieb

Im Repository `docker compose up --build -d` ausführen. Die Oberfläche ist auf Port
8099 erreichbar. Beim ersten Öffnen ein eigenes Passwort mit mindestens zwölf
Zeichen wählen. Das Docker-Volume muss über Updates und Neustarts erhalten bleiben.

## Anbieter verbinden

Unter **Verbindungen** Bring!-E-Mail und Passwort eintragen, speichern und den
Verbindungstest ausführen. Für Monsieur Cuisine zuerst den Browserhelfer als ZIP
herunterladen, entpacken und in Chrome/Edge unter Erweiterungen den Entwicklermodus
einschalten. **Entpackte Erweiterung laden** wählen und den entpackten Ordner angeben.
Dann in Listenbrücke **Mit Lidl anmelden** wählen, die Anmeldung im eigenen Browser
abschließen und das kopierte JSON des Helfers in Listenbrücke einfügen. Dieser
Einmal-Code gehört nur in die eigene App-Oberfläche.

Im Docker-Betrieb kann zusätzlich eine Home-Assistant-URL mit Zugriffstoken verbunden
werden. Bei Verwendung einer gesonderten lokalen HA-Liste muss das Konto die
Entitätsregistrierung und die To-do-Liste lesen und bearbeiten dürfen.

### Amazon Alexa direkt verbinden

Im Amazon-Konto muss die Zwei-Schritt-Verifizierung mit einer Authenticator-App
aktiviert sein. Unter **Verbindungen → Amazon Alexa** die Amazon-E-Mail-Adresse,
das Passwort und den aktuellen sechsstelligen Code aus dieser App eingeben.
**Anmelden & verbinden** startet die Anmeldung; ihr Fortschritt bleibt sichtbar.
Ein SMS-Code wird von diesem Anmeldeweg nicht unterstützt.

Gespeichert wird ausschließlich die verschlüsselte Amazon-Sitzung. Passwort und
Einmalcode werden nicht gespeichert und nach dem Absenden aus dem Formular entfernt.
Bei einer abgelaufenen Sitzung die Anmeldung mit einem frischen Code wiederholen.
Eine Alexa-Integration in Home Assistant ist für diesen direkten Zugang nicht nötig.
Verlangt Amazon zusätzlich ein CAPTCHA, eine Passkey-Anmeldung oder eine andere
Sicherheitsbestätigung, kann dieser Anmeldeweg sie nicht beantworten. Offene
Sicherheitsabfragen im eigenen Amazon-Konto prüfen und die Anmeldung erneut versuchen.

Danach die Verbindung testen und unter **Listen & Abgleich** die gewünschte
Alexa-Einkaufsliste auswählen. Bring! und Alexa können auch ohne Monsieur Cuisine
abgeglichen werden. Eine über Home Assistant eingebundene Alexa-Liste darf nicht
gleichzeitig mit dem direkten Alexa-Zugang als zweite Quelle verwendet werden.

## Listen auswählen

Unter **Listen & Abgleich** mindestens zwei Anbieterlisten auswählen. Eine über
Bring! eingebundene HA-Liste wird nicht nochmals angeboten. Automatischen Abgleich
aktivieren und speichern. Das Intervall kann 30 bis 3600 Sekunden betragen. Je nach
Anbieter verzögern sich Anzeige und Bestätigung einer Änderung um dessen Abrufintervall.

Löschen wird erst nach Aktivierung der entsprechenden Einstellung übertragen.
Geschützte MC-Rezeptzutaten werden niemals durch Entfernen eines ganzen Rezepts
ersetzt. Konflikte bleiben im Protokoll sichtbar und können durch Angleichen der
betroffenen Angaben in den Hersteller-Apps aufgelöst werden.

## Backup, Fehler und Updates

Das gesamte App-Datenverzeichnis sichern, insbesondere `app.sqlite3` und
`encryption.key`. Ein fehlender Schlüssel wird nicht automatisch durch einen neuen
ersetzt. Bei Verbindungsfehlern zuerst den Test des betreffenden Anbieters ausführen;
bei abgelaufener MC-Anmeldung die Browseranmeldung erneut verwenden. Ein gescheiterter
Abruf löst keine Leerlisten- oder Massenlöschungsaktion aus.

Dieser Stand wurde mit simulierten Providerantworten geprüft. Ein vollständiger
Live-Abgleich und Containerstart auf der eigenen Home-Assistant-Installation sind
zusätzlich erforderlich. Der Quellcode kann unter MIT veröffentlicht werden;
Drittanbieter-Lizenzen in `THIRD_PARTY_LICENSES.md` bleiben beizulegen.

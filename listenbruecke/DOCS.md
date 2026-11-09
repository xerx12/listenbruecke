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

## Geräte, Entitäten und gemeinsame Einkaufsliste

Die native Integration benötigt Home Assistant ab **2026.10.4** und eine laufende
Listenbrücke-App ab **0.3.0**. Sie kann auch eine eigenständige Docker-Installation
verbinden, sofern Home Assistant deren Adresse erreichen kann.

### Integration installieren und verbinden

1. In HACS das Menü für benutzerdefinierte Repositories öffnen und
   `https://github.com/xerx12/listenbruecke` als Typ **Integration** hinzufügen.
2. Listenbrücke herunterladen. Bei experimentellen Vorabversionen gegebenenfalls
   die gewünschte Version im Download-Dialog auswählen. Home Assistant neu starten.
3. In der Listenbrücke-Oberfläche unter **Verbindungen → Home Assistant** den Bereich
   für Geräte und Entitäten öffnen. Einen Verbindungsschlüssel erstellen und die
   angezeigte App-Adresse sowie den Schlüssel kopieren.
4. In Home Assistant unter **Einstellungen → Geräte & Dienste → Integration hinzufügen**
   **Listenbrücke** wählen und diese beiden Angaben eintragen.

Manuelle Installation: Den Ordner `custom_components/listenbruecke` aus diesem
Repository nach `/config/custom_components/listenbruecke` kopieren und Home Assistant
neu starten. Alternativ enthält der GitHub-Release die ZIP-Datei der Integration.

Der Schlüssel wird einmalig angezeigt. In der App wird nur sein Hash gespeichert.
Beim Erstellen eines neuen Schlüssels muss die bestehende HA-Verbindung mit dem
neuen Schlüssel aktualisiert werden. Herstellerzugänge werden nicht erneut abgefragt.

### Verfügbare Entitäten

| Entität | Verwendung |
| --- | --- |
| Gemeinsame Einkaufsliste | Artikel hinzufügen, ändern, abhaken und entfernen |
| Anbieter verbunden | Status von Bring!, Monsieur Cuisine, Alexa und Home Assistant |
| Problem | Zeigt einen Fehler beim letzten Abgleich |
| Letzter erfolgreicher Abgleich | Zeitpunkt für Dashboard und Benachrichtigungen |
| Offene Artikel | Anzahl unerledigter Artikel der gemeinsamen Liste |
| Übertragene Änderungen | Bestätigte Ergänzungen und Aktualisierungen insgesamt |
| Automatischer Abgleich | Abgleich einschalten oder pausieren |
| Prüfintervall | Abstand zwischen 30 und 3600 Sekunden |
| Jetzt abgleichen | Einen manuellen Durchlauf starten |

Die Entity-IDs können in Home Assistant umbenannt werden. Die gemeinsame Liste
findet sich im Bereich **To-do-Listen** und lässt sich als **To-do-Liste** ins
Dashboard einfügen. Für Automationen steht die Standardaktion `todo.add_item`
zur Verfügung. Nach Freigabe der Liste für Assist ist auch Spracheingabe möglich.

Mit der gemeinsamen Liste genügt eine zusätzliche externe Anbieter-Liste. Die
eigenen Listenbrücke-Entitäten werden nicht als weitere HA-Quelle angeboten, damit
der Abgleich die gemeinsame Liste genau einmal verarbeitet.

Lokale Änderungen werden dauerhaft gespeichert, auch wenn der automatische Abgleich
pausiert ist. Das Löschen eines Eintrags entfernt ihn sofort aus der gemeinsamen
Liste. Auf die externen Listen wird die Löschung nur bei aktivierter Löschweitergabe
übertragen. Geschützte Monsieur-Cuisine-Rezeptzutaten bleiben geschützt.

### Apple-Erinnerungen auf dem iPhone

Die experimentelle Funktion **Reminders Sync** der iOS-Companion-App kann eine
Apple-Erinnerungsliste mit der gemeinsamen HA-To-do-Liste verbinden. Sie erfordert
auf dem iPhone Zugriff auf Erinnerungen; iOS entscheidet über Hintergrundläufe.
Dies ist eine Anbindung über das iPhone und keine zusätzliche Apple-Anmeldung in
Listenbrücke. Einrichtung: [Companion-Dokumentation](https://companion.home-assistant.io/docs/integrations/ios-reminders-sync/).

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

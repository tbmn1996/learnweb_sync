# LearnWeb Sync Exploration Paper

## Kurzfazit

Das aktuelle Skript funktioniert als kursweiser ZIP-Exporter, ist aber nicht die richtige Grundlage fuer das Ziel "nur neue oder geaenderte Dateien herunterladen". Fuer dieses Ziel muss das Projekt von einem Bulk-Export auf einen echten Ressourcensync mit persistentem Manifest umgestellt werden.

Die beste Zielarchitektur ist ein Hybrid-Ansatz:

- zuerst pruefen, welche LearnWeb-/Moodle-Schnittstellen auf dem konkreten Account verfuegbar sind
- wenn moeglich den offiziellen Mobile-/Webservice-Pfad nutzen
- sonst auf robustes Session-/HTML-Scraping zurueckfallen
- Deadlines getrennt davon ueber Kalenderexport/ICS nach Notion spiegeln
- den bisherigen Kurs-ZIP-Download nur als Backup-/Exportmodus behalten

Dieses Dokument ist bewusst eine Entscheidungsgrundlage und keine finale Implementierungsspezifikation.

---

## 1. Ausgangslage

Im aktuellen Projekt existiert im Wesentlichen nur ein lokaler Prototyp in `learnweb_download.py`.

Der Prototyp:

- loggt sich per Session-Login in LearnWeb ein
- liest das Dashboard unter `/my/index.php`
- findet Kurslinks
- ruft pro Kurs den `downloadcontent.php`-Flow auf
- speichert pro Kurs genau ein ZIP im lokalen Zielordner

Relevante Beobachtungen aus dem Ist-Zustand:

- Das Skript ist klein, direkt und aktuell funktional.
- Der erfolgreiche Lauf in `logs/20260312_204618.log` hat am 12.03.2026 insgesamt 17 Kurs-ZIPs geladen.
- Ein frueherer Lauf in `logs/20260312_204427.log` hat stattdessen HTML-Antworten bekommen und alles uebersprungen. Das zeigt, wie fragil der Flow gegen kleine Abweichungen in LearnWeb ist.
- Es gibt aktuell kein richtiges Repo-Setup mit `pyproject.toml`, `README`, `tests`, `src/`-Struktur oder persistentem State.
- Es gibt keine echte Delta-Logik. Jeder Lauf erzeugt neue ZIP-Dateien mit Zeitstempel.

### Warum das fachlich nicht reicht

Das Ziel ist nicht "regelmaessig Sicherheitskopien aller Kurse ziehen", sondern "nur neue oder geaenderte Inhalte herunterladen". Diese beiden Produkte sind nicht dasselbe:

- Ein Kurs-ZIP ist ein Bulk-Export.
- Ein inkrementeller Sync ist ein per Remote-Objekt nachvollziehbarer Abgleich.

Ein globaler Zeitstempel wie `last_download_at` reicht fuer Letzteres nicht aus. Korrekt ist nur ein per Datei bzw. Remote-Objekt gefuehrtes Manifest mit stabiler Identitaet und Fingerprint.

---

## 2. Zielbild

Das Zielprojekt soll:

- als sauberes Python-Repo aufgebaut sein
- manuell und automatisiert ausfuehrbar sein
- nur neue oder geaenderte LearnWeb-Inhalte herunterladen
- lokale Datenintegritaet hoch halten
- spaeter Deadlines, Benachrichtigungen oder andere LearnWeb-Signale in Notion spiegeln koennen

Das Zielbild ist damit zweiteilig:

1. **Datei-Sync**
   Nur neue oder geaenderte Materialien herunterladen, ohne bestaende stumpf neu zu exportieren.

2. **Informations-Sync**
   Strukturierte LearnWeb-Signale wie Deadlines, Foren oder Benachrichtigungen in ein externes System, vor allem Notion, ueberfuehren.

---

## 3. Bewertungskriterien

Jede Architekturvariante wird an denselben Kriterien gemessen:

- **Korrektheit des Delta-Syncs**
  Erkennt die Option neue und geaenderte Inhalte auf Objektebene oder nur grob?
- **Datenintegritaet**
  Ist klar, wann etwas "gleich", "geaendert" oder "verschwunden" ist?
- **Robustheit gegen UI-Aenderungen**
  Wie stark haengt die Loesung an HTML-Strukturen?
- **Abhaengigkeit von Adminrechten**
  Funktioniert die Loesung mit normalem Nutzerkonto?
- **Notion-Fit**
  Eignet sich die Quelle fuer spaetere strukturierte Synchronisation nach Notion?
- **Wartbarkeit**
  Wie gut laesst sich die Loesung testen, beobachten und weiterentwickeln?
- **Implementierungsaufwand**
  Wie viel Weg liegt zwischen aktuellem Prototyp und einer belastbaren V1?

---

## 4. Nicht verhandelbare Integritaetsregeln

Unabhaengig von der gewaelten Architektur sollte die spaetere Implementierung diese Regeln einhalten:

- Kein globaler Zeitstempel als einzige Wahrheitsquelle.
- Per-Objekt-Manifest mit stabiler Remote-ID oder bestmoeglichem Ersatz.
- Persistenter State in SQLite oder aehnlich robuster lokaler Datenbank.
- Downloads immer zuerst in eine Temp-Datei schreiben und erst danach atomisch verschieben.
- Alle Zeitwerte intern in UTC speichern.
- Parallele Laeufe mit Lock verhindern.
- Remote-Verschwinden nicht automatisch als lokalen Delete interpretieren.
- Manuelle Nutzerarbeit in Notion nie ungefragt durch Sync-Felder ueberschreiben.

Diese Regeln sind wichtig, weil sonst ein scheinbar "einfacher" Timestamp-Ansatz schnell zu stillen Fehlern fuehrt.

---

## 5. Option 1: Bestehenden ZIP-Export haerten

### Wie funktioniert es

Der aktuelle `downloadcontent.php`-Flow bleibt die Hauptlogik. Er wird nur um etwas State ergaenzt, z. B.:

- `last_download_at`
- pro Kurs letzter Exportzeitpunkt
- optional Hash oder Dateigroesse pro erzeugtem ZIP

### Vorteile

- kleinster Umbau vom aktuellen Skript aus
- schneller erster Schritt
- gut fuer grobe Backups oder Archivzwecke

### Nachteile

- fachlich ungeeignet fuer "nur neue Dateien"
- Aenderungserkennung nur auf Kurs-Gesamtebene, nicht auf Dateiebene
- jeder ZIP-Export ist erneut teuer
- Umbenennungen, kleine Updates oder partielle Aenderungen lassen sich nicht sauber einordnen

### Datenintegritaet

Schwach. Selbst mit Manifest bleibt unklar, welche einzelne Datei sich im Kurs geaendert hat. Man weiss nur, dass ein Export anders aussieht oder neuer ist.

### Notion-Eignung

Begrenzt. Ein Kurs-ZIP liefert keine gute strukturierte Basis fuer Deadlines, Foren oder Benachrichtigungen.

### Aufwand

Niedrig.

### Urteil

Nur als Backup-/Exportmodus sinnvoll. Nicht als Basis fuer das eigentliche Produkt.

---

## 6. Option 2: HTML-basierter Ressourcensync

### Wie funktioniert es

Das Tool nutzt weiterhin einen normalen LearnWeb-Login per Session, verlaesst aber den ZIP-Exportpfad. Stattdessen:

- Kurse vom Dashboard einsammeln
- Kursseiten und Materiallisten scrapen
- echte downloadbare Ressourcen und Ordner identifizieren
- pro Remote-Objekt ein Manifest fuehren
- nur neue oder geaenderte Ressourcen herunterladen

Typische Datenquellen waeren:

- Kursseite
- Abschnittslisten
- Ressourcen-Seiten
- Folder-Ressourcen
- Download-Links mit Session-Cookies

### Vorteile

- funktioniert realistisch auch ohne Adminrechte
- naehert sich dem eigentlichen Dateisync-Ziel direkt an
- besser an lokale Objekte koppelbar als der Kurs-ZIP-Export
- gute Baseline fuer spaetere Verfeinerung

### Nachteile

- abhaengig von HTML-Struktur und DOM-Merkmalen
- LearnWeb-Theme- oder Moodle-Updates koennen Parser brechen
- je nach Aktivitaetstyp muessen mehrere Seitentypen unterstuetzt werden

### Datenintegritaet

Gut, wenn ein sauberes Manifest vorhanden ist. Kritisch ist die Wahl der Remote-Identitaet:

- ideal: stabile Moodle-IDs wie `courseid`, `cmid`, `contextid`, Dateireferenz
- fallback: URL + Dateiname + Groesse + Remote-Zeitstempel
- letzter fallback: Inhalts-Hash nach Download

### Notion-Eignung

Mittel. Fuer Dateisync gut, fuer Deadlines und Benachrichtigungen eher zweitrangig. HTML-Scraping ist fuer strukturierte Notion-Objekte moeglich, aber nicht der sauberste erste Pfad.

### Aufwand

Mittel.

### Urteil

Die beste sichere V1-Baseline ohne Adminannahmen. Wenn gar kein offizieller API-Pfad offen ist, ist das der richtige Hauptweg.

---

## 7. Option 3: Offizieller Moodle-Mobile-/Webservice-Pfad

### Wie funktioniert es

Wenn LearnWeb den offiziellen Mobile-/Webservice-Zugang fuer normale Nutzerkonten zulaesst, kann das Projekt ueber dokumentierte Moodle-Funktionen arbeiten statt ueber HTML:

- `login/token.php` bzw. ein vergleichbarer Token-Flow fuer den Mobile-Service
- `core_course_get_contents`
- `core_course_get_updates_since`
- `core_files_get_files`
- spaeter je nach Freischaltung auch weitere Funktionen fuer Foren, Grades oder Completion

Der offizielle Moodle-Mobile-Service ist laut Moodle-Dokumentation der eingebaute Webservice fuer die offizielle App. Wenn er auf einer Instanz aktiviert und fuer Nutzer verfuergbar ist, ist das meist der stabilste maschinenlesbare Pfad.

### Vorteile

- technisch die sauberste und robusteste Route
- klarere Datenmodelle statt Theme-/HTML-Abhaengigkeit
- bessere Grundlage fuer spaetere Zusatzfunktionen
- deutlich besser testbar

### Nachteile

- nicht garantiert verfuegbar
- kann durch fehlende Rechte oder gesperrte Token-Erzeugung blockiert sein
- haengt von Site-Konfiguration ab, die ausserhalb des Projekts liegt

### Datenintegritaet

Sehr gut. API-Daten liefern deutlich bessere Anker fuer stabile Remote-Identitaeten und Aenderungserkennung als HTML-Scraping.

### Notion-Eignung

Hoch. Strukturierte API-Daten sind ideal fuer Deadlines, Forumseintraege, Aufgaben oder spaetere Notion-Upserts.

### Aufwand

Mittel bis hoch, allerdings nur dann sinnvoll, wenn `doctor` diesen Pfad auch wirklich bestaetigt.

### Urteil

Technisch die beste Option, aber als alleiniger Plan zu riskant. Sie taugt nicht als einzige V1-Annahme.

---

## 8. Option 4: Hybrid-Architektur

### Wie funktioniert es

Das Projekt fuehrt zu Beginn eine Capability-Pruefung aus und entscheidet erst dann ueber den besten Pfad:

1. pruefen, ob ein offizieller Mobile-/Webservice-Zugang verfuegbar ist
2. falls ja, API-basiert arbeiten
3. falls nein, HTML-/Session-Sync verwenden
4. fuer Deadlines bevorzugt auf persoenlichen Kalenderexport/ICS setzen
5. Kurs-ZIP-Export nur als separaten Backup-Modus behalten

### Vorteile

- verbindet Realismus mit technischer Ambition
- blockiert die Implementierung nicht an einer unklaren Site-Konfiguration
- ermoeglicht spaetere Upgrades ohne Architekturbruch
- trennt Bulk-Export, Dateisync und Informationssync sauber

### Nachteile

- etwas hoehere konzeptionelle Komplexitaet
- `doctor` und Provider-Auswahl muessen sauber gebaut werden

### Datenintegritaet

Gut bis sehr gut, wenn die Integritaetsregeln aus Abschnitt 4 eingehalten werden.

### Notion-Eignung

Hoch, weil der Informationssync je Quelle den jeweils besten Pfad nutzen kann:

- Dateien ueber API oder HTML
- Deadlines ueber ICS
- spaeter Foren ueber RSS, API oder HTML

### Aufwand

Mittel. Hoher Nutzen pro investierter Komplexitaet.

### Urteil

Das ist die empfohlene Zielarchitektur.

---

## 9. Vergleich auf einen Blick

| Option | Delta-Sync fachlich sauber? | Ohne Admin realistisch? | Robustheit | Notion-Fit | Urteil |
| --- | --- | --- | --- | --- | --- |
| 1. ZIP-Export haerten | Nein, nur grob | Ja | Mittel | Niedrig | Nur Backup-/Exportmodus |
| 2. HTML-Ressourcensync | Ja | Ja | Mittel | Mittel | Sichere V1-Baseline |
| 3. Offizieller API-Pfad | Ja | Vielleicht | Hoch | Hoch | Technisch beste Route, aber nicht garantiert |
| 4. Hybrid | Ja | Ja | Hoch | Hoch | Empfohlene Architektur |

---

## 10. Capability-Katalog: Was LearnWeb potenziell automatisierbar hergibt

Die folgende Einordnung basiert auf offiziellen Moodle-Dokumentationspfaden und auf der Annahme, dass LearnWeb eine Moodle-Instanz mit lokalem Theme/Setup ist.

| Capability | Wahrscheinlicher Pfad | Ohne Admin realistisch | Einordnung |
| --- | --- | --- | --- |
| Kursdateien und Materialien | Mobile-API oder HTML/Session | Ja | Kernfunktion fuer V1 |
| Kurs-ZIP-Exporte | bestehender `downloadcontent.php`-Flow | Ja | Backup-/Exportmodus, nicht Delta-Sync |
| Deadlines / Kalender | persoenlicher ICS-Kalenderexport | Ja | Sehr guter erster Notion-Pfad |
| Forenbeitraege | RSS, Mobile-API oder HTML | Teilweise | RSS nur wenn site-/kursseitig aktiviert |
| Benachrichtigungen | Mobile, Session-Endpunkte oder E-Mail-Workaround | Teilweise | Direktes maschinelles Auslesen nicht immer offen |
| Grades / Bewertungen | API oder HTML | Teilweise | Erst nach Verfuegbarkeitspruefung sinnvoll |
| Completion / Fortschritt | API | Teilweise | Spaeter sinnvoll fuer Dashboards |
| Chat-Nachrichten | API | Teilweise | Spezialfall, nicht V1 |
| Quiz-Metadaten | API oder HTML | Teilweise | Spaeter moeglich, nicht prioritaer |

### Wichtige "Hacks", die wirklich nuetzlich sein koennen

#### 1. ICS statt HTML fuer Deadlines

Fuer Deadlines ist ein persoenlicher Kalenderexport meist deutlich robuster als HTML-Scraping. Wenn LearnWeb einen persoenlichen ICS-Feed anbietet, ist das sehr wahrscheinlich der beste erste Input fuer Notion.

#### 2. E-Mail-Ingest statt UI-Scraping fuer Notifications

Wenn webseitige Notifications nicht stabil oder nicht offiziell zugreifbar sind, kann es billiger und robuster sein, LearnWeb-E-Mail-Benachrichtigungen zu aktivieren und diese per Mail-Filter oder IMAP spaeter in Notion zu ueberfuehren.

#### 3. `doctor` als Capability-Matrix

Statt von vornherein eine einzige Integrationsart zu unterstellen, sollte das Tool am Anfang selbst pruefen:

- funktioniert der Mobile-Service?
- gibt es Hinweise auf persoenliche Kalenderexports?
- sind Foren-RSS-Feeds im konkreten Kurs sichtbar?
- welche Seitentypen kommen in den belegten Kursen ueberhaupt vor?

#### 4. "Recent activity" nur als Beschleuniger, nie als Wahrheit

Aktivitaetsseiten koennen helfen, den Suchraum eines Laufs zu verkleinern. Sie duerfen aber nicht die einzige Wahrheitsquelle sein, weil sie weder Vollstaendigkeit noch stabile Objektidentitaeten garantieren.

---

## 11. Notion-Optionen

### Empfohlener erster Notion-Use-Case

Der erste strukturierte Notion-Output sollte **nicht** allgemeine Notifications oder Foren sein, sondern Deadlines bzw. kalendarische LearnWeb-Ereignisse.

Warum:

- Deadlines sind zeitkritisch.
- Sie haben einen natuerlichen strukturierten Shape.
- Sie passen gut in eine Aufgaben- oder Fristen-Datenbank.
- Ein ICS-Feed ist hier oft robuster als HTML-Scraping.

### Empfohlene Modellrichtung

Fuer Notion bietet sich mittelfristig eine Aufgaben- oder Fristen-Datenquelle an mit Feldern wie:

- `Source ID`
- `Title`
- `Course`
- `Due`
- `Kind`
- `Source URL`
- `Remote State`
- `Fingerprint`
- `Last Seen`
- `Action Status`

Wichtig:

- `Source ID` dient fuer idempotente Upserts.
- `Action Status` bleibt nutzergepflegt und wird vom Sync nicht ueberschrieben.
- Vom Sync gepflegte Felder und von dir gepflegte Workflow-Felder muessen getrennt bleiben.

### Aktueller Blocker

Die konkrete Notion-Struktur konnte zum Zeitpunkt dieses Papiers nicht live geprueft werden, weil die Notion-Integration auf die verlinkten Datenbanken aktuell mit `404 object_not_found` antwortet.

Das bedeutet:

- Dieses Papier macht bewusst **keine** DB-spezifischen Aussagen ueber deine existierenden Properties oder Relationen.
- Die Notion-Integration muss spaeter noch auf dein tatsaechliches Modell zugeschnitten werden.

---

## 12. Empfehlung

### Empfohlene Zielarchitektur

Option 4, also eine Hybrid-Architektur.

Begruendung:

- Sie ist mit normalem Nutzerkonto realistisch.
- Sie blockiert das Projekt nicht an unklaren Admin-Freischaltungen.
- Sie laesst den technisch besseren API-Pfad offen.
- Sie trennt das eigentliche Sync-Problem von Nebenpfaden wie ZIP-Backup oder spaeteren Notion-Erweiterungen.

### Empfohlene erste Umsetzungsstufe

Die erste echte Iteration sollte **nicht** sofort alles gleichzeitig bauen, sondern in dieser Reihenfolge:

1. Repo sauber aufsetzen
2. `doctor` bauen
3. lokales Manifest und SQLite-State einfuehren
4. HTML-basierten Ressourcensync fuer Dateien implementieren
5. ZIP-Export als optionalen Backup-Befehl beibehalten
6. danach Deadlines per ICS nach Notion spiegeln

Der entscheidende Punkt ist: Option 2 ist die sichere Startbasis, Option 3 die opportunistische Aufwertung, Option 4 der Rahmen darum.

---

## 13. Empfohlene naechste Iteration

### Repo-Grundgeruest

- `pyproject.toml`
- `src/learnweb_sync/`
- `tests/`
- `fixtures/`
- `.env.example`
- `README.md`
- `docs/`

### Geplante CLI-Befehle

- `learnweb-sync doctor`
- `learnweb-sync sync-files`
- `learnweb-sync sync-deadlines`
- `learnweb-sync export-course-zips`

### Was `doctor` pruefen sollte

- Login funktioniert
- Dashboard/Kursliste ist parsebar
- gibt es Hinweise auf Mobile-/Webservice-Verfuegbarkeit?
- gibt es persoenliche Kalenderexporte?
- sind RSS-Hinweise in Foren sichtbar?
- welche Aktivitaets-/Ressourcentypen treten in den belegten Kursen ueberhaupt auf?

### Was `sync-files` leisten muss

- Kurse einsammeln
- Materialien identifizieren
- Remote-Identitaet und Fingerprint bilden
- gegen Manifest vergleichen
- nur neue oder geaenderte Ressourcen herunterladen
- keinen automatischen Delete ausfuehren

### Was `sync-deadlines` spaeter leisten soll

- ICS-Quelle lesen
- Ereignisse normalisieren
- per `Source ID` idempotent nach Notion upserten
- lokale Workflow-Felder in Notion unangetastet lassen

---

## 14. Offene Fragen

- Welche konkreten LearnWeb-Ressourcentypen dominieren in deinen Kursen?
- Ist der offizielle Mobile-/Webservice-Pfad auf deinem LearnWeb-Account ueberhaupt offen?
- Bietet LearnWeb einen persoenlichen ICS-Export an, und wenn ja, in welcher Form?
- Soll lokal geloeschtes oder remote verschwundenes Material jemals aktiv archiviert werden oder nur markiert?
- Wie genau ist deine Notion-Struktur aufgebaut, sobald die Integration Zugriff darauf hat?
- Sollen Benachrichtigungen spaeter direkt aus LearnWeb kommen oder ist E-Mail-Ingest dafuer akzeptabel?

---

## 15. Quellen

Die folgenden Quellen sind die massgebliche Referenz fuer die oben genannten Optionen und Einschraenkungen:

- MoodleDocs: [Using web services](https://docs.moodle.org/en/Using_web_services)
- MoodleDocs: [Mobile web services](https://docs.moodle.org/en/Mobile_web_services)
- MoodleDocs: [Using Calendar](https://docs.moodle.org/en/Using_Calendar)
- MoodleDocs: [Notifications](https://docs.moodle.org/en/Notifications)
- MoodleDocs: [RSS feeds settings](https://docs.moodle.org/en/RSS_feeds_settings)
- MoodleDocs: [Using RSS feeds](https://docs.moodle.org/en/Using_RSS_feeds)
- MoodleDocs Dev: [Web service API functions](https://docs.moodle.org/dev/Web_service_API_functions)
- Notion API: [Introduction](https://developers.notion.com/reference/intro)

---

## 16. Entscheidungsstand

Wenn auf Basis dieses Papiers heute entschieden werden soll, welche Architektur als naechstes umgesetzt wird, lautet die Entscheidung:

- **Jetzt bauen:** HTML-basierter Ressourcensync mit sauberem Manifest und `doctor`
- **Parallel pruefen:** ob der offizielle Mobile-/Webservice-Pfad freigeschaltet ist
- **Danach erweitern:** Deadlines per ICS nach Notion
- **Bewusst nicht als Kern nehmen:** den bisherigen Kurs-ZIP-Export

Das ist der kuerzeste Weg zu einem fachlich korrekten Produkt statt zu einem nur etwas "ordentlicheren" ZIP-Downloader.

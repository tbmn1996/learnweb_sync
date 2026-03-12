# LearnWeb Organizer Skill Integration Exploration

## Kurzfazit

Der `learnweb-organizer`-Skill ist kein Loesungsbaustein fuer den eigentlichen LearnWeb-Delta-Sync, sondern ein Nachbearbeitungs-Workflow fuer bereits vorhandene Moodle-/LearnWeb-ZIP-Exporte. Sein staerkster Wert liegt in der lokalen Kuratierung:

- Materialien sortieren
- Rauschdateien herausfiltern
- Dateien standardisiert umbenennen
- ein saubereres Archivpaket erzeugen

Fuer das Gesamtprodukt ist der Skill deshalb am plausibelsten als:

- optionales Sidecar zum ZIP-Export
- spaeterer Post-Processor fuer lokale Materialsammlungen
- Inspirationsquelle fuer Benennungs- und Filterregeln

Er ist nicht geeignet als Kern der Delta-Sync-Architektur.

---

## 1. Ausgangslage

Im bestehenden LearnWeb-Konzept gibt es bereits eine klare Hauptlinie:

- LearnWeb-Inhalte sollen korrekt und inkrementell synchronisiert werden
- Kurs-ZIP-Exporte sollen hoechstens ein Nebenpfad sein
- Deadlines und spaeter weitere Signale sollen in Notion gespiegelt werden

Das erste Exploration Paper in `docs/learnweb_sync_exploration.md` trennt diese Themen bereits implizit:

- **Sync** = Remote nach lokal spiegeln
- **Export** = grober Bulk-Download eines gesamten Kurses
- **Informationssync** = strukturierte Signale wie Deadlines, Foren oder Notifications

Der `learnweb-organizer`-Skill sitzt nicht auf der Sync-Schicht, sondern deutlich spaeter im Datenfluss: Er setzt voraus, dass bereits ein ZIP oder eine lokale Materialmenge vorhanden ist.

---

## 2. Was der Skill konkret tut

Die `SKILL.md` aus dem Paket `learnweb-organizer.skill` beschreibt einen sehr konkreten Workflow fuer Moodle-/LearnWeb-ZIP-Exporte:

- ZIP analysieren
- Inhalte entpacken
- PDFs und weitere Dateitypen finden
- Kurs und Abkuerzung heuristisch bestimmen
- Materialien in Kategorien einordnen
- Rauschdateien wie Werbeflyer, `index.html` oder `moodle.css` herausfiltern
- standardisierte Dateinamen erzeugen
- die umbenannten Dateien in ein neues, flaches ZIP packen

### Uebersetzbare Bausteine des Skills

Der Skill laesst sich in drei inhaltliche Regelbereiche zerlegen:

1. **Kategorisierungslogik**
   Zuweisung zu Typen wie `Lecture`, `Tutorial`, `Exam`, `Resource`, `Script`, `Assignment`.

2. **Noise-Filter**
   Entfernen irrelevanter Artefakte wie Werbe- oder Moodle-Hilfsdateien.

3. **Benennungs- und Paketierungslogik**
   Standardisierte Dateinamen und erneutes Verpacken in ein sauberes Archiv.

Das ist fachlich wertvoll, aber es ist ein lokaler Ordnungs-Workflow, kein Synchronisations-Workflow.

---

## 3. Was der Skill nicht tut

Der Skill loest mehrere Kernfragen des Gesamtprojekts gerade **nicht**:

- kein Login in LearnWeb
- keine Remote-Erkundung von Kursen
- kein Delta-Sync
- kein per-Objekt-Manifest
- keine stabile Remote-Identitaet
- keine API-Nutzung fuer Moodle
- keine Notion-Integration
- keine belastbare Live-Automation gegen die LearnWeb-Quelle

Das ist wichtig, weil sonst die falsche Produktentscheidung naheliegt: Der Skill kann Materialien verschönern, aber nicht korrekt entscheiden, **ob** etwas neu oder geaendert ist.

Ein Organizer darf daher nie:

- die einzige Quelle fuer Objektidentitaet sein
- die Wahrheit ueber Aenderungen bestimmen
- Download- oder Delta-Entscheidungen ersetzen

---

## 4. Portabilitaet und technische Luecken

Der Skill ist aktuell kein direkt wiederverwendbares Modul, sondern ein Claude-spezifischer Prompt-Workflow mit festen Umgebungsannahmen.

### Konkrete Bindungen an die Ursprungsumgebung

- Arbeitsverzeichnis `/home/claude/lw-extract`
- Output-Verzeichnis `/mnt/user-data/outputs/[KURS]`
- Shell-zentrierter Ablauf statt Python-Modul oder CLI
- Benutzerinteraktion ueber Prompt-Entscheidungen statt stabile API oder Konfiguration

### Konsequenz

Der Skill kann nicht 1:1 in dieses Projekt "eingesteckt" werden. Fuer eine Integration gaebe es nur zwei realistische Wege:

1. den Workflow in Python und Repo-eigene CLI-Kommandos uebersetzen
2. nur die Regeln und Heuristiken uebernehmen, nicht aber den Skill selbst

Das spricht bereits gegen eine zentrale Rolle des Skills im Kernsystem.

---

## 5. Begriffsrahmen fuer das Gesamtprodukt

Damit die spaetere Architektur nicht verschwimmt, sollte das Projekt drei Begriffe sauber trennen:

### Sync

Remote nach lokal spiegeln, idempotent und integritaetsorientiert.

Ziele:

- neue/geaenderte Inhalte erkennen
- Manifest pflegen
- Downloads korrekt und wiederholbar ausfuehren

### Export

Bulk-ZIP eines gesamten Kurses oder Kursmaterials.

Ziele:

- Backup
- grobe Archivierung
- Materialsammlung auf einen Schlag

### Organize

Lokale Kuratierung bereits vorhandener Dateien.

Ziele:

- strukturieren
- umbenennen
- Rauschen filtern
- ein konsumierbares Archivformat erzeugen

Diese Begriffe duerfen in der Produktarchitektur nicht vermischt werden. Der `learnweb-organizer` gehoert in die dritte Kategorie.

---

## 6. Integrationsoptionen

Alle sinnvollen Optionen werden entlang derselben Fragen beurteilt:

- Wie passt es ins Produkt?
- Vorteile
- Nachteile
- Risiken fuer Datenintegritaet
- Beziehung zum Delta-Sync
- Implementierungsaufwand
- Urteil

---

## 7. Option 1: Rein externer Skill

### Wie passt es ins Produkt?

Der Skill bleibt ausserhalb des Repos ein manueller Helfer fuer einmalige oder historische Bestandsbereinigungen von Moodle-/LearnWeb-ZIPs.

### Vorteile

- keine Repo-Komplexitaet
- sofort nutzbar fuer manuelle Aufraeumarbeiten
- kein Risiko, die Sync-Architektur mit Organize-Logik zu vermischen

### Nachteile

- kein einheitlicher Gesamtworkflow
- keine Wiederverwendung im eigentlichen Tooling
- Ergebnisse und Regeln leben ausserhalb des Repos

### Risiken fuer Datenintegritaet

Gering fuer das Kernsystem, weil keine Kopplung besteht. Mittel fuer die lokale Materialpflege, weil die Heuristiken manuell und promptgesteuert bleiben.

### Beziehung zum Delta-Sync

Keine direkte. Der Skill arbeitet erst nach dem Download oder Export.

### Implementierungsaufwand

Sehr niedrig.

### Urteil

Sinnvoll fuer spontane Bestandsaufraeumung, aber strategisch eher eine Minimalvariante.

---

## 8. Option 2: Optionales Sidecar zum ZIP-Export

### Wie passt es ins Produkt?

Das spaetere Repo haette einen klar separaten Exportpfad, z. B. `learnweb-sync export-course-zips`. Daran koennte optional ein Organizer-Layer anhaengen, der Export-ZIPs lokal kuratiert.

### Vorteile

- klare Produktgrenze
- gute Wiederverwendung des Skills fuer Bulk-Archive
- keine Verunreinigung der Kern-Sync-Logik
- besonders passend fuer Altbestaende oder Semesterarchive

### Nachteile

- der Organizer haengt weiterhin an ZIP-Exporten, die selbst nicht das Kernziel des Projekts sind
- zwei nacheinanderliegende Workflows muessen gepflegt werden

### Risiken fuer Datenintegritaet

Niedrig, wenn die Organizer-Schicht nur auf lokalen Kopien oder Exportartefakten arbeitet und nie die Sync-Wahrheit beeinflusst.

### Beziehung zum Delta-Sync

Nur indirekt. Der Delta-Sync bleibt unberuehrt, der Organizer bearbeitet nur einen Nebenpfad.

### Implementierungsaufwand

Niedrig bis mittel.

### Urteil

Sehr plausible Integrationsform. Besonders gut, wenn ZIP-Exporte als Backup oder Archivierungsformat im Produkt bleiben.

---

## 9. Option 3: Integrierter Post-Processor fuer das Repo

### Wie passt es ins Produkt?

Das Repo bekommt einen separaten Befehl wie:

- `learnweb-sync organize-materials`
- optional spaeter `learnweb-sync organize-archive --input <zip|folder>`

Dieser Befehl arbeitet auf lokal bereits vorhandenen Dateien oder Verzeichnissen, unabhaengig davon, ob diese aus ZIP-Exporten oder aus einem spaeteren Datei-Sync stammen.

### Vorteile

- staerkste Wiederverwendbarkeit
- nicht auf ZIP beschraenkt
- macht aus dem Skill einen echten Produktbaustein
- bessere Testbarkeit als der heutige Prompt-Workflow

### Nachteile

- braucht echte Re-Implementierung in Python
- Kategorisierung und Benennung sind heuristisch und koennen Fehlklassifikationen erzeugen
- Scope des Produkts wird breiter

### Risiken fuer Datenintegritaet

Mittel. Nicht im Sinne des Remote-Syncs, sondern fuer die lokale Materialorganisation:

- falsche Kategoriezuordnung
- unerwuenschte Umbenennungen
- versehentliches Wegfiltern relevanter Dateien

Das muss durch Dry-Run, Mapping-Ausgabe und explizite User-Bestaetigung abgefedert werden.

### Beziehung zum Delta-Sync

Getrennt. Der Post-Processor darf nur auf lokale Artefakte wirken und nie darueber entscheiden, was remote neu oder geaendert ist.

### Implementierungsaufwand

Mittel.

### Urteil

Die staerkste produktive Integrationsvariante, wenn das Projekt spaeter mehr als nur "Download-Sync" sein soll.

---

## 10. Option 4: Heuristik-Extraktion in den Kern

### Wie passt es ins Produkt?

Nicht der ganze Skill wird integriert, sondern nur ausgewählte Regeln:

- Kategorielisten
- Noise-Pattern
- Benennungsschemata

Diese Regeln werden als Python-Konfiguration oder Hilfsmodul uebernommen, aber nur fuer lokale Materialorganisation.

### Vorteile

- maximale Wiederverwendung des wertvollen Wissens
- keine 1:1-Abhaengigkeit vom Skill-Format
- gute Grundlage fuer spaetere CLI-Features

### Nachteile

- Gefahr, dass Organizelogik schleichend in die Kern-Sync-Schicht einwandert
- Heuristiken sind kurs- und sprachabhaengig
- ohne saubere Produktgrenze droht Konzeptvermischung

### Risiken fuer Datenintegritaet

Mittel. Vor allem dann, wenn Heuristiken ungeprueft auf produktive Dateien losgelassen werden.

### Beziehung zum Delta-Sync

Sollte keine haben. Diese Regeln duerfen nie zur Aenderungserkennung oder Objektidentitaet verwendet werden.

### Implementierungsaufwand

Mittel.

### Urteil

Sinnvoll als selektive Wissensuebernahme, aber nur mit harter Trennung zwischen Sync- und Organizer-Schicht.

---

## 11. Option 5: Nicht integrieren, nur als Referenz

### Wie passt es ins Produkt?

Der Skill wird nur als Inspirationsquelle dokumentiert. Weder CLI noch Repo-Workflow uebernehmen ihn direkt.

### Vorteile

- keine zusaetzliche Komplexitaet
- keine technische Portierungsarbeit
- klare Fokussierung auf den eigentlichen Sync

### Nachteile

- Potenzial fuer Materialorganisation bleibt ungenutzt
- Wissen aus dem Skill muss spaeter eventuell doppelt neu erfunden werden

### Risiken fuer Datenintegritaet

Sehr gering, weil keinerlei operative Kopplung entsteht.

### Beziehung zum Delta-Sync

Keine.

### Implementierungsaufwand

Sehr niedrig.

### Urteil

Saubere Minimalvariante, aber verschenkt moeglichen Produktwert fuer kuratierte Studienarchive.

---

## 12. Vergleich auf einen Blick

| Option | Produktfit | Risiko fuer Sync-Kern | Wiederverwendbarkeit | Aufwand | Urteil |
| --- | --- | --- | --- | --- | --- |
| 1. Rein extern | niedrig bis mittel | sehr niedrig | niedrig | sehr niedrig | brauchbar fuer Einzelfaelle |
| 2. Sidecar zum ZIP-Export | mittel bis hoch | niedrig | mittel | niedrig bis mittel | sehr gute Zusatzoption |
| 3. Integrierter Post-Processor | hoch | niedrig bis mittel | hoch | mittel | beste produktive Ausbauform |
| 4. Heuristik-Extraktion | mittel | mittel | mittel bis hoch | mittel | nur selektiv sinnvoll |
| 5. Nur Referenz | niedrig | sehr niedrig | niedrig | sehr niedrig | gute Minimalvariante |

---

## 13. Bewertung

Der Skill ist fuer **Archivierung, Ordnung und Umbenennung** klar wertvoll. Er kodiert nuetzliche Regeln fuer:

- Materialkategorien
- Dateibenennung
- Noise-Filter
- saubere Archivpakete

Er ist aber **nicht** wertvoll als Quelle fuer:

- Aenderungserkennung
- Remote-Wahrheit
- Delta-Entscheidungen
- Objektidentitaet

Die wichtigste Produktgrenze lautet deshalb:

- **Sync-Schicht** beschafft und validiert Daten
- **Organizer-Schicht** kuriert und praesentiert lokale Daten

Sobald das vermischt wird, wird das Produkt fachlich unsauber.

---

## 14. Notion-Bezug

Der Skill hat keinen direkten starken Notion-Fit.

Er liefert:

- lokale Ordnung
- standardisierte Dateinamen
- moeglicherweise spaeter bessere Metadatenableitung

Er liefert nicht:

- Deadlines
- stabile Task-/Event-Objekte
- Benachrichtigungsstroeme
- natuerliche Notion-Upsert-Identitaeten

Deshalb sollten im Gesamtprodukt zwei Dinge getrennt bleiben:

1. **Notion-Deadlines und Informationssync**
   aus LearnWeb-Strukturen wie Kalender, API-Daten oder spaeter Foren/Notifications

2. **Organizer-Funktionen**
   fuer lokale Materialkuration

Wenn es spaeter doch einen Notion-Bezug geben soll, dann hoechstens indirekt:

- Organizer erzeugt sauberere lokale Artefakte
- daraus koennen spaeter Metadaten oder Sammlungen fuer Notion abgeleitet werden

Mehr sollte dieses Dokument bewusst nicht behaupten, solange weder Notion-Struktur noch Organizer-Integration technisch umgesetzt sind.

---

## 15. Empfehlung

### Standardempfehlung

Die beste strategische Einordnung ist:

- kurzfristig `Option 5` oder `Option 1`
- mittelfristig `Option 2`
- langfristig am sinnvollsten `Option 3`

Anders formuliert:

- **jetzt**: Skill als Referenz oder externer Helfer behandeln
- **spaeter**: optional an den ZIP-Exportpfad haengen
- **nur wenn das Produkt breiter werden soll**: als echten Post-Processor ins Repo uebersetzen

### Warum nicht als Kern?

Weil der Skill die falsche Stelle im Datenfluss adressiert. Er bearbeitet Ergebnisse, aber er loest nicht das Kernproblem der Beschaffung und Korrektheit.

Die empfohlene Grundhaltung fuer das Gesamtprodukt lautet deshalb:

- Kernsystem: LearnWeb-Sync mit Integritaet
- Zusatzsystem: Organizer fuer lokale Kuration

Das ist fachlich klarer und langfristig robuster.

---

## 16. Empfohlene Produktgrenzen und moegliche CLI-Oberflaechen

Wenn spaeter eine technische Integration gebaut wird, sollten nur diese Grenzen gelten:

- `learnweb-sync export-course-zips`
  Bulk-Export fuer Backup/Archivzwecke
- `learnweb-sync organize-materials`
  Lokale Dateien oder Verzeichnisse kuratieren
- optional spaeter `learnweb-sync organize-archive --input <zip|folder>`
  Expliziter Organizer-Einstieg fuer manuelle Materialpakete

Wichtig ist:

- Organizer-Kommandos arbeiten nur lokal
- Organizer-Kommandos duerfen Dry-Run und Mapping-Preview haben
- Organizer-Kommandos duerfen nie Sync-State oder Delta-Wahrheit veraendern

---

## 17. Naechste sinnvolle Folgefrage

Die naechste echte Produktfrage lautet nicht, wie man den Skill technisch "irgendwie mit reinzieht", sondern:

Soll dieses Repo langfristig nur ein belastbarer LearnWeb-Sync werden oder auch ein persoenlicher Wissens- und Archivlayer fuer kuratierte Studienunterlagen?

Wenn die Antwort nur "Sync" ist, bleibt der Skill eher extern oder referenziell.

Wenn die Antwort "Sync plus Studienarchiv" ist, wird `Option 3` spaeter deutlich attraktiver.

---

## 18. Entscheidungsstand

Wenn heute entschieden werden soll, wie der `learnweb-organizer` in das Gesamtkonstrukt einzuordnen ist, lautet die Entscheidung:

- **Nicht** als Kernbestandteil des LearnWeb-Delta-Syncs
- **Ja** als optionaler Organizer-Layer fuer ZIP-Exporte oder lokale Materialsammlungen
- **Ja** als Quelle fuer Kategorisierungs-, Filter- und Benennungsregeln
- **Nein** als Quelle fuer Objektidentitaet, Delta-Logik oder Notion-Synchronisationswahrheit

Damit bleibt das System fachlich sauber:

- erst korrekt beschaffen
- dann optional kuratieren
- spaeter eventuell in ein persoenliches Studienarchiv ausbauen

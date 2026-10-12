# Funktionsprüfung für WS 26/27

Dieser Bericht hält den ursprünglichen Auditstand auf Code-Baseline `6a25a1d` fest. Die hier
beschriebenen Fehlbefunde und der damalige Scope sind historische Ausgangswerte, keine Aussage
über den aktuellen Reparaturstand. Die anschließende Live-Nachprüfung und offene Abnahmepunkte
stehen im [Reparaturbericht](reviews/261012-Reparatur-Abnahme.md).

**Stand: 12.10.2026. Ergebnis: noch keine Semesterabnahme.** Der Kernparser funktioniert
gegen LearnWeb und die 164 vorhandenen Offline-Tests bestehen. Vier neue Semesterkurse sind
noch nicht in Notion erfasst. Zusätzliche gezielte Fehlerproben zeigen Lücken bei Erfolgsmeldung,
Dry-Run und Wiederanlauf. Auftrag: prüfen und dokumentieren; keine Funktionsreparaturen,
Notion-Mutationen, Aktivierungen, Commits, Pushes oder Deployments.

**Einordnung nach zwei unabhängigen DSH-Reviews:** Der Leseweg für das neue Semester ist
funktionsfähig und live geprüft. Ein vollständiger neuer Import und die laufende Railway-Automatik
sind noch nicht abgenommen; ein Totalausfall ist nicht belegt. Kursauswahl/-aktivierung,
Fehlerverhalten des Datei-Syncs und der separate Transkriptions-Worker sind getrennt zu bewerten.
Review-Ergebnisse und Nachprüfungen: [DSH-REVIEW.md](DSH-REVIEW.md).

## Semesteranschluss

Das Projektskript erkennt nach echtem Login 28 belegte Kurse. Der Connector liefert 29
Dashboard-Treffer; der zusätzliche Treffer `94743` ist nicht als Kurs ladbar und wird vom
strengeren Projektfilter nicht übernommen. In Notion stehen 25 Kurszeilen mit 24 eindeutigen
Kurs-IDs. Die folgenden vier belegten Kurse fehlen dort vollständig:

| Kurs | LearnWeb-ID | Gelesener Shortname | Erkannte Aktivitäten |
|---|---|---|---|
| Analysis / Mathematik für WI WS 26/27 | [96289](https://www.uni-muenster.de/LearnWeb/learnweb2/course/view.php?id=96289) | `AFWFW-2026_2` | 99 |
| Informatik I WS 26/27 | [94909](https://www.uni-muenster.de/LearnWeb/learnweb2/course/view.php?id=94909) | `Informatik_I-2026_2` | 13 |
| Einführung in Java WS 26/27 | [94906](https://www.uni-muenster.de/LearnWeb/learnweb2/course/view.php?id=94906) | `Java-2026_2` | 4 |
| Lineare Algebra WS 26/27 | [96296](https://www.uni-muenster.de/LearnWeb/learnweb2/course/view.php?id=96296) | `LAFW-2026_2` | 34 |

Aktivitätszahlen sind Parser-Ergebnisse einschließlich nicht pushbarer Typen, keine Zahl
erfolgreich importierter Dateien. Diese Kurse enthalten bereits `resource`, `folder`, `url`
oder `page`-Inhalte. Ein `sync-courses` würde neue Kursseiten zunächst mit `SyncContent=false`
anlegen. Die Auswahl für den produktiven Import bleibt Thomas' Entscheidung.

In Notion sind acht ältere Kurse aktiv: `WA-2026_1`, `BWL2-2026`, `TAFWFW-2026_1`,
`OR-2026_1`, `Informatik_II-2026_1`, `AFW-2026_1`, `FOF-2025_2`, `DaMa-2026_1`.
Zwei inaktive IT-Sicherheitszeilen zeigen beide auf Kurs `79220`; der Code behandelt die
Kurs-ID deshalb als Konflikt. Keine Zeile wurde geändert oder entfernt.

Die lokale Konfiguration enthält `COURSE_MAP` nur für `OR-2025_1` und `FOF-2025_2`.
Die neue Kursrelation wäre dennoch möglich, das zusätzliche Select-Feld `Kurs` bliebe ohne
Mapping leer. `CURRENT_SEMESTER_OVERRIDE` ist lokal nicht gesetzt; die Funktion liefert
korrekt `WS 26/27`. Die alte `.env`-Variable `CURRENT_SEMESTER` wird nicht ausgewertet.
Railway-Variablen sind davon unabhängig und noch ungeprüft.

Ein später ergänztes `COURSE_MAP` füllt bestehende `resource`-/`url`-/`page`-Seiten nicht nach:
Sie werden wegen vorhandener `notion_id` übersprungen. Auch unveränderte Folder werden nicht
neu gepatcht. Das Select-Feld benötigt gegebenenfalls eine gezielte Property-Korrektur;
die Kursrelation bleibt davon unberührt. Fehlendes Mapping allein blockiert den Import nicht.

Offen ist, weshalb ein Inhaltsbeleg vom 08.10.2026 vorhanden ist, die vier neuen Kursseiten
aber fehlen. Einschreibungszeitpunkt, tatsächlicher Auslöser des Inhaltsimports und letzte
Railway-Läufe sind unbekannt. Neue Kurse könnten erst danach sichtbar geworden sein;
auch der Kursabgleich selbst überspringt Ladefehler nach einem Logeintrag
(`learnweb_sync.py:1519-1521`). Die Live-Logs müssen klären, ob nur Kursanlage/Aktivierung
ansteht oder der Betrieb repariert werden muss. Der Zeitstempel allein beweist keinen Schedulerlauf.

## Bestätigte Funktionsbefunde

### 1. Fehlererkennung und Dateiübernahme

| Priorität | Befund und Auswirkung | Beleg / Abhilfe zur späteren Entscheidung |
|---|---|---|
| Hoch | Scrape-Fehler eines Kurses werden protokolliert und übersprungen; der Gesamtlauf kann mit Exit 0 enden. | `learnweb_sync.py:1651`, `:2616`; unabhängige Fehlerprobe schlägt wie erwartet fehl. Fehlerzahl an `run` weitergeben. |
| Hoch | Fehler einzelner Push-Handler erscheinen nur in der Zusammenfassung. Railway kann einen unvollständigen Import als erfolgreich behandeln. | `learnweb_sync.py:2587`, `:2618`; zweite unabhängige Probe bestätigt normalen Return trotz `1 Fehler`. |
| Hoch | Ein fehlgeschlagener Resource-Upload führt trotzdem zu einer Notion-Seite ohne Datei und `status=synced`. Spätere Läufe überspringen sie wegen `notion_id`. | `learnweb_sync.py:2114`, `:2154`, `:2178`; Codeprüfung und bestehende Tests tolerieren `upload_id=None`. Transiente Uploadfehler sollten wiederholbar bleiben; die Behandlung von Dateien über 20 MB ist gesondert festzulegen. |
| Mittel | HTTP 200 mit nicht erkennbarer Kursstruktur kann alle lokalen Kursaktivitäten als `removed` markieren. Noch nicht importierte Inhalte werden bis zu einem korrekten Scan übersprungen; Notion-Seiten werden dadurch nicht gelöscht. | `learnweb_sync.py:632`, `:1686`, `:2046`; ein späterer korrekter Scan reaktiviert die Einträge (`:388`), erhaltene `notion_id` verhindert erneute Anlage. |

Ein tatsächlicher verlorener PDF-Anhang ist damit **nicht** nachgewiesen: Die Live-Abfrage
`Format=pdf` und `LW Download leer` ergab null Treffer. Der Uploadbefund beschreibt einen
bestätigten Fehlerpfad, keinen behaupteten vorhandenen Datenverlust.

Zusätzlich durch die DSH-Reviews und isolierte Gegenproben bestätigt:

1. **Abbruchfenster:** Wird nach erfolgreicher Notion-Seitenerstellung, aber vor dem lokalen
   Speichern der ID abgebrochen, kann ein Folgelauf eine zweite Seite erzeugen. Railway beendet
   den Sync standardmäßig nach 1800 Sekunden. Die Codekette wurde mit einer simulierten harten
   Unterbrechung reproduziert; ein entsprechender Produktionsvorfall ist nicht belegt.
2. **Größenlimit:** Eine Resource über 20 MiB wird schon beim Download abgewiesen und erhält
   keine Notion-Seite. Ein solches Folder-Mitglied blockiert auch dessen Neuanlage oder
   Aktualisierung mit kleineren Dateien. Vorhandene Seiten werden nicht gelöscht. Der Fehler
   bleibt erneut versuchbar; die Ursache der alten lokalen `download_unavailable`-Fälle ist
   damit nicht geklärt. Bei bekannter Content-Length wird der Dateibody gar nicht gelesen.

Code, Gegenproben und Einschränkungen stehen in [DSH-REVIEW.md](DSH-REVIEW.md).

### 2. Scheduler, Neustart und Health

| Priorität | Befund und Auswirkung | Beleg / Abhilfe zur späteren Entscheidung |
|---|---|---|
| Mittel | Code-Default `0 6 * * 1-5` bedeutet unter APScheduler 3 Di–Sa statt Mo–Fr. Montag veröffentlichte Inhalte würden erst Dienstag berücksichtigt; fünf wöchentliche Termine bleiben. | `server.py:46`, `:252`, `:275`; [APScheduler-3-Dokumentation](https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html). Aktuelle Live-Variable unbekannt; kein belegter Dienstausfall. |
| Hoch | Der Fehler-Schwellenwert ruft `sys.exit(1)` aus einem Hintergrundthread auf. Der Prozess bleibt bestehen, die zugesagte Railway-Neustartregel wird so nicht ausgelöst. | `server.py:166`, `:171`, `:246`; isolierter Subprozess gibt nach dem Fehler `PROCESS_STILL_ALIVE` mit Exit 0 aus. [Python 3.12](https://docs.python.org/3.12/library/sys.html#sys.exit). |
| Mittel | `/health` prüft nur drei LearnWeb-Variablen und den existierenden DB-Elternpfad. Fehlendes Notion-Token, DB-IDs oder Webhook-Secret verhindern kein HTTP 200. Ohne Secret akzeptiert der Webhook Aufrufe ohne Authentifizierung. | `server.py:42`, `:84`, `:222`. Konkreter Konfigurationsfehlerpfad; die produktiven Werte wurden nicht ausgelesen. Kein belegter offener Live-Endpunkt. |

Die bestandene isolierte `_run_sync()`-Probe belegt nur, dass nach drei Fehlern `SystemExit`
entsteht. Sie belegt keinen Prozessneustart. Die zusätzliche echte Thread-/Subprozessprobe
widerlegt diese weitergehende Schlussfolgerung ausdrücklich.

### 3. Lokale Transkription

| Priorität | Befund und Auswirkung | Beleg / Abhilfe zur späteren Entscheidung |
|---|---|---|
| Hoch | Opencast-`transcribe --dry-run` ruft `cmd_sync_courses()` auf und kann Notion-Kurse anlegen oder URLs aktualisieren. | `learnweb_sync.py:3311`; isolierte Probe erfasst genau einen unerlaubten `notion_create_course`-Aufruf. Nur der getrennte `--url`-Dry-Run nutzt ausschließlich Notion-Lookups. |
| Hoch | Nach hartem Abbruch bleiben `claimed`, `downloaded`, `transcribed` und weitere Zwischenstände liegen. Der nächste normale Lauf überspringt sie. | `transcription/manifest.py:133`, `learnweb_sync.py:3427`; Probe mit verwaistem `downloaded` endet mit `skipped`. Recovery nach Erhalt des exklusiven Locks fehlt. |
| Hoch | `--force` leert eine vorhandene Meeting-Seite nur bei lokalem `appended_block_count > 0`. Nach Remote-Append vor lokalem Checkpoint kann der Zähler 0 sein und der Text doppelt angehängt werden. | `learnweb_sync.py:3530`; Probe mit vorhandener Meeting-Seite und Zähler 0 bestätigt fehlenden Löschaufruf trotz erfolgreicher Neuverarbeitung. |

Ein weiterer Subagent-Verdacht wurde **verworfen**: `urljoin(base, None)` wirft hier keinen
`TypeError`, sondern liefert die Basis-URL. Der behauptete Abbruch bei einer linklosen
Opencast-Aktivität ist nicht belegt. Die falsche Basis-URL ist kein sinnvoller Discovery-Aufruf,
wird aber nicht als bestätigter Laufabbruch berichtet.

### 4. Lokaler Betrieb und Dokumentation

1. Die lokale `.venv` enthält die Sync-/Whisper-Pakete, aber kein Flask, APScheduler oder
   gunicorn. Der Railway-Server kann in dieser Umgebung nicht unverändert gestartet werden.
   Es wurden keine Abhängigkeiten installiert.
2. Der konfigurierte lokale `DOWNLOAD_DIR` existiert nicht. `export-zips` bricht deshalb schon
   vor Login ab (`learnweb_sync.py:2714`). Der Export ist ein separater Legacy-Backup-Pfad.
3. Der Transkriptions-LaunchAgent ist weder installiert noch geladen. Die syntaktisch gültige
   Vorlage ist keine laufende Automatik. Die lokale DB enthält 5 fertige und 15 fehlgeschlagene
   Transkriptionen; alle 15 scheiterten historisch beim Download mit jeweils einem Versuch.
4. Das lokale Ressourcenmanifest enthält 924 Zeilen: 215 `synced`, 687 `new`, 17 `deferred`,
   3 `removed`, 2 `error`. Letzte Ressourcensichtung: 19.05.2026; letzter Transkriptstand:
   20.06.2026. Diese Zahlen beschreiben **nicht** das Railway-Volume.
5. README, Architekturtext und `.env.example` enthielten falsche bzw. überholte Zusagen zu
   Produktion/Testing, Mo–Fr-Zeitplan, Dry-Run und Crash-Recovery. Diese Aussagen sind in der
   Dokumentation korrigiert. Der fehlende AGENTS-Einstieg und die Roadmap-Pflege wurden ergänzt.

## Live-Daten und Zugriffsgrenzen

| Quelle | Aktueller Beleg | Grenze |
|---|---|---|
| LearnWeb | Connector-Kursliste, echter Login des Projektskripts und Parsing aller vier neuen Semesterkurse erfolgreich | Kein vollständiger Download aller Lehrmaterialien |
| Notion-Kursverwaltung | Vollständige Abfrage: 25 Zeilen, `has_more=false`, 8 aktive Zeilen | Keine Aktivierungen oder Kursanlage |
| Learnweb Inhalte | Aktuelle DB und alle vom Sync benötigten Property-Typen vorhanden, einschließlich `Ziel-URL`, `Quelle` und Kursrelation | Connector-Zugriff belegt nicht die Schreibrechte des separaten Skript-Tokens |
| Meeting-Notizen | Title-, Quelle-, Status-, Kategorie- und Relationsfelder passen; `Uni/Vorlesung` und `Erledigt` existieren | Kein neues Transkript geschrieben oder Whisper-Modell ausgeführt |
| Letzter Inhaltsbeleg | Notion-Seite [Terminzuteilung Einsichtnahme](https://app.notion.com/p/3f3bf244cadc81b5b5b5c6a4fbba831d), erstellt 08.10.2026 06:06 UTC, cmid `4013308`, Semester `WS 26/27` | Belegt einen vorhandenen Inhalt, nicht Vollständigkeit oder aktuellen Schedulerbetrieb |
| GitHub / Railway | Deployment #5140119136 meldet `success` vom 21.06.2026, SHA `825c13d`; GitHub-Webhook-Secretnamen sind vorhanden | Kein heutiger Health-/Log-/Volume-Nachweis; Secretwerte wurden nicht gelesen |

Lokaler HEAD: `6a25a1d`. Nach `git fetch` liegt `origin/main` zwei Commits weiter auf
`825c13d`. Beide haben denselben Dateibaum; der Checkout wurde nicht umgestellt.
Die offene PR-Liste ist leer. Die bestehenden Issues [#7](https://github.com/tbmn1996/learnweb_sync/issues/7)
und [#8](https://github.com/tbmn1996/learnweb_sync/issues/8) wurden gelesen und in der Roadmap
verlinkt, nicht verändert. Die vorhandene Backfill-Spezifikation ist noch nicht implementiert.

Die Railway-CLI scheitert aktuell mit `invalid_grant`/`Unauthorized`. Das katalogisierte
Railway-Credential ist technisch vorhanden, gehört aber zu **R-Factory** und wurde für dieses
private Projekt nicht verwendet. Eine erneuerte Anmeldung am richtigen Konto ist der konkrete
Zugangsschritt für Live-Logs und Volume-Prüfung; es wurde kein Login-Workaround eingerichtet.

Im Notion-DB-Directory existieren Einträge für alle drei Ziel-DBs. Ihre Prüfstände sind Juni
2026. Für Learnweb Inhalte existiert außerdem ein zweiter Eintrag ohne DB-Link. Die gelesene
Inhalts-DB hat keine Beschreibung mit DOKU-Link. Diese externen Dokumentationsabweichungen
wurden nur festgehalten; keine Directory-/Schema-Mutation im Projektcheck.

## Testbelege

Alle Testausführungen liefen über `testlauf`. Die Fehlerproben benutzen Mocks, isolierte
Subprozesse und temporäre bzw. In-Memory-Datenbanken. Kein produktiver Schreibpfad wurde
ausgeführt. Die Prüfskripte liegen außerhalb des Repos unter `/tmp/` und wurden nicht in die
reguläre Suite aufgenommen.

| Prüfung | Ergebnis | Belegordner unter `~/.local/state/testlauf/belege/` |
|---|---|---|
| Vollständige vorhandene unittest-Suite | 164 bestanden | `20261012-142805-auftrag-266e` |
| Echter Projekt-Login, Kursliste, vier Kursseiten | PASS | `20261012-143046-auftrag-7f92` |
| Worker-Suite und Plist-Syntax, unabhängiger Prüfer | 88 bestehende Tests und Plist bestanden; Teilmenge der 164 | `20261012-143235-learnweb-transcription-audit-dea3` |
| Unabhängige Erfolgsmelde-Proben | 2 fehlgeschlagene Anforderungen, 1 bestandene SystemExit-Probe | `20261012-143534-learnweb-audit-regressions-9d8c` |
| Ergänzende Gegenproben | 4 fehlgeschlagene Anforderungen; 1 bestandene Widerlegung des TypeError-Verdachts | `20261012-144104-auftrag-2927` |
| Dokumentation des ursprünglichen Prüflaufs | PASS; lokale Links/Sprungmarken, AGENTS-Symlink, unveränderte aktive Konfiguration und `git diff --check` | `20261012-144552-auftrag-c1a2` |

Der globale Claudex-Integritätscheck bestand zunächst als direkter Integritätsaufruf
mit Exit 0 (10 Prüfungen); dieser erste Aufruf war kein `testlauf`-Beleg. Der erste DSH-Reviewer
wiederholte ihn über `testlauf`: `/tmp/learnweb-dsh-review/belege/20261012-145429-dsh-review-integrity-9ae5/`.
Er prüft das globale Setup. Der projektspezifische relative Link `AGENTS.md -> CLAUDE.md`
wurde zusätzlich durch den Doku-Check geprüft. Ein gemeinsames Doc-Watcher-Prüfprotokoll
war im Git-Verzeichnis nicht eingerichtet. Abschlussprüfungen nach den DSH-Korrekturen
stehen in [DSH-REVIEW.md](DSH-REVIEW.md).

Zusätzliche fehlgeschlagene Proben sind bestätigte Auditbefunde, keine neu eingebauten Fehler.
Flask/APScheduler wurden für Funktionsisolation durch kleine Stubs ersetzt. Der echte
Scheduler-Trigger wurde nicht lokal ausgeführt; seine Wochentagssemantik ist durch die
Herstellerdokumentation belegt. Neue Live-Schreibrechte, Wiederanlauf nach echtem Remote-Timeout,
vollständige Transkriptqualität und Cloud-Backup-/Restore bleiben ungeprüft.

Prüfskripte: `/tmp/learnweb-readonly-probe.py`, `/tmp/learnweb-audit-regressions.py`,
`/tmp/learnweb-additional-probes.py`. Vollständige Ausgaben stehen jeweils in `befehle/01.log`.
Die zentrale Belegablage hat eine Aufbewahrungsfrist; der Befundtext und die Code-Referenzen
in diesem Bericht bleiben der dauerhafte Projektbeleg.

## Prüfverfahren und Übergabe

Drei abgeschlossene Headless-Codex-Läufe auf Luna/max trennten Sync-Audit, Worker-Audit und
unabhängige Fehlerproben. Laufakten: `~/.codex-headless/runs/run.6m6Tda`, `run.vZbt4f`,
`run.TrMnfH`. Entscheidende Befunde wurden am Code, durch Live-Lesezugriffe oder eigene
Gegenproben geprüft. Der Wrapper meldete für den initialen Titelreadback `NAMING_UNSUPPORTED`
(erste echte Eingabe nicht belegt); die fachlichen Läufe sind vollständig abgeschlossen.

Bestehende Lösung bleibt maßgeblich: Python-Sync, SQLite, Notion-Integration, APScheduler und
lokaler Worker wurden geprüft. Kein Eigenbau, kein neues Paket und kein Architekturwechsel
wurde vorgeschlagen oder umgesetzt. Ein vollständiger Graphify-Neuaufbau war für die direkte
Prüfung dieses kleinen Repos nicht nötig; ein bestehender Graph war nicht vorhanden.

Für die spätere Wiederinbetriebnahme sind in dieser Reihenfolge zu entscheiden:

1. Richtigen Railway-Zugang wiederherstellen und den heutigen Service-/Volume-Stand lesen.
2. Benötigte Semesterkurse bestimmen und ihre Anlage/Aktivierung samt Mapping freigeben.
3. Die bestätigten Sync-/Betriebsfehler gezielt beheben und unabhängig nachprüfen lassen.
4. Einen produktiven, begrenzten Sync inklusive Notion-Readback und Wiederholung abnehmen.
5. Den lokalen Transkriptionspfad separat nach Klärung von Dry-Run und Recovery abnehmen.

Diese Liste ist eine Übergabe offener Punkte, keine Umsetzungsfreigabe. Der aktuelle Auftrag
endet mit Prüfung und Dokumentation.

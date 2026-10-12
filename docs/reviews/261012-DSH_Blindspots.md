> Unveränderter Bericht einer unabhängigen Reviewer-Instanz. Er enthält auch später verworfene oder eingeschränkte Aussagen. Maßgeblich ist die [Gegenprüfung und Einordnung](../DSH-REVIEW.md).

# Blindspot-Zweitprüfung learnweb_sync (DSH Flash, reasoning=max)

Stand: 12.10.2026, HEAD `6a25a1d` (Dateibaum identisch zu `origin/main` `825c13d`).
Auftrag: nur prüfen; keine Repo-, Notion-, DB- oder Prozessänderung. Eigene Proben
ausschließlich über `testlauf` mit Belegwurzel `/tmp/learnweb-dsh-blindspots/belege`.
Vorgehen: erst README/PLAN/Code (ohne `docs/PRUEFBERICHT.md`), Kandidaten gebildet,
dann isolierte Offline-Gegenproben, erst danach den vorhandenen Prüfbericht gelesen.

Offline-Beleg dieses Laufs:
`/tmp/learnweb-dsh-blindspots/belege/20261012-145614-dsh-blindspots-probe1-2bec/`
(Skript `/tmp/learnweb-dsh-blindspots/probe.py`, Log `befehle/01.log`, `beleg.json`).

---

## Neue materielle Befunde

### B1 — `run` beendet sich mit Exit 2, wenn ein aktiver Kurs nur nicht-pushbare Typen hat; `server.py` zählt das als Sync-Fehler und eskaliert bis zum Scheduler-Tod

**Priorität: Hoch.**
**Ort:** `learnweb_sync.py:2619-2629` (Exit 2), `learnweb_sync.py:1664-1677` (Auslöser),
`server.py:138-147` (returncode != 0 → `_consecutive_failures += 1`), `server.py:160-166`.

**Auslöser:** Ein Kurs mit `SyncContent=true`, der Aktivitäten hat, aber keine der vier
pushbaren Typen (`resource`, `folder`, `url`, `page`). Genau diese Verteilung ist im
Projektbestand als realer Aktivitätstyp belegt: `zoom`, `forum`, `quiz`, `feedback`,
`opencast`, `assign`, `scorm`, `ratingallocate`, `moodleoverflow`, `choice`,
`questionnaire`, `subsection` (lokales Manifest, z. B. `AFW-2026_1` mit `zoom=1`,
`quiz=5`, `forum=2`, `feedback=1`; Testfixture `tests/test_learnweb_sync.py:2977-2984`
mit `{"forum": 7, "quiz": 5, "zoom": 10}`). `cmd_scan` sammelt solche Kurse in
`tracked_only_courses`, `cmd_run` beendet nach `cmd_push` mit `sys.exit(2)`.
Kein lokaler Kurs ist derzeit *ausschließlich* aus diesen Typen zusammengesetzt – das ist
eine Momentaufnahme, keine Strukturaussage; ein Kurs, der nur Ankündigungsforum, Zoom und
Quiz enthält (typisch für die ersten Semesterwochen oder reine Übungskurse), löst den Pfad
sofort aus.

**Nutzerfolgeschaden:** Ein völlig normaler Semesterzustand (Kurs existiert, hat nur Forum/
Quiz/Zoom/Opencast) wird als Fehlschlag gewertet. `server.py` erhöht den globalen
`_consecutive_failures`-Zähler, der nur bei Exit 0 zurückgesetzt wird. Nach drei solchen
Läufen ruft der Scheduler-Thread `sys.exit(1)` auf; nach dem bereits belegten Thread-Verhalten
aus `PRUEFBERICHT.md` (Abschnitt 2) stirbt damit der APScheduler-Job, während der Gunicorn-
Worker und `/health` (HTTP 200, `sync_running:false`) weiterleben. Ab dann gibt es bis zum
nächsten Deployment **keinen automatischen Sync mehr**, und kein Signal zeigt das an. Für den
Kurs selbst ist das Verhalten sogar korrekt – die Strafe trifft den gesamten Dienst.
Zusatzaspekt: `scan`/`push` haben zu diesem Zeitpunkt bereits geschrieben; Exit 2 erscheint
nach vollständigem Lauf, der Webhook-Aufrufer bekam vorher 202.

**Beleg/Probe:** eigener isolierter Lauf (`probe.py`, Abschnitt A):
`cmd_run` mit einer einzigen tracked-only-Kursmap → Exitcode 2, `cmd_push` wurde aufgerufen;
Nachbau der `server.py`-Zählung → `failures_after_three_exit2=3 threshold_reached=True`.
Deckt sich mit dem bestätigten Befund „sys.exit im Thread bewegt den Prozess nicht“.

**Einordnung:** Der vorhandene Bericht nennt Exit 0 als Problem (Fehler werden verschluckt),
aber nicht den umgekehrten Fall: ein *gewollter* Nicht-Erfolg wird als Betriebsfehler gezählt.
**Empfehlung:** Dokumentation kann den Zustand nur beschreiben; die Trennung
„Kurs ohne pushbare Inhalte“ (Hinweis, Exit 0) von „Lauf fehlgeschlagen“ braucht spätere
Implementierung. Bis dahin: Kurs entweder deaktivieren oder die Konsequenz im Betrieb
bewusst akzeptieren. Dokumentkorrektur allein genügt nicht.

---

### B2 — Fehlt ein `COURSE_MAP`-Eintrag für einen neuen Semesterkurs, bleibt `Kurs` dauerhaft leer; kein Lauf und kein CLI-Befehl repariert das

**Priorität: Mittel-Hoch.**
**Ort:** `learnweb_sync.py:1278-1282` (Setzen nur bei Treffer),
`learnweb_sync.py:1302-1316` (Property-Aufbau), `learnweb_sync.py:2114` (`resource`),
`:2309` (`url`), `:2367` (`page`) → `skipped` bei vorhandener `notion_id`;
`:2268-2274` (`folder` → `notion_update_lw_page` ist der einzige Reparaturpfad).

**Auslöser:** Neuer Semesterkurs wird synchronisiert, sein Shortname (z. B. `AFWFW-2026_2`)
fehlt in `COURSE_MAP`. Das ist der Regelfall beim Semesterwechsel, weil `COURSE_MAP` eine
Railway-Umgebungsvariable ist und die neuen Shortnames erst nach dem ersten `sync-courses`
bekannt sind. `_build_lw_page_properties` lässt die Property dann still weg – kein Log,
keine Warnung, kein Platzhalter.

**Nutzerfolgeschaden:** Alle in diesem Lauf angelegten `resource`/`url`/`page`-Seiten haben
leeres `Kurs`. Notion-Ansichten/Custom-Agents, die auf `Kurs` filtern, finden die neuen
Materialien nicht – obwohl sie in der DB stehen. Wird `COURSE_MAP` später nachgezogen,
korrigiert das nur künftige `folder`-Updates; `resource`, `url` und `page` werden nie wieder
angefasst (`skipped` wegen `notion_id`). Es gibt außerdem keinen CLI-Befehl, der Properties
bestehender Seiten nachzieht. Korrektur nur manuell in Notion oder per kompletter Neuanlage.
Der vorhandene Bericht hat den `COURSE_MAP`-Zustand dokumentiert („Feld bliebe leer“), aber
nicht die Permanenz und die fehlende Reparaturmöglichkeit über die vorhandenen Laufwege.

**Beleg/Probe:** `probe.py` Abschnitt B: ohne Mapping `Kurs` nicht in den Properties, mit
Mapping vorhanden; keine Warnung im Pfad. Zusätzlich Codekette `skipped`-Guards oben.

**Empfehlung:** Dokumentation kann den Punkt schärfen („einmal leer = dauerhaft leer“);
echte Abhilfe wäre ein Warn-Log bei fehlendem Mapping plus ein Property-Backfill.
Dokumentkorrektur genügt allenfalls als Warnung, nicht als Behebung.

---

### B3 — `transcribe --cmid/--course` verlangt `SyncContent=true`; ohne Flag läuft nichts, Exitcode ist trotzdem 0

**Priorität: Mittel.**
**Ort:** `learnweb_sync.py:3312-3325` (Filter + Warnung + `return`),
Gegenstück `learnweb_sync.py:3188-3201` (`--url`-Pfad ohne diesen Filter).

**Auslöser:** Aufruf `transcribe --cmid <ID> --force` (oder `--course X`) für einen Kurs, der
in KurseLearnWeb `SyncContent=false` hat. Das ist typisch für Kurse, die nur Aufzeichnungen
liefern oder für den Datei-Sync bewusst deaktiviert sind. `--cmid`/`--course` sind als
Eingrenzung dokumentiert, wirken aber nur *innerhalb* der SyncContent-Menge.

**Nutzerfolgeschaden:** Thomas fordert ausdrücklich genau eine Aufzeichnung an, bekommt nur
`WARNING Keine passenden Kurse mit SyncContent=true gefunden.`, keinen Prozess-Exit-Code und
keinen `print`-Abschluss (der `return` liegt vor der Summary). In Automatisierung (launchd,
Skripte) ist das von Erfolg nicht unterscheidbar; zusätzlich gibt es in der Datenbank keinen
neuen Transkript-Eintrag, an dem man den Nichtlauf erkennen könnte. Der vorhandene Bericht
beschreibt nur den umgekehrten Fall (Opencast-`--dry-run` schreibt beim Kursabgleich).

**Beleg/Probe:** `probe.py` Abschnitt C: `cmd_transcribe(dry_run=True)` ruft
`cmd_sync_courses` genau einmal und endet bei leerer Kursmap ohne Exit-Signal
(Logzeile `WARNING Keine passenden Kurse ...`). Kein Test in `tests/test_transcription.py`
prüft die SyncContent-Gatter für `--cmid`/`--course` (Grep auf `sync_content` ohne Treffer).

**Empfehlung:** Nicht als „Dry-Run“-Bug duplizieren, sondern als Bedien-/Erwartungslücke
dokumentieren: `--cmid` hebt die SyncContent-Auswahl nicht auf. Echte Abhilfe
(explizites `--cmid` unabhängig von SyncContent, klarer Exitcode) ist Implementierung.

---

### B4 — Dateien über 20 MB erzeugen für `resource` gar keine Notion-Seite; für `folder` fällt der ganze Ordner aus, ohne sichtbaren Nutzerhinweis

**Priorität: Mittel.**
**Ort:** `learnweb_sync.py:1797-1802` (`_read_download_response` → `None`),
`:1895-1902` (`_download_resource` → `retryable_error`, `download_unavailable`),
`:2129-2147` (`_push_resource_activity` markiert `error`, legt **keine** Seite an),
`:2223-2249` (`_push_folder_activity` bricht den kompletten Folder ab).

**Auslöser:** Eine `resource`-Datei, deren `Content-Length`/Größe 20 MB überschreitet
(Notion single-part). Für `folder` genügt eine einzige zu große Datei.

**Nutzerfolgeschaden:** Die Aktivität verschwindet vollständig aus Notion – es entsteht keine
Seite, kein Platzhalter, kein Body. Sie bleibt als `status='error'`, `retryable=1` in
`state.db` und wird bei jedem Lauf erneut heruntergeladen und erneut verworfen
(Endlos-Retry ohne Zustandswechsel; im lokalen Bestand stehen genau solche Altfälle, z. B.
`download_unavailable` in `WA-2026_1`). Sichtbar wird das nur für jemanden, der die DB oder
das Push-Log liest; der Lauf selbst endet mit „erstellt/Fehler“-Summary. Bei einem Ordner
verliert man zusätzlich alle kleineren, problemlos hochladbaren Dateien. Der vorhandene
Bericht nennt „Behandlung von Dateien über 20 MB ist gesondert festzulegen“, aber nicht,
dass für `resource` nicht einmal eine Seite entsteht und dass der Retry-Zustand dauerhaft ist.

**Beleg/Probe:** Codekette vollständig gelesen; `notion_upload_file` wird in diesem Pfad nie
erreicht. Kein Offline-Test dieses Laufs mit echten Bytes nötig, da der Zweig rein
größenabhängig ist (`learnweb_sync.py:1797`, `:1809`). Die beiden lokalen `error`-Zeilen
(`WA-2026_1`, `download_unavailable`, letzter Versuch 19.05.2026) sind konsistent mit dem
Pfad; in `logs/` liegt dafür kein passender Lauf mehr vor (nur Logs bis 24.05.2026), daher
wird die Größenursache nicht als lokal nachgewiesen behauptet.

**Empfehlung:** Dokumentation sollte den Nutzerverlust klar benennen („fehlt in Notion, nur in
der DB sichtbar“); Abhilfe (Seite ohne Anhang + Quelllink, Größenwarnung, Retry-Deckel)
ist Implementierung.

---

### B5 — `.env.example` lässt `STATE_DB_PATH` und alle Railway-Servervariablen aus; wer die Vorlage als „alle Variablen“ liest, betreibt den Sync ohne persistentes Manifest

**Priorität: Niedrig-Mittel.**
**Ort:** `.env.example` (vollständig), `learnweb_sync.py:107` (`STATE_DB_PATH`-Default neben
dem Skript), `server.py:35` (`/data/state.db` nur als Code-Default), README-Tabelle
(„`STATE_DB_PATH` ✓ Pflicht“), `docs/BETRIEB.md:34-37`.

**Auslöser:** Neuaufsetzen des Railway-Service (oder Löschen/Neuerstellen der Variablen) anhand
`.env.example`. In der Datei fehlen `STATE_DB_PATH`, `STATE_LOCK_PATH`, `SYNC_WEBHOOK_SECRET`,
`CRON_SCHEDULE`, `SYNC_SCHEDULE_TIMEZONE`, `SYNC_RUN_TIMEOUT_SECONDS`,
`SYNC_FAILURE_EXIT_THRESHOLD` und `LOG_DIR`.

**Nutzerfolgeschaden:** Ohne `STATE_DB_PATH=/data/state.db` schreibt `learnweb_sync.py` in
`<repo>/state.db` (im Container `/app/state.db`, `state.db` ist gitignored). Der Deploy
startet fehlerfrei, `/health` ist grün, der Sync läuft – aber das Manifest liegt im
Container-Dateisystem und ist beim nächsten Redeploy/Restart weg. Der nächste Lauf importiert
alle aktiven Inhalte erneut und legt für jede bereits vorhandene Notion-Seite eine neue an
(kein `notion_id` im Manifest, kein Duplikatsschutz in der Notion-DB). Das ist genau das
Duplikatszenario, das `BETRIEB.md` in Schritt 4 abprüfen lässt, hier aber ohne Vorwarnung
durch die Vorlage. Die README nennt die Variable in der Deployment-Tabelle, `AGENTS.md`
bezeichnet `.env.example` jedoch als „Vorlage für alle Env-Variablen“.

**Beleg/Probe:** Abgleich aller `os.getenv`-Namen im Code gegen `.env.example`
(26 Code-Namen, 8 fehlen vollständig). Kein Live-Nachweis der Railway-Variablen möglich
(CLI-Anmeldung abgelaufen), daher als Konfigurations- und Doku-Risiko geführt, nicht als
behaupteter Ist-Zustand.

**Empfehlung:** Dokumentkorrektur genügt: Railway-Variablen in `.env.example` als
kommentierten Block ergänzen (ohne Werte) und den Default-Pfad als nur-lokal kennzeichnen.

---

## Grenzen dieser Prüfung

- Kein Netz außer dem Textabruf der APScheduler-Doku (nicht erneut nötig); kein echter
  LearnWeb-/Notion-Zugriff, kein MCP. Railway-Variablen, Volume, Logs und Scheduler-Trigger
  bleiben ungeprüft (CLI-Anmeldung abgelaufen) – Aussagen dazu sind aus Code/README abgeleitet.
- Read-only-Analysen: `sqlite3 -readonly` auf der lokalen `state.db`, `logs/*.log`,
  Git-Objekte. Keine `.env`-Lektüre, keine Schreibzugriffe, keine Tests gegen Produktion.
- Die Offline-Suite wurde nicht erneut vollständig ausgeführt; die 164 bestehenden Tests
  gelten laut Erstprüfung als grün. Eigene Probe ist pass/fail-neutral (`PASS`, 1 Befehl).
- Priorisierung ist auf den Semesterkernpfad (Sync → Notion, Wiederholung, Transkription)
  bezogen, nicht auf eine allgemeine Security-Checkliste.

## Bereits bekannte und ausgeschlossene Befunde (nicht gedoppelt)

Aus `docs/PRUEFBERICHT.md` bestätigt und hier bewusst nicht erneut als neue Befunde geführt:
Exit 0 trotz Scrape-/Push-Fehlern; Seite ohne Datei nach Uploadfehler; leere Aktivitätenliste
markiert alles `removed`; APScheduler `1-5` = Di–Sa; `sys.exit` im Thread löst keinen
Railway-Neustart aus; `/health` prüft Notion/Webhook nicht; Opencast-`--dry-run` schreibt
beim Kursabgleich; verwaiste Zwischenzustände (`claimed`/`downloaded`/…) werden nicht
fortgesetzt; `--force`-Duplikatlücke bei Zähler 0.

Zusätzlich geprüft und **verworfen**:
- `upsert_activity` heilt `removed` → `new` beim Wiedererscheinen und `deferred` → `new`,
  sobald der Link da ist (Tests `:1229`, `:1285`); es gibt keinen dauerhaften Klebezustand
  durch einen Parser-Aussetzer. Der 20-MB-Fall (B4) ist davon getrennt, weil dort gar keine
  Seite entsteht.
- `_acquire_transcribe_lock` nutzt `flock` auf einem offenen File-Handle; ein harter
  Prozessabbruch gibt die Sperre frei, es bleibt keine Scheinsperre zurück.
- `--url` ist im Code bewusst als manueller Override ohne SyncContent-Gatter gebaut
  (`:3188-3201`); das ist eine dokumentierte Entscheidung, kein Befund.
- `_notion_request` behandelt 429 mit Retry-After und drosselt auf ~3 req/s; kein
  Ratenlimit-Befund.

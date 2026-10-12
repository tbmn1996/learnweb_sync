> Unveränderter Bericht einer unabhängigen Reviewer-Instanz. Er enthält auch später verworfene oder eingeschränkte Aussagen. Maßgeblich ist die [Gegenprüfung und Einordnung](../DSH-REVIEW.md).

# Adversariales Review des Prüflaufs learnweb_sync (WS 26/27)

Reviewer-Instanz: DSH, Max Reasoning, read-only. Kein MCP/Connector in dieser Instanz.
Geprüfter Stand: HEAD `6a25a1d`, 13 unveränderte Arbeitsstand-Einträge (`git diff --quiet HEAD origin/main` leer, getrackte Bäume identisch).
Eigene Belege: `/tmp/learnweb-dsh-review/belege/` (testlauf).

## Urteil

**Überwiegend bestätigt — Korrekturen nur an Belegführung, Priorisierung und an einer
unaufgelösten Kernfrage.** Kein bestätigter Befund des Prüflaufs ist falsch; die zentrale
Herstelleraussage (APScheduler 3, `1-5` = Di–Sa) habe ich gegen die offizielle Dokumentation
verifiziert. Die Zahlen des Berichts zum lokalen Bestand stimmen exakt. Die Doku-Arbeit liegt
im Rahmen des Bootstrap-Pflegevertrags und hat das Projektziel nicht verschoben.

Zu korrigieren sind: ein **veraltetes Belegzitat für die Doku-Prüfung** (Punkt 1), ein
**nicht belegter Integritätscheck-PASS** (Punkt 2), eine **unaufgelöste Spannung zwischen zwei
Live-Beobachtungen**, die über "nur Aktivierung" vs. "Wiederinbetriebnahme" entscheidet
(Punkt 3), die **Prioritäts-/Wirkungskalibrierung** (Punkt 4) und ein **ausgelassenes
Normalpfad-Risiko** (Punkt 5). Die Schlussfolgerung "noch keine Semesterabnahme" ist als
Verfahrensaussage richtig; als Aussage über die Funktionsfähigkeit ist sie zu grob (Punkt 4c).

Alle eigenen Proben liefen über `testlauf` (Belegwurzel `/tmp/learnweb-dsh-review/belege`,
da `~/.local/state/testlauf/belege` für diese Instanz nicht beschreibbar ist). Kein
Produktions-Subkommando (`scan`/`push`/`run`/`transcribe`), keine Repo-Änderung, keine
`.env`-Lektüre.

---

## Priorisierte materielle Reviewpunkte

### 1. Belegzitat der Doku-Prüfung zeigt auf den vorletzten Lauf — der gelieferte Stand ist ein anderer

**Datei:Zeile:** `docs/PRUEFBERICHT.md:137` (Zeile "Dokumentation, Links und Arbeitsumfang | … | `20261012-144515-auftrag-8764`").

**Gegenbeleg:** Das zitierte Beleg-JSON hält für `docs/PLAN.md` sha256 `225c5dc0…` und für
`docs/PRUEFBERICHT.md` sha256 `63dedb46…` fest. Ausgeliefert sind `6543fe40…` bzw.
`91f70f84…` — das ist der **spätere** Lauf `20261012-144552-auftrag-c1a2` (14:45:52 vs.
14:45:15). Zwischen beiden Läufen wurden also genau `docs/PLAN.md` **und der Prüfbericht
selbst** geändert. Damit deckt das zitierte PASS die gelieferten Fassungen nicht ab.
Die *Sache* ist trotzdem in Ordnung: ich habe den Doku-Check eigenständig gegen den
gelieferten Stand ausgeführt — `11 documentation files, 36 local links/anchors, AGENTS
symlink: OK` (Beleg `20261012-145611-dsh-review-doccheck-7afc`, PASS).

**Konsequenz:** Zitat auf `20261012-144552-auftrag-c1a2` ziehen (oder beide nennen). Sonst
sieht die maßgebliche Fassung von PLAN/Prüfbericht unbelegt aus, obwohl sie geprüft ist.

### 2. Der Integritätscheck-PASS hat keinen testlauf-Beleg; der Check prüft nur das globale Setup

**Datei:Zeile:** `docs/PRUEFBERICHT.md:139` ("Der gemeinsame Claudex-Integritätscheck bestand mit Exit 0 (10 Prüfungen)").

**Gegenbeleg:** Ich habe alle Beleg-JSONs mit Arbeitsverzeichnis `learnweb_sync` aufgezählt —
sieben Läufe (`266e`, `7f92`, `dea3`, `9d8c`, `2927`, `8764`, `c1a2`); **keiner** führt
`check-claudex-integrity.py` aus. Der Prüfbericht erklärt aber pauschal "Alle Ausführungen
liefen über `testlauf`" (`:125`). Inhaltlich stimmt die Zahl: ich habe den Check reproduziert,
`ERGEBNIS: alle 10 Prüfungen bestanden` (Beleg `20261012-145429-dsh-review-integrity-9ae5`).
Der Check liest jedoch ausschließlich `~/Scripts/CLAUDE.md` und `~/.claude/CLAUDE.md`
(`check-claudex-integrity.py:31-36`, Prüfungen 1–6) und damit **nicht** die Projektanleitung.
Der Bericht sagt das mit "der projektspezifische relative Link … wurde zusätzlich direkt
geprüft" (`:140-141`) korrekt — die Formulierung "gemeinsamer" Check sollte aber nicht als
Projektnachweis gelesen werden.

**Konsequenz:** Beleg für den Integritätslauf nachtragen und im Bericht als globalen
Setup-Check kennzeichnen; Projektnachweis bleibt der Doku-Check aus Punkt 1.

### 3. Die entscheidende offene Frage ist nicht gestellt: lebt der Railway-Scheduler oder nicht?

**Datei:Zeile:** `docs/PRUEFBERICHT.md:104` (Notion-Seite "Terminzuteilung Einsichtnahme",
erstellt **08.10.2026 06:06 UTC**, Semester WS 26/27) gegen `docs/PRUEFBERICHT.md:14-21`
(dieselben vier WS26/27-Kurse fehlen vollständig in KurseLearnWeb).

**Gegenbeleg / Mechanismus:** `learnweb_sync.py:2616` — `cmd_run` beginnt **immer** mit
`cmd_sync_courses` (`:1437`), das jeden unbekannten belegten Kurs anlegt (`:1482-1495`,
`:1515` ff.). Ein erfolgreicher Lauf um 06:00 UTC (Code-Default `server.py:46`) am 08.10.2026
hätte also die damals sichtbaren WS26/27-Kurse anlegen müssen. Beide Beobachtungen zusammen
sind nur auflösbar durch: **(a)** die vier Kurse wurden erst nach dem 08.10. sichtbar
→ Scheduler lebt, es fehlt nur die Aktivierung; oder **(b)** die Kursanlage scheitert still
— dafür existiert ein realer Pfad: `learnweb_sync.py:1519-1521` überspringt einen Kurs bei
Ladefehler mit `continue`, ohne ihn in `tracked_only_courses` aufzunehmen, also auch ohne
Exit 2 über `:2619-2629`. Der Bericht behandelt beide Fakten getrennt und lässt den Schluss
aus; unter "Live-Daten" steht nur "Kein heutiger Health-/Log-/Volume-Nachweis" (`:105`) und
"Railway-Variablen … ungeprüft" (`:37`).

**Konsequenz:** Diese Frage explizit als offenen Punkt führen. Sie ist die billigste
Unterscheidung (Railway-Logs der letzten Läufe, ohnehin Schritt 1 in
`docs/PRUEFBERICHT.md:169` und der Read-only-Einstieg in `docs/BETRIEB.md:89-90`)
und ändert die Semesterempfehlung grundlegend: **nur Aktivierung** statt
**Wiederinbetriebnahme**. Für den Leser ist der Unterschied größer als jeder der sieben
"Hoch"-Einträge.

### 4. Prioritäten und Wirkungssätze sind teils zu stark

**(a) Befund "leere Kursstruktur → alles `removed`" (`docs/PRUEFBERICHT.md:48`) lässt die
Selbstheilung weg.** Gegenbeleg: `learnweb_sync.py:388` setzt beim nächsten erfolgreichen
Scan desselben `cmid` `status = 'new'` für zuvor `removed`/`deferred`; `_mark_missing_activities_removed`
(`:2051-2059`) löscht keine Notion-Seite; und alle vier Push-Handler brechen bei vorhandener
`notion_id` ab (`:2114` resource, `:2217`/`:2268` folder, `:2309` url, `:2367` page). Es
entstehen also **keine** Duplikate und **keine** Löschungen in Notion. Bleibender Schaden:
noch nicht gepushte Aktivitäten werden bis zum nächsten guten Scan übersprungen und
Fehlerdiagnosen werden geleert (`:2054-2056`). Priorität "Mittel" ist damit vertretbar, der
Wirkungssatz liest sich aber wie Datenverlust.

**(b) Scheduler-Befund auf "Hoch" (`docs/PRUEFBERICHT.md:58`) ist zu hoch.** Der Effekt ist
eine Kadenzverschiebung (5 Läufe Di–Sa statt 5 Läufe Mo–Fr, Montagsinhalte kommen Dienstag),
kein Ausfall. Zusätzlich ist der **Live-Wert unbekannt** — `CRON_SCHEDULE` ist laut
`README.md:112` optional, der Code-Default (`server.py:46`) also nur ein Default. Die
APScheduler-Aussage selbst ist korrekt: die offizielle Doku sagt "APScheduler treats 0 as
Monday while the original crontab treats it as Sunday … cannot be changed in the 3.x series";
`1-5` = Di–Sa. "Mittel" entspricht der Nutzerwirkung.

**(c) Die Gesamtbewertung sollte präziser sein.** Alle vier Sync-Befunde
(`docs/PRUEFBERICHT.md:45-48`) liegen auf **Fehlerpfaden**; der Normalpfad ist live belegt
(echter Login, 28 Kurse, vier Kursseiten mit 99/13/4/34 Aktivitäten — Beleg
`20261012-143046-auftrag-7f92`, von mir gegen das Beleg-Log geprüft). Der einzige echte
Semester-Blocker ist die ausstehende Schreibfreigabe/Aktivierung der vier Kurse
(`:25-26`) plus `COURSE_MAP` (`:33-36`), also eine **Bedienerentscheidung**, kein Defekt.
Präziser: "Kernweg semesterfähig und live geprüft; Abnahme offen wegen (1) Freigabe der
Kursanlage/-aktivierung, (2) drei Fehler-/Sichtbarkeitspfaden, (3) separatem lokalem
Transkriptions-Worker." `README.md:7-11` sagt das bereits richtig ("noch nicht abgenommen");
in der Summe aus sieben "Hoch"-Zeilen — davon drei für den **optionalen** lokalen Worker —
kippt die Darstellung jedoch in "unzuverlässig".

**Konsequenz:** Wirkungssätze auf den belegten Mechanismus begrenzen; Scheduler auf "Mittel";
im Fazit die drei Ebenen trennen. Keine Funktionsänderung nötig.

### 5. Ausgelassenes Normalpfad-Risiko: 30-Minuten-Timeout beim ersten Vollauf

**Datei:Zeile:** `server.py:52` (`SYNC_RUN_TIMEOUT_SECONDS`, Default `1800`) und
`server.py:130-136` (`subprocess.run(..., timeout=TIMEOUT)`, killt das Kind). Der Prüfbericht
nennt "Wiederanlauf nach echtem Remote-Timeout" nur als ungeprüft (`:147`), ohne das Risiko
zu benennen; `README.md:114` listet die Variable ohne Warnung.

**Mechanismus (abgeleitet, nicht beobachtet):** Der erste Vollauf nach längerer Pause ist der
wahrscheinlichste Timeout-Kandidat — vier neue Kurse mit ~150 Aktivitäten, davon pushbar 43
(96289), 7 (94909), 1 (94906), 22 (96296) laut Beleg-Log `7f92`, jeweils Download und
Notion-Upload. Wird der Prozess zwischen Notion-Seitenerstellung (`learnweb_sync.py:2160-2175`)
und lokalem `_mark_activity_synced` (`:2177-2183`) gekillt, fehlt die `notion_id` lokal; da
alle Handler nur über die lokale `notion_id` deduplizieren, entsteht beim Folgelauf eine
**doppelte Notion-Seite**. Der Bericht deckt diesen Pfad nicht ab.

**Konsequenz:** Als Restrisiko benennen (Zeitgrenze für den ersten Lauf bewusst setzen oder
Lauf in Etappen), ohne Umsetzung in diesem Auftrag. Als abgeleitet kennzeichnen, nicht als
beobachtet.

---

## Bestätigte Befunde des Prüflaufs

Von mir am Code bzw. durch eigene Proben bestätigt:

1. **APScheduler 3: `0 6 * * 1-5` = Di–Sa** (`docs/PRUEFBERICHT.md:58`) — gegen die offizielle
   Herstellerdoku verifiziert (0 = Montag, in 3.x nicht änderbar).
2. **`sys.exit(1)` im Hintergrundthread beendet den Prozess nicht** (`server.py:166` in
   `_scheduled_sync` `:171` über `:181`, Webhook-Thread `:246`) — `docs/PRUEFBERICHT.md:59`.
   Die isolierte Subprozessprobe liefert `PROCESS_STILL_ALIVE` mit Exit 0 (Beleg `2927`).
   Vorbildlich: der Bericht trennt die bestandene Direktaufruf-Kontrolle ausdrücklich von
   dieser weitergehenden Aussage (`:62-64`) — das ist korrekt und war der Fehler des
   Vorreviewers.
3. **Resource-Uploadfehler → Seite ohne Datei, `status=synced`, dauerhaft übersprungen**
   (`learnweb_sync.py:2154-2158`, `:2177-2183`, `:2114`) — `docs/PRUEFBERICHT.md:47`.
4. **Scrape-Ausnahme → `continue` → Exit 0** (`learnweb_sync.py:1650-1656`; die einzige
   Nicht-Null-Sicherung `:2619-2629` greift nur bei nicht-leerer, aber unpushbarer
   Aktivitätsliste, `:1664`) — `docs/PRUEFBERICHT.md:45`.
5. **Push-Handler-Fehler → Exit 0** (`learnweb_sync.py:2587-2604`, Aufruf `:2618` ohne
   Auswertung). Die Zusammenfassung beginnt trotz Fehlern mit "✓" — `docs/PRUEFBERICHT.md:46`.
6. **Opencast-`--dry-run` schreibt über `cmd_sync_courses`** (`learnweb_sync.py:3311`, nicht
   durch `dry_run` gegated; nur `--url` zweigt vorher ab `:3283-3285`) —
   `docs/PRUEFBERICHT.md:70`. Für den Semesterfall besonders relevant: ein "Dry-Run" würde
   genau die vier fehlenden Kursseiten anlegen.
7. **`manifest.claim` akzeptiert nur `pending`/`failed`** (`transcription/manifest.py:153`) —
   liegengebliebene `downloaded`/`transcribed` werden dauerhaft übersprungen, ohne dass
   `attempts` steigt (`docs/PRUEFBERICHT.md:71`).
8. **`--force` leert nur bei `appended_block_count > 0`** (`learnweb_sync.py:3534-3537`) —
   `docs/PRUEFBERICHT.md:72`.
9. **`/health` prüft nur drei LearnWeb-Variablen und den DB-Elternpfad** (`server.py:84`,
   `:87-98`); leerer `SYNC_WEBHOOK_SECRET` deaktiviert die Auth (`server.py:222`) —
   `docs/PRUEFBERICHT.md:60`. Die Nichtbehauptung eines offenen Live-Endpunkts ist korrekt.
10. **`urljoin(base, None)` wirft keinen `TypeError`** (`learnweb_sync.py:732-734`) — der
    Vorreviewer-Verdacht ist zu Recht verworfen (`docs/PRUEFBERICHT.md:74-77`).
11. **Lokaler Bestand und lokale Umgebung exakt wie berichtet** (eigene read-only Prüfung):
    `resources` 924 = 687 `new`, 215 `synced`, 17 `deferred`, 3 `removed`, 2 `error`;
    `last_seen` 19.05.2026; `transcripts` 5 `done`, 15 `failed`, alle `attempts=1`, alle
    `failure_stage=download`, letzter Stand 20.06.2026 — deckt `docs/PRUEFBERICHT.md:87-91`.
    `.venv` ohne `flask`/`apscheduler`/`gunicorn`, mit `mlx_whisper`/`faster_whisper`/`yt_dlp`
    (`:81-83`); LaunchAgent weder installiert noch geladen, `plutil -lint` OK (`:86-88`);
    `downloads/` fehlt (`:84`); `.env` ist ungetrackt und über `.gitignore:1` ignoriert
    (kein Secret-Befund).
12. **Die Doku-Arbeit ist vertragskonform.** Die neuen Abschnitte "Dokumentation und Roadmap"
    (`CLAUDE.md`) und "Pflege und Form" (`docs/roadmap/README.md`) sind nach
    `bootstrap-project/SKILL.md:43-53` (Schritte 3 und 4, Abschlussfrage 2) **gefordert**,
    nicht Ausweitung; der Verweis erfolgt im Projekt, nicht auf die Skill-Dateien. Das
    Projektziel (`docs/PLAN.md`, Abschnitt "Kontext & Problem") ist unverändert, die
    historischen Lieferumfänge bleiben erhalten, `AGENTS.md -> CLAUDE.md` ist als relativer
    Symlink vorhanden. Kein Vorhaben wurde als umgesetzt behauptet.

## Widerlegte bzw. abzuschwächende Befunde

- **Widerlegt (zu Recht):** `urljoin(base, None)` → `TypeError` und daraus abgeleiteter
  Laufabbruch bei linkloser Opencast-Aktivität (`docs/PRUEFBERICHT.md:74-77`). Bestätigt.
- **Abzuschwächen:** Befund "leere Kursstruktur → alle Aktivitäten `removed`" (`:48`) — der
  Mechanismus stimmt, die Wirkung ist durch Selbstheilung (`learnweb_sync.py:388`) und die
  `notion_id`-Sperren der Handler begrenzt (Punkt 4a).
- **Abzuschwächen:** Scheduler-Priorität "Hoch" (`:58`) → "Mittel" (Punkt 4b).
- **Abzuschwächen:** Gesamtfazit — als Verfahrensaussage richtig, als Funktionsaussage zu
  grob (Punkt 4c). Kein Dokument behauptet wörtlich "unzuverlässig"; die Verdichtung
  entsteht erst aus der Summe der "Hoch"-Zeilen.

## Ausgelassene Kernrisiken

1. Die unaufgelöste Spannung aus Punkt 3 (Scheduler-Lebendigkeit vs. fehlende Kursanlage) —
   entscheidend für "Aktivierung" oder "Wiederinbetriebnahme".
2. Der stille Übersprungpfad in der Kursanlage (`learnweb_sync.py:1519-1521`): ein Kurs, dessen
   Seite nicht lädt, wird weder angelegt noch gemeldet noch über `tracked_only_courses` zu
   Exit 2 geführt. Das ist ein zweiter Mechanismus für Punkt 3 und in
   `docs/PRUEFBERICHT.md:45` nur über die Code-Referenz `:2616` angedeutet.
3. Die 30-Minuten-Zeitgrenze samt Duplikatfenster beim ersten Vollauf (Punkt 5).

## Bloße Grenzen (vom Prüflauf korrekt als ungeprüft ausgewiesen)

Railway-Livezustand (Logs, Variablen, Volume), Schreibrechte des Skript-Tokens, die
Connector-Aussagen (29 Dashboard-Treffer, `94743` nicht ladbar, 25 Kurszeilen/24 eindeutige
IDs, Schemata), Transkriptionsqualität sowie Cloud-Backup/Restore. Diese Punkte hat der
Prüflauf ausdrücklich als ungeprüft markiert und nicht als Befund ausgegeben — das ist
korrekt. Ich kann sie mangels Connector ebenfalls **nicht** verifizieren und habe dazu keine
Gegenbelege erfunden. Ebenfalls nicht lokal ausgeführt: der echte APScheduler-Trigger
(Wochentagssemantik nur über Herstellerdoku belegt) und ein echter Remote-Timeout.

## Eigene Testlaufbelege

| Prüfung | Ergebnis | Beleg |
|---|---|---|
| Vorhandene Suite `.venv/bin/python -m unittest discover -s tests -v` | **164 Tests, OK** (HEAD `6a25a1d`, 13 Einträge unverändert) | `/tmp/learnweb-dsh-review/belege/20261012-145429-dsh-review-suite-3b57` |
| Claudex-Integritätscheck | **alle 10 Prüfungen bestanden** | `/tmp/learnweb-dsh-review/belege/20261012-145429-dsh-review-integrity-9ae5` |
| Doku-/Linkprüfung `/tmp/learnweb-doc-check.py` gegen den gelieferten Stand | **PASS**, 11 Dokumente, 36 lokale Links/Sprungmarken, Symlink OK | `/tmp/learnweb-dsh-review/belege/20261012-145611-dsh-review-doccheck-7afc` |
| Read-only-Abfrage `state.db` (URI `mode=ro`) | Zahlen deckungsgleich mit `docs/PRUEFBERICHT.md:87-91` | keine Belegdatei (reine Leseabfrage) |
| Importprobe `.venv` (flask/apscheduler/gunicorn/whisper/yt-dlp) | deckungsgleich mit `:81-83` | keine Belegdatei |
| LaunchAgent-Status + `plutil -lint` | nicht installiert, nicht geladen, Plist OK | keine Belegdatei |
| APScheduler-3-Cron-Doku | bestätigt `1-5` = Di–Sa | https://apscheduler.readthedocs.io/en/3.x/modules/triggers/cron.html |
| GitHub-Issue #7 (Roadmap-Herkunft) | existiert, Titel deckt `docs/roadmap/youtube-retry.md` | https://github.com/tbmn1996/learnweb_sync/issues/7 |

## Nicht geprüfte Punkte dieses Reviews

- Sämtliche Connector-/Notion-Aussagen (Kurszeilenzahlen, Schemafelder, `Format=pdf`-Abfrage,
  Schreibrechte) — in dieser Instanz keine MCP-Tools; nur gegen die mitgelieferten Belege
  prüfbar, nicht live.
- Railway-Zustand (Logs, Variablen, Volume), Deployment-Historie, offene PR-Liste.
- Ob die vier WS26/27-Kurse am 08.10.2026 bereits sichtbar waren — genau die offene Frage aus
  Punkt 3; ohne Railway-Log oder Connector nicht entscheidbar.
- Echter Scheduler-Trigger, echter Remote-Timeout, echte Wiederaufnahme nach Abbruch.

## Kleinere Korrekturen (nicht priorisiert)

- Die `server.py`-Zeilenangaben in `docs/PRUEFBERICHT.md:58-60` treffen die Kommentarzeile
  über der Anweisung, nicht die Anweisung: `:44` statt `:46` (`CRON_SCHEDULE`), `:160` statt
  `:166` (`sys.exit`), `:239` statt `:246` (`Thread`), `:40` statt `:42`, `:83` statt `:84`,
  `:221` statt `:222`. Die `learnweb_sync.py`-Angaben sind überwiegend exakt.
- `docs/PRUEFBERICHT.md:46` verweist für den Push-Befund auf `:2587`/`:2618`; die
  Fehlerzählung steht in `:2588-2589`, die fehlende Auswertung in `:2619-2629`.
- `docs/PRUEFBERICHT.md:137` und `:135-136` summieren sich zu den im Auftrag genannten
  6 fehlgeschlagenen Anforderungen und 2 bestandenen Kontrollen; die Protokolle stimmen
  (`Ran 3 tests … failures=2`, `Ran 5 tests … failures=4`) — das ist konsistent und war nur
  zu verifizieren.

## Empfehlung an den Parent

Doku-only, in dieser Reihenfolge: (1) Belegzitat auf `c1a2` korrigieren, (2) Integritätslauf
als Beleg nachtragen und als globalen Setup-Check kennzeichnen, (3) die Scheduler-Frage aus
Punkt 3 als entscheidenden offenen Punkt in `docs/PRUEFBERICHT.md` und `docs/BETRIEB.md`
führen, (4) Scheduler-Priorität auf "Mittel" und die Wirkungssätze der Befunde 4 auf den
belegten Mechanismus begrenzen, (5) die Zeitgrenze aus Punkt 5 als Restrisiko benennen.
Keine Funktionsimplementierung, kein Commit.

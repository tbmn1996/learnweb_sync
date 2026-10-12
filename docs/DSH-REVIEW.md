# Unabhängige DSH-Reviews

**Ergebnis: Hauptbefunde bestätigt, Gesamturteil präzisiert, zusätzliche Grenzen ergänzt.**
Zwei frische Prüfer haben die Befunde und die Dokumentationsarbeit unabhängig kontrolliert.
Der erste prüfte den bisherigen Bericht adversarial, der zweite bildete zunächst eigene
Kandidaten am Code und verglich sie erst danach mit dem Bericht. Die abschließende Bewertung
unten bezieht sich auf die Audit-Code-Baseline `6a25a1d` und berücksichtigt die Gegenprüfung
durch den Hauptagenten; sie übernimmt die Rohberichte nicht ungeprüft. Spätere Codeänderungen
und ihr Live-Abnahmestand stehen im [Reparaturbericht](reviews/261012-Reparatur-Abnahme.md).

## Was jetzt gilt

1. **Der Leseweg funktioniert für die vier neuen Semesterkurse.** 164 bestehende Tests sind
   grün, auch beim unabhängigen Wiederholungslauf. Ein neuer vollständiger Import und heutiger
   Railway-Schedulerbetrieb sind dadurch noch nicht live bewiesen. Die frühere Chat-Formulierung
   „noch nicht zuverlässig einsatzbereit“ war zu pauschal; ein Dienstausfall ist nicht belegt.
2. **Die bestätigten Fehlerpfade bleiben bestehen.** Fehler können als Erfolg enden;
   Uploadfehler können Seiten ohne Datei hinterlassen. Die Dry-Run-/Recovery-Befunde des
   separaten lokalen Workers wurden bestätigt. Der Standardzeitplan Di–Sa ist als mittlere
   Abweichung von Mo–Fr eingeordnet, nicht als vollständiger Ausfall.
3. **Die Dokumentationspflege war im Scope.** Projektziel und vorhandene Vorhaben blieben
   erhalten. Ein veralteter Testbeleg wurde korrigiert; globaler Integritätscheck und
   projektspezifischer Link-/Symlink-Check sind nun ausdrücklich getrennt.

## Ergänzungen aus den Reviews

| Punkt | Nachprüfung und Ergebnis | Konsequenz |
|---|---|---|
| Abbruch zwischen Notion-Erstellung und SQLite-Checkpoint | `learnweb_sync.py:2171-2183`: isolierte Unterbrechung vor `_mark_activity_synced`, dann erneuter Aufruf erzeugt zwei simulierte Remote-Seiten. Railway hat standardmäßig 1800 Sekunden Timeout (`server.py:52`, `:130-135`). | Bestätigtes Duplikatfenster. Kein entsprechender Produktionsvorfall und keine erwartete Laufzeitüberschreitung der neuen Kurse belegt. |
| Dateien über 20 MiB | `_read_download_response` verwirft sie vor dem Upload. Zwei simulierte Resource-Versuche bleiben `retryable_error`, ohne Notion-Seite. Ein großes Folder-Mitglied verhindert auch die Neuanlage mit kleineren Dateien. | Eigenständige Größenbegrenzung, getrennt vom Uploadfehler nach erfolgreichem Download. Vorhandene Folder-Seiten bleiben erhalten; deren Aktualisierung fällt aus. Bei bekannter Content-Length wird der Dateibody gar nicht gelesen. |
| Später ergänztes Kursmapping | `resource`, `url` und `page` werden mit vorhandener `notion_id` übersprungen; Folder mit unverändertem Fingerprint ebenfalls. | Ein leeres `Kurs`-Select füllt sich durch nachträgliches `COURSE_MAP` nicht automatisch. Gezielter Property-Patch ist möglich; keine Neuanlage erforderlich. Die separate Kursrelation bleibt vorhanden. |
| Gezielte Transkription | `learnweb_sync.py:3312-3325`: `--cmid`/`--course` wirken innerhalb von `SyncContent=true`, ohne Treffer folgt Warnung und Return. | Bediengrenze in der README ergänzt. Dass dies aufzuheben sei, ist keine bestehende Anforderung; eine Verhaltensänderung braucht einen eigenen Auftrag. |

Die eigentliche Semesterfrage bleibt offen: Wurden die neuen Kurse erst nach dem letzten
erkennbaren Import vom 08.10. sichtbar, schlug ihre Anlage fehl oder nutzte der damalige Import
einen anderen Auslöser/Konfigurationsstand? Der Zeitpunkt einer Notion-Seite beweist nicht,
dass ein bestimmter Schedulerlauf erfolgreich war. Aktuelle Railway-Logs sind der nächste
Prüfschritt. Auch beim ersten Kursabgleich werden Ladefehler geloggt und übersprungen
(`learnweb_sync.py:1519-1521`), ohne den Gesamtfehlerstatus zu setzen.

## Verworfene oder eingegrenzte Reviewer-Aussagen

| Reviewer-Aussage | Gegenbeleg | Urteil |
|---|---|---|
| Zweitprüfer B1: Nach drei Exit-2-Läufen stirbt der Scheduler dauerhaft. | [APScheduler 3.11 `run_job`](https://raw.githubusercontent.com/agronholm/apscheduler/3.11.0/src/apscheduler/executors/base.py) fängt `BaseException` einschließlich `SystemExit` ab. Der [Pool-Executor](https://raw.githubusercontent.com/agronholm/apscheduler/3.11.0/src/apscheduler/executors/pool.py) behandelt den abgeschlossenen Job über den normalen Callback. Die Reviewer-Probe bildete nur den Fehlerzähler nach. | Dauerhafter Scheduler-Tod verworfen. Richtig bleibt: Exit 2 für einen aktiven Kurs ohne pushbare Inhalte erhöht den Fehlerzähler; der behauptete Prozessneustart erfolgt nicht. Das Exit-2-Verhalten ist bereits ausdrücklich getestet und dokumentiert, kein neu belegter Defekt. |
| Zweitprüfer B5: Ohne `STATE_DB_PATH` schreibt der reguläre Railway-Lauf in `/app/state.db`. | `server.py:35` verwendet `/data/state.db`; `:135` übergibt diesen Wert ausdrücklich an den CLI-Subprozess. Eigene Probe ohne Environment-Override bestätigt `/data/state.db`. | Fehlermechanismus für den dokumentierten Railway-Start verworfen. Die unvollständige Variablenvorlage war real und wurde ausschließlich durch Kommentare ergänzt. Der tatsächliche Volume-Mount bleibt ungeprüft. |
| Erstprüfer: Der vollständige Normalpfad sei bereits live bewiesen bzw. nur Aktivierung fehle. | Live geprüft wurden Login und Parsing, kein vollständiger neuer Notion-Schreibpfad. Einschreibungszeitpunkte und heutige Railway-Logs fehlen. | Aussage auf den Leseweg begrenzt. `COURSE_MAP` ist keine zwingende Importvoraussetzung. |
| Zweitprüfer B4: Lokale `download_unavailable`-Zeilen seien konkrete Größenfälle. | Keine passenden Laufbelege oder Dateigrößen liegen vor; derselbe Fehlercode hat mehrere Ursachen. | Nur der Codepfad und seine isolierte Reproduktion sind bestätigt, nicht diese Ursache der alten lokalen Fehler. |

Beim Leerparser-Befund ist ergänzt: Der nächste korrekte Scan reaktiviert die Manifestzeilen;
Notion-Seiten werden nicht gelöscht. Der initiale Fehlstatus kann die Übernahme verzögern,
belegt aber keinen dauerhaften Datenverlust.

## Herkunft und Belege

Die Konfiguration des DSH-Headless-Profils wurde vor beiden Starts gelesen:
`model=deepseek-flash`, `reasoningEffort=max`. Die [offizielle Modell-/Preisseite](https://api-docs.deepseek.com/quick_start/pricing/)
ordnete diesen Modellnamen am Prüftag DeepSeek-V4.1-Flash zu. Beide Starts am 12.10.2026
um 12:50 bzw. 12:53 UTC lagen außerhalb der dort dokumentierten Peak-Zeiten.
Der Wrapper meldet Modell/Effort nur als `default`; die konkrete Wahl ist deshalb über
die aktive Profilkonfiguration belegt, nicht aus diesem generischen Aktenfeld abgeleitet.

| Prüfung | Ergebnis | Quelle |
|---|---|---|
| Erstreview, 433 Sekunden | regulär fertig, Exit 0 | [Originalbericht](reviews/261012-DSH_Befundpruefung.md); Laufakte `~/.codex-headless/runs/run.gkqqpT` |
| Blindspot-Zweitreview, 309 Sekunden | regulär fertig, Exit 0 | [Originalbericht](reviews/261012-DSH_Blindspots.md); Laufakte `~/.codex-headless/runs/run.EwKDTh` |
| Unabhängige Bestandssuite | 164 Tests bestanden | `/tmp/learnweb-dsh-review/belege/20261012-145429-dsh-review-suite-3b57/` |
| Unabhängiger globaler Integritätscheck und Doku-Check | 10 globale Prüfungen bestanden; 11 damalige Dokumente / 36 Links geprüft | `/tmp/learnweb-dsh-review/belege/20261012-145429-dsh-review-integrity-9ae5/` und `20261012-145611-dsh-review-doccheck-7afc/` |
| Parent-Gegenproben | PASS: Verhalten von Abbruchfenster, Größenlimit, Folder und Railway-Pfad wie oben reproduziert | `~/.local/state/testlauf/belege/20261012-150015-learnweb-dsh-gegenproben-7560/` |

Ein PASS der Gegenproben bedeutet, dass das beobachtete Verhalten reproduziert wurde,
nicht dass die enthaltenen Fehler behoben sind. Das eigenständige Skript liegt unter
`/tmp/learnweb-dsh-review/parent-counterprobes.py`; keine Produktionsdatei wurde geändert.
Die [aus den ursprünglichen Connector-Antworten extrahierten Daten](reviews/261012-Live-Snapshot.json)
sind erhalten. Die DSH-Instanzen hatten keine Connector-Werkzeuge und haben diese Daten
nicht selbst live gelesen. Die Rohberichte enthalten abweichende Reviewer-Aussagen;
maßgeblich sind die Gegenbelege und Urteile in diesem Dokument.

Abschlusschecks der nachgezogenen Dokumentation laufen über `testlauf` mit der separaten
Belegwurzel `/tmp/learnweb-dsh-final-checks/`: lokale Links/Sprungmarken, AGENTS-Symlink,
unveränderte aktive Konfigurationswerte, keine getrackten Codeänderungen, `git diff --check`
und der globale Claudex-Integritätscheck. Der genaue Abschlussbeleg wird im Chat verlinkt.
Ein Verbrauchswert ist für beide DSH-Läufe nicht verfügbar (`keine passende dsh-Sitzung gefunden`);
keine erfundene Token- oder Kostenangabe.

## Ergebnisgrenze

Nur Dokumentation und Prüfbelege sind geändert. Funktionscode, aktive Konfiguration, lokale
Produktionsdatenbank und externe Systeme bleiben unverändert. Railway-Livezustand, vollständiger
neuer Notion-Import und Transkriptqualität sind weiter offen. Bestätigte Reparaturvorschläge
sind keine erteilte Umsetzungsfreigabe.

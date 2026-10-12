# learnweb_sync

Synchronisiert LearnWeb (Moodle, Uni Münster) automatisch mit Notion.
Neue `resource`-, `folder`-, `url`- und `page`-Aktivitäten werden erkannt und als Notion-Seiten angelegt. Unterstützte Originaldateien landen als Notion-Anhänge; abgelehnte Formate bleiben offene Importfehler und werden nicht als ZIP ersetzt.
Ein separater lokaler Worker transkribiert Opencast- und YouTube-Aufzeichnungen nach Notion.

**Prüfstand 12.10.2026:** 42 reguläre Semesterinhalte und ein Transkript sind live bestätigt.
Der Quellenbestand umfasst 80 ältere Aktivitäten mit drei bestätigten Legacy-Links. Insgesamt
sind 106 Originaldateien bytegleich geprüft. 18 von Notion abgelehnte
Originaldateien bleiben offen; bestehende Dubletten und manuelle Inhalte blieben erhalten.

Codecommit `ae1b0a0` ist gepusht und deployed; die sichere Offline-Suite besteht mit 210 Tests. Aktueller
Deployment-, Scheduler- und Abnahmestatus stehen im [Reparaturbericht](docs/reviews/261012-Reparatur-Abnahme.md).
[Ursprünglicher Prüfbericht](docs/PRUEFBERICHT.md) · [Unabhängige DSH-Reviews](docs/DSH-REVIEW.md) ·
[Betrieb und Semesterwechsel](docs/BETRIEB.md) · [Roadmap](docs/roadmap/README.md).

## Betrieb

**Primär:** Railway-Service mit internem Scheduler. Sollzeit ist Montag bis Freitag um 06:00 UTC
(`0 6 * * mon-fri`). Die abgeschlossene Deployment- und Scheduler-Abnahme steht im
[Reparaturbericht](docs/reviews/261012-Reparatur-Abnahme.md).
**Fallback:** GitHub Actions `workflow_dispatch` → ruft Railway-Webhook auf.

Das produktive Manifest liegt im persistenten Railway-Volume unter `/data`; die lokale `state.db`
ist ein separater Bestand. Letzter Lauf, aktive Variablen und verbleibende Providergrenzen werden im
[Reparaturbericht](docs/reviews/261012-Reparatur-Abnahme.md) geführt.

## Was das Skript macht

1. Loggt sich in LearnWeb ein
2. Erkennt alle belegten Kurse und legt neue Kurse in `KurseLearnWeb` an
3. Scrapt nur Kurse mit `SyncContent=true` auf neue Aktivitäten
4. Vergleicht mit dem Manifest (`state.db`) — nur echte Neuigkeiten werden gemeldet
5. Lädt neue Dateien herunter, aggregiert Folder-Dateien in einer Notion-Row und legt Notion-Seiten für `resource`, `folder`, `url` und `page` an
   `url`-Aktivitäten speichern ihre Zieladresse dabei in der Notion-Property `Ziel-URL`; nur Altbestand behält die frühere Bookmark-Darstellung im Seiteninhalt.
6. Meldet aktive Kurse explizit, wenn sie zwar Inhalte haben, aber keine aktuell pushbaren Inhaltstypen enthalten

## Lokales Setup (Entwicklung)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# .env mit eigenen Zugangsdaten befüllen
```

CLI-Befehle für lokale Läufe:

| Befehl | Was er tut |
|--------|-----------|
| `python learnweb_sync.py sync-courses` | Alle belegten Kurse erkennen und fehlende Notion-Kursseiten anlegen |
| `python learnweb_sync.py scan` | Kursabgleich inklusive möglicher Notion-Writes, danach aktive Kurse ins lokale Manifest scannen |
| `python learnweb_sync.py push` | Pushbare Inhalte aus aktiven Kursen nach Notion schreiben bzw. aktualisieren |
| `python learnweb_sync.py run` | `sync-courses` + `scan` + `push` in einem Schritt |
| `python learnweb_sync.py diagnose-resource-errors --limit 50` | Offene `resource`-Fälle read-only klassifizieren und nach Fehlergrund gruppieren |
| `python learnweb_sync.py export-zips` | Alle Kurse als ZIP-Backup herunterladen |
| `python learnweb_sync.py transcribe` | Opencast-Aufzeichnungen aller aktiven Kurse lokal transkribieren |
| `python learnweb_sync.py transcribe --cmid <ID>` | Eine Opencast-Aktivität transkribieren; alternativ mit `--course <ID oder Shortname>` eingrenzen |
| `python learnweb_sync.py transcribe --url <LearnWeb-URL>` | YouTube-Videos aus einer LearnWeb-Abschnittsseite transkribieren: Untertitel zuerst, sonst lokales Whisper |

Für den lokalen Transkriptions-Worker zusätzlich installieren:

```bash
pip install -r requirements-transcription.txt
brew install ffmpeg
```

`--url`, `--cmid` und `--course` sind gegenseitig exklusiv. YouTube-Aufrufe laufen ohne
LearnWeb-Cookies. Im aktuellen lokalen Code verwenden beide `--dry-run`-Pfade lesende
Kursabfragen und eine In-Memory-Datenbank; sie schreiben nicht nach Notion oder dauerhaft in
`state.db`. Medien können trotzdem geladen und mit Whisper verarbeitet werden. Diese Änderung
ist in Commit `5547703` enthalten; der Transkriptions-Worker läuft ausschließlich lokal.

`--cmid` und `--course` grenzen nur innerhalb der Kurse mit `SyncContent=true` ein.
Sie aktivieren keinen inaktiven Kurs; ohne passende aktive Aufzeichnung meldet der lokale
Code `no_matching_active_recording` maschinenlesbar und beendet sich mit Exitcode 3. Der
separate `--url`-Pfad hat dieses Kurs-Gate nicht.

`--force` ist nur zusammen mit einer der drei Eingrenzungen zulässig. Der aktuelle lokale Code
gleicht vorhandene Remote-Blöcke mit gespeicherten Eigentumsnachweisen ab; verifizierte eigene
Blöcke können ersetzt werden, manuell veränderte oder nicht eindeutig zuordenbare Inhalte bleiben
geschützt. Unterbrochene Zustände werden unter exklusiver Sperre wieder aufgenommen. Eine reale
Aufzeichnung wurde mit 12 von 12 Blöcken zurückgelesen; `--force` wurde nicht gegen bestehende
Nutzertexte ausgeführt. Weitere Ergebnisse stehen in der [Reparatur-Abnahme](docs/reviews/261012-Reparatur-Abnahme.md).
Die Railway-Instanz führt den Transkriptions-Worker nicht aus.

Lokal läuft der Sync direkt als CLI — kein `server.py` nötig.

Vorhandene Offline-Tests ausschließlich über den sicheren Runner ausführen. Er setzt
Platzhaltervariablen und blockiert ungemockte Netzwerkzugriffe; `unittest discover` direkt
umgeht diesen Schutz.

```bash
testlauf -C "$PWD" \
  -c '.venv/bin/python tests/run_offline.py'
```

Die Tests verwenden Mocks und temporäre Datenbanken; die Reparaturtests prüfen außerdem Flask
und APScheduler in isolierten Prozessen. Sie bestätigen weder produktive Notion-Schreibrechte
noch den tatsächlich bereitgestellten Railway-Scheduler.

## Railway-Deployment

### Voraussetzungen (einmalig im Railway-Dashboard)

1. **Volume** anlegen und unter `/data` an den Service hängen
2. **Env-Variablen** setzen (alle Pflichtfelder):

| Variable | Pflicht | Beispiel / Hinweis |
|----------|---------|-------------------|
| `LEARNWEB_URL` | ✓ | `https://sso.uni-muenster.de/LearnWeb/learnweb2` |
| `LEARNWEB_USERNAME` | ✓ | WWU-Kennung |
| `LEARNWEB_PASSWORD` | ✓ | WWU-Passwort |
| `NOTION_TOKEN` | ✓ | Notion Integration Token |
| `NOTION_COURSES_DB_ID` | ✓ | KurseLearnWeb-Datenbank |
| `NOTION_LW_DB_ID` | ✓ | Learnweb Inhalte-Datenbank |
| `CURRENT_SEMESTER_OVERRIDE` | optional | Nur für Backfills/Archivimporte, z.B. `WS 25/26` |
| `SEMESTER_TIMEZONE` | optional | Default `Europe/Berlin`; Semesterlabel wird automatisch berechnet |
| `COURSE_MAP` | empfohlen | JSON: `{"OR-2025_1": "OR"}` |
| `STATE_DB_PATH` | ✓ | `/data/state.db` |
| `STATE_LOCK_PATH` | ✓ | `/data/state.lock` |
| `SYNC_WEBHOOK_SECRET` | ✓ | Zufälliger Bearer-Token |
| `CRON_SCHEDULE` | optional | Code-Default `0 6 * * mon-fri`; tatsächlich aktiven Railway-Wert nach Deployment zurücklesen |
| `SYNC_SCHEDULE_TIMEZONE` | optional | `UTC` (Default) |
| `SYNC_RUN_TIMEOUT_SECONDS` | optional | `1800` (Default) |
| `SYNC_FAILURE_EXIT_THRESHOLD` | optional | `3` (Default) |

Zusätzlich muss in der via `NOTION_LW_DB_ID` referenzierten Learnweb-Inhalte-Datenbank die
URL-Property `Ziel-URL` existieren. Neue `modtype=url`-Einträge schreiben ihre Zieladresse in
diese Property; bestehende URL-Seiten behalten ihre frühere Bookmark-Darstellung.

3. **Deploy** starten — `railway.toml` im Repo übernimmt Start-Command, Health-Check und Restart-Policy.

### HTTP-Endpunkte

| Endpunkt | Methode | Beschreibung |
|----------|---------|-------------|
| `/health` | GET | Prüft Konfiguration sowie beschreibbare Datenbank-, Lock- und Statuspfade; meldet letzten Lauf. `content_verified=false`: kein Nachweis vollständiger Inhalte |
| `/webhook/sync` | POST | Manueller Sync-Start (`Authorization: Bearer <TOKEN>`) |

### Manueller Trigger (Fallback)

```bash
curl -X POST https://<deine-railway-url>/webhook/sync \
     -H "Authorization: Bearer <SYNC_WEBHOOK_SECRET>"
```

Oder per GitHub Actions: `workflow_dispatch` im `.github/workflows/sync.yml`.

## Projektstruktur

```
learnweb_sync/
├── .env.example              # Vorlage für alle Env-Variablen
├── .github/workflows/
│   └── sync.yml              # Manueller Fallback-Trigger (kein eigener Python-Lauf)
├── .python-version           # Python 3.12 (für Railpack)
├── docs/PLAN.md              # Architektur und historische Umsetzung
├── docs/BETRIEB.md           # Betrieb, Datenhaltung und Semesterwechsel
├── docs/PRUEFBERICHT.md      # Datierter Funktionscheck mit offenen Befunden
├── docs/DSH-REVIEW.md        # Historische unabhängige Reviews
├── docs/reviews/             # Reparaturabnahme und Originalbelege
├── docs/roadmap/README.md    # Bestehende Vorhaben und ihre Zustände
├── launchd/                  # Nicht automatisch aktivierte Vorlage für den lokalen Worker
├── learnweb_sync.py          # CLI + Sync-Logik
├── transcription/            # Discovery, Download, Transkription und Manifest-Logik
├── tests/                    # Unit-Tests für Sync und Transkription
├── server.py                 # Railway-Orchestrator (Flask + APScheduler)
├── railway.toml              # Railway-Konfiguration (Start-Command, Health-Check)
├── requirements.txt          # Railway- und Sync-Abhängigkeiten
├── requirements-transcription.txt  # Zusätzliche lokale Whisper-/yt-dlp-Abhängigkeiten
├── state.db                  # Manifest (gitignored; auf Railway via Volume persistent)
└── logs/                     # Logdateien (gitignored; lokal wenn LOG_DIR gesetzt)
```

## Details

Architektur und Integrationsregeln: [docs/PLAN.md](docs/PLAN.md).
`fix_modulhandbuch_inf2_mapping.py` ist ein historisches Migrationswerkzeug mit eigenem
`--apply`-Schreibpfad, kein Bestandteil des laufenden Syncs. Die Datei unter `.notion-pipeline/`
ist eine noch nicht umgesetzte Backfill-Spezifikation, kein ausführbarer CLI-Befehl.

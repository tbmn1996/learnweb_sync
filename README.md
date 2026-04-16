# learnweb_sync

Synchronisiert LearnWeb (Moodle, Uni Münster) automatisch mit Notion.
Neue Dateien, Folien und Ressourcen werden erkannt und als Notion-Seiten angelegt — inklusive PDF-Anhang, den Notion AI lesen kann.

## Betrieb

**Primär:** Railway-Service mit internem Scheduler (Mo–Fr 06:00 UTC).
**Fallback:** GitHub Actions `workflow_dispatch` → ruft Railway-Webhook auf.

Der Service läuft dauerhaft auf Railway und hält `state.db` in einem persistenten Volume unter `/data`. Kein Git-Branch-Workaround, kein lokaler Python-Lauf in CI.

## Was das Skript macht

1. Loggt sich in LearnWeb ein
2. Erkennt alle belegten Kurse und legt neue Kurse in `KurseLearnWeb` an
3. Scrapt nur Kurse mit `SyncContent=true` auf neue Aktivitäten
4. Vergleicht mit dem Manifest (`state.db`) — nur echte Neuigkeiten werden gemeldet
5. Lädt neue Dateien herunter und legt Notion-Seiten an (mit PDF-Anhang)

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
| `python learnweb_sync.py scan` | Nur Kurse mit `SyncContent=true` scrapen und neue Aktivitäten ausgeben |
| `python learnweb_sync.py push` | Nur neue Ressourcen aus aktiven Kursen herunterladen + Notion-Seiten anlegen |
| `python learnweb_sync.py run` | `sync-courses` + `scan` + `push` in einem Schritt |
| `python learnweb_sync.py export-zips` | Alle Kurse als ZIP-Backup herunterladen |

Lokal läuft der Sync direkt als CLI — kein `server.py` nötig.

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
| `CURRENT_SEMESTER` | ✓ | z.B. `SoSe 26` |
| `COURSE_MAP` | empfohlen | JSON: `{"OR-2025_1": "OR"}` |
| `STATE_DB_PATH` | ✓ | `/data/state.db` |
| `STATE_LOCK_PATH` | ✓ | `/data/state.lock` |
| `SYNC_WEBHOOK_SECRET` | ✓ | Zufälliger Bearer-Token |
| `CRON_SCHEDULE` | optional | `0 6 * * 1-5` (Default) |
| `SYNC_SCHEDULE_TIMEZONE` | optional | `UTC` (Default) |
| `SYNC_RUN_TIMEOUT_SECONDS` | optional | `1800` (Default) |
| `SYNC_FAILURE_EXIT_THRESHOLD` | optional | `3` (Default) |

3. **Deploy** starten — `railway.toml` im Repo übernimmt Start-Command, Health-Check und Restart-Policy.

### HTTP-Endpunkte

| Endpunkt | Methode | Beschreibung |
|----------|---------|-------------|
| `/health` | GET | Konfigurationscheck + Betriebsstatus (Railway Deploy-Gate) |
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
├── docs/PLAN.md              # Architektur- und Entwicklungsplan
├── learnweb_sync.py          # CLI + Sync-Logik
├── server.py                 # Railway-Orchestrator (Flask + APScheduler)
├── railway.toml              # Railway-Konfiguration (Start-Command, Health-Check)
├── requirements.txt
├── state.db                  # Manifest (gitignored; auf Railway via Volume persistent)
└── logs/                     # Logdateien (gitignored; lokal wenn LOG_DIR gesetzt)
```

## Details

Architektur, Entwicklungsphasen und Integrationsregeln: [`docs/PLAN.md`](docs/PLAN.md)

# learnweb_sync — Projektplan

> Dieses Dokument ersetzt die beiden Explorations-Docs (`learnweb_sync_exploration.md`,
> `learnweb_organizer_integration.md`) und ist die einzige Wahrheitsquelle für Architektur
> und Entwicklungsplan des Projekts. Es wird als `docs/PLAN.md` im Repo geführt.

> **Migration 2026-05-19 abgeschlossen:** Die früheren `(TESTING)`-DBs sind jetzt
> Produktion (`KurseLearnWeb`, `Learnweb Inhalte`); die ursprünglichen Produktions-DBs
> wurden zu `KurseLearnWeb (OLD)` / `Learnweb Inhalte (OLD)` umbenannt und logisch
> archiviert (Backlinks intakt für historische Daten). Property-Namen und DB-Titel
> tragen kein Suffix mehr.

---

## Kontext & Problem

Thomas studiert Wirtschaftsinformatik an der Uni Münster (FB 04) und nutzt Notion als
zentrales Studien-Management-System. Er hat ein ausgereiftes relationales Datenbankschema
aufgebaut (Modulhandbuch, Kurse, Prüfungen, Veranstaltungen) und nutzt Notion Custom Agents
für AI-gestützte Workflows.

**Das Kernproblem:** LearnWeb (Moodle-Instanz der Uni Münster) ist eine Black Box. Neue
Folien, Übungsblätter und Ankündigungen werden nicht aktiv kommuniziert. Inhalte werden nicht
gesucht, weil unklar ist ob sie existieren.

**Das Ziel:** LearnWeb-Inhalte automatisch erkennen wenn sie neu sind und als Notion-Seiten mit
PDF-Anhang in `Learnweb Inhalte` eintragen — so dass Notion AI und Custom Agents darauf
reagieren können.

---

## Betriebsmodell

**Primär: Railway-Service**

Der Service läuft dauerhaft auf Railway. Ein interner APScheduler (Mo–Fr 06:00 UTC) startet
den Sync automatisch. `state.db` liegt auf einem persistenten Railway-Volume unter `/data`.

```
[APScheduler intern, Mo–Fr 06:00 UTC]  |  [POST /webhook/sync, manuell]
                    │                                    │
                    └──────────────┬─────────────────────┘
                                   ▼
                        [server.py — Railway-Orchestrator]
                          threading.Lock (in-process Guard)
                          fcntl.flock (prozessübergreifend)
                                   │
                                   ▼
                        [Subprozess: learnweb_sync.py run]
                                   │
                    ┌──────────────▼─────────────────────┐
                    │           state.db                  │
                    │     (/data — Railway-Volume)         │
                    └─────────────────────────────────────┘
```

**Fallback: GitHub Actions `workflow_dispatch`**

Ruft per `curl` den Railway-Webhook auf. Führt kein eigenes Python aus, schreibt nicht in
`state.db`. Dient als Operator-Trigger wenn Railway manuell gestartet werden soll.

---

## Technische Rahmenbedingungen (geprüft)

| Frage | Ergebnis |
|-------|----------|
| Moodle Web Service API | ❌ Nicht verfügbar (404 auf `/webservice/rest/server.php`) |
| `/my/` Dashboard | ❌ 404, aber `/my/index.php` ✅ |
| HTML-Struktur Kursseiten | ✅ Bekannt, stabil, gut parsebar |
| Datei-Download per Session | ✅ requests.Session mit Cookies |
| Notion File Upload API | ✅ Vorhanden (`POST /v1/file_uploads`, bis 20MB single_part) |
| Railway Volume (SQLite) | ✅ Persistenter Speicher unter `/data`, Single-Replica |
| Railway Multi-Replica + Volume | ❌ Nicht kombinierbar — Single-Replica ist Voraussetzung |

---

## Was wir bauen

Ein Python-CLI-Tool (`learnweb_sync.py`) mit folgenden Kommandos:

```
python learnweb_sync.py sync-courses  # Alle belegten Kurse erkennen und fehlende Notion-Kursseiten anlegen
python learnweb_sync.py scan          # Nur Kurse mit SyncContent=true scrapen
python learnweb_sync.py push          # Neue Manifest-Einträge aktiver Kurse → Notion-Seite anlegen
python learnweb_sync.py run           # sync-courses + scan + push
python learnweb_sync.py diagnose-resource-errors  # Offene Resource-Fehler read-only klassifizieren
python learnweb_sync.py export-zips   # Backup: alle Kurse als ZIP herunterladen
python learnweb_sync.py transcribe    # Lokaler Worker für Opencast- und YouTube-Aufzeichnungen
```

Der produktive Einstiegspunkt auf Railway ist `server.py` (Flask + APScheduler), das
`learnweb_sync.py run` als Subprozess startet. `transcribe` läuft ausschließlich lokal auf
dem Mac und ist kein Teil des Railway-Prozesses.

---

## Architektur

### Gesamtdatenfluss

```
[APScheduler, Mo–Fr 06:00 UTC  |  POST /webhook/sync]
         │
         ▼
[server.py: threading.Lock.acquire(blocking=False)]
  └─ bereits aktiv → 409 Conflict / Scheduler überspringt
         │
         ▼
[Subprozess: sys.executable learnweb_sync.py run]
  fcntl.flock(STATE_LOCK_PATH) — sekundärer prozessübergreifender Guard
         │
         ▼
[1. Login]  sso.uni-muenster.de/LearnWeb/learnweb2/login/index.php
         │  CSRF-Token aus logintoken-Feld, POST mit Credentials
         │  → requests.Session mit MoodleSession-Cookie
         ▼
[2. Kursliste]  /my/index.php
         │  extrahiert: course_url, course_id
         ▼
[3. Discovery / Notion-Abgleich]
         │  bekannte Kurse per course_id aus Notion-URL erkennen
         │  unbekannte Kurse einmalig laden → shortname/LW-ID extrahieren
         ▼
[4. Kursseiten scrapen]  /course/view.php?id={course_id}
         │  nur für Kurse mit SyncContent=true
         │  extrahiert pro Aktivität: cmid, modtype, name, section, view_url
         ▼
[5. Manifest-Vergleich]  state.db (/data — Railway-Volume)
         │  neu = cmid nicht in Tabelle resources
         ▼
[6. Inhalt extrahieren / herunterladen]  (für pushbare Modtypes)
         │  resource → Datei
         │  folder   → mehrere Dateien
         │  url      → Ziel-URL
         │  page     → Klartext
         ▼
[7. Notion-Seite anlegen]
         │  POST /v1/file_uploads → file_upload_id(s)
         │  POST upload_url       → Datei(en) hochladen (≤20MB je Datei)
         │  POST /v1/pages        → Seite in "Learnweb Inhalte" anlegen
         │  PATCH /v1/blocks/...  → optionale Paragraph-Blöcke für `page`-Inhalte anhängen
         ▼
[8. Manifest aktualisieren]  notion_id + status='synced' in state.db
```

### Lokaler Transkriptionsfluss

```
[transcribe --cmid/--course]              [transcribe --url <LearnWeb-Seite>]
           │                                           │
           ▼                                           ▼
[Opencast-Episoden finden]                 [YouTube-Links cookie-frei finden]
           │                                           │
           ▼                                           ▼
[Medien laden + Whisper]                   [Untertitel, sonst Audio + Whisper]
           └──────────────────────┬────────────────────┘
                                  ▼
                 [transcripts-Zustand in state.db]
                                  ▼
          [Learnweb-Inhalt + Meeting-Notiz in Notion]
```

Der Worker beansprucht Aufzeichnungen atomar per SQLite und `flock`, persistiert jeden
Zwischenstand und kann nach Abbrüchen fortsetzen. Der stabile Schlüssel lautet
`{cmid}-{sha1(episode_id|media_url)[:12]}`. `--dry-run` hinterlässt weder Notion-Writes noch
dauerhafte Änderungen in `state.db`.

### HTML-Parsing (Kursseiten)

Jede Aktivität ist ein `<li data-for="cmitem" data-id="{cmid}">`:

```html
<li class="activity resource modtype_resource"
    data-for="cmitem"
    data-id="3857603">
  <div data-activityname="Vorlesung 1" data-region="activity-card">
    <a href=".../mod/resource/view.php?id=3857603">...</a>
  </div>
</li>
```

**Bekannte Aktivitätstypen:**

| modtype | Phase 1 | Phase 2+ |
|---------|---------|---------|
| `resource` | Metadaten im Manifest | Datei herunterladen + Notion |
| `folder` | Metadaten im Manifest | Eine Notion-Row mit mehreren Dateianhängen |
| `page` | Metadaten im Manifest | Klartext nach Notion-Paragrafen |
| `forum` | Metadaten im Manifest | Forum-Seite scrapen (Phase 5) |
| `url` | Metadaten im Manifest | Nur URL in Notion eintragen |
| `opencast` | Metadaten im Manifest | Separater lokaler `transcribe`-Pfad, nie Railway-`push` |
| `assign` | Metadaten im Manifest | Deadline in Notion (Phase 5) |
| `label` | Ignorieren | – |

### SQLite-Manifest (`state.db`)

```sql
CREATE TABLE IF NOT EXISTS resources (
    cmid             TEXT PRIMARY KEY,
    course_id        TEXT NOT NULL,
    course_name      TEXT NOT NULL,
    course_shortname TEXT,
    modtype          TEXT NOT NULL,
    name             TEXT NOT NULL,
    section          TEXT,
    view_url         TEXT,
    first_seen       TEXT NOT NULL,   -- ISO-8601 UTC
    last_seen        TEXT NOT NULL,
    file_hash        TEXT,
    file_name        TEXT,
    notion_id        TEXT,
    status           TEXT DEFAULT 'new'  -- new / synced / error / removed
);
```

Der lokale Worker ergänzt die Tabelle `transcripts`. Sie speichert Recording-Key,
Quellmetadaten, Claim-/Retry-Status, temporäre Pfade, Notion-Page-IDs und den Append-Fortschritt.
Die Schema-Initialisierung erfolgt nur im `transcribe`-Pfad, damit der Railway-Sync unverändert
bleibt.

### Notion-Integration

**Feldmapping beim Anlegen einer neuen Seite:**

| Notion-Feld | Typ | Quelle / Logik |
|-------------|-----|----------------|
| `Name` | title | `data-activityname` aus HTML |
| `Kurs` | select | shortname → `COURSE_MAP` JSON-Env |
| `Kurs-ID` | text | `course_id` |
| `Kategorie` | select | Heuristik aus Aktivitätsname |
| `Format` | select | Dateiendung (pdf/ipynb/py/pkl/zip) |
| `Quell-Semester` | select | automatisch aus Datum in `Europe/Berlin` (`SoSe`: 01.04.–30.09., `WS`: 01.10.–31.03.), optionaler Override via `CURRENT_SEMESTER_OVERRIDE` |
| `LW Download` | file | ein oder mehrere `file_upload`-Einträge nach Notion File Upload |
| `Ziel-URL` | url | extrahierte externe Zieladresse, nur für `modtype=url` |
| `Nr` | text | cmid |
| `Variante` | select | Original / Solution / Template / … |
| `KurseLearnWeb` | relation | Notion Page-ID aus KurseLearnWeb |

Bereits synchronisierte `url`-Seiten behalten ihren historischen Bookmark-Block im Inhalt.
Neue `url`-Seiten zeigen die Zieladresse ausschließlich in `Ziel-URL`; diese gemischte UI-Darstellung
ist akzeptiert und wird nicht automatisch backfilled.

**Notion File Upload Flow (2 Schritte):**

```python
# Schritt 1: Upload initiieren
resp = requests.post(
    "https://api.notion.com/v1/file_uploads",
    headers={"Authorization": f"Bearer {NOTION_TOKEN}",
             "Notion-Version": "2022-06-28"},
    json={"mode": "single_part", "content_type": "application/pdf"}
)
upload_id = resp.json()["id"]
upload_url = resp.json()["upload_url"]

# Schritt 2: Datei hochladen (POST, nicht PUT! + Notion-Version Header)
requests.post(upload_url,
              headers={"Authorization": f"Bearer {NOTION_TOKEN}",
                       "Notion-Version": "2022-06-28"},
              files={"file": (filename, file_bytes, "application/pdf")})
```

---

## Repo-Struktur

```
learnweb_sync/
├── .env.example              # Vorlage für alle benötigten Variablen
├── .gitignore
├── .github/
│   └── workflows/
│       └── sync.yml          # Manueller Fallback-Trigger (kein eigener Python-Lauf)
├── .python-version           # Python 3.12 (für Railpack)
├── README.md
├── requirements.txt          # requests, beautifulsoup4, python-dotenv, Flask, APScheduler, gunicorn
├── requirements-transcription.txt  # lokale Whisper-/yt-dlp-Abhängigkeiten
├── railway.toml              # Start-Command, Health-Check, Restart-Policy
├── learnweb_sync.py          # CLI + Sync-Logik
├── server.py                 # Railway-Orchestrator (Flask + APScheduler)
├── transcription/            # Discovery, Download, Transkription und Manifest
├── tests/                    # Unit-Tests für Sync und Transkription
├── launchd/                  # nicht automatisch aktivierte Worker-Vorlage
├── state.db                  # gitignored; auf Railway via /data-Volume persistent
├── downloads/                # gitignored; temporäre Dateien während eines Runs
├── docs/
│   └── PLAN.md               # dieses Dokument
└── logs/                     # gitignored; lokal wenn LOG_DIR gesetzt
```

---

## Entwicklungsphasen

### Phase 1 — Scraper + Manifest ✅ ABGESCHLOSSEN

**Deliverables:** `learnweb_sync.py` mit `scan`-Kommando, SQLite-Manifest, `sync-courses`-Kommando.

### Phase 2 — Datei-Download + Notion-Push ✅ ABGESCHLOSSEN

**Deliverables:** `push`-Kommando, `run`-Kommando, Fehlerbehandlung, Deduplizierung.

**Testergebnis (NEW DB, jetzt Produktion, WS 25/26):** 91 Ressourcen, 0 Fehler, Kurs 100 %, R Resource 35 %.

### Phase 3 — Railway-Deployment ✅ ABGESCHLOSSEN

**Deliverables:**
- `server.py`: Flask + APScheduler, Webhook, Health-Check, Parallelisierungsschutz
- `railway.toml`: Start-Command, Health-Check, Restart-Policy
- `.python-version`: Python 3.12 für Railpack
- `requirements.txt` um Flask, APScheduler, gunicorn erweitert
- `learnweb_sync.py` deploymentsicher gemacht: `STATE_DB_PATH` aus Env, Logging nur stdout ohne `LOG_DIR`
- `.github/workflows/sync.yml` auf reinen Webhook-Trigger umgebaut

**Railway-Setup (einmalig im Dashboard):**
- Volume unter `/data` anlegen und mounten
- Env-Variablen setzen (siehe README)
- Zwei neue GitHub Secrets: `LEARNWEB_SYNC_WEBHOOK_URL`, `SYNC_WEBHOOK_SECRET`

**Produktions-Switchover (abgeschlossen am 2026-05-19):**
- Aktuelle DB-IDs liegen in `.env`. Die DBs heißen jetzt `KurseLearnWeb` und
  `Learnweb Inhalte` (ehemals mit `(TESTING)`-Suffix); die früheren Produktions-DBs
  sind jetzt `KurseLearnWeb (OLD)` / `Learnweb Inhalte (OLD)` und nur noch für
  historische Backlinks aktiv.
- `COURSE_MAP` muss bei jedem Semesterwechsel um neue aktive Kurse erweitert werden.

### Lokaler Transkriptions-Worker ✅ ABGESCHLOSSEN

**Deliverables:**
- Opencast-Discovery und lokale Whisper-Transkription mit Crash-/Resume-sicherem Manifest
- YouTube-Discovery über LearnWeb-Seiten, Untertitel-Cascade und cookie-freier Audio-Fallback
- Idempotente Notion-Erstellung für Learnweb-Inhalt und Meeting-Notiz
- `--cmid`, `--course`, `--url`, `--limit`, `--force` und write-freier `--dry-run`
- Launchd-Vorlage für den lokalen Mac; keine Aktivierung durch das Repository

### Phase 4 — Notion-Button Trigger (optional, zurückgestellt)

Railway-Webhook löst dieselbe Funktion ab. Ein Notion-Button kann direkt auf den
Railway-Webhook-URL zeigen, sofern der Webhook-Auth-Header gesetzt werden kann.

### Phase 5 — Forum/Ankündigungen + Assignments (optional, Zukunft)

Scope erst definieren wenn Phase 3 stabil und im Einsatz.

---

## Kritische Integrationsregeln

1. **`cmid` ist Wahrheit** — nie Name oder URL als Identität.
2. **Nie lokal löschen** wenn etwas remote verschwindet — `status='removed'` setzen.
3. **Bestehende Notion-Seiten nur gezielt patchen**: `folder` darf über denselben `notion_id` aktualisiert werden; andere Typen bleiben initial-only.
4. **Single-Replica** — Railway-Volume erlaubt keinen Multi-Replica-Betrieb.
5. **`state.db` nie in main-Branch** — gitignored; Persistenz über Railway-Volume.
6. **Organizer-Skill bleibt extern** — Sync = remote→Notion. Organize = lokal→kuratiert.
7. **Transkription bleibt lokal** — keine Whisper-Abhängigkeiten und kein Opencast-Download auf Railway.
8. **Keine LearnWeb-Cookies an YouTube** — YouTube-Untertitel und -Audio laufen cookie-frei.

---

## Offene Punkte / Entscheidungen

- **Notion API Version**: Der Code ist auf `2022-06-28` gepinnt; eine Aktualisierung erfolgt separat.
- **Notion-Button für Webhook**: Notion-Buttons können POST senden — prüfen ob Railway-URL direkt nutzbar ist (Auth-Header?).

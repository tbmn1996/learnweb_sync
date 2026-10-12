# learnweb_sync — Architektur und Umsetzungsstand

> Dieses Dokument ersetzt die beiden Explorations-Docs (`learnweb_sync_exploration.md`,
> `learnweb_organizer_integration.md`) und beschreibt die Architektur
> des Projekts. Der aktuelle Stand bestehender Vorhaben wird in
> [roadmap/README.md](roadmap/README.md) gepflegt. Betrieb und Abnahme stehen in
> [BETRIEB.md](BETRIEB.md) und [PRUEFBERICHT.md](PRUEFBERICHT.md); der aktuelle
> Reparaturabnahme steht in [reviews/261012-Reparatur-Abnahme.md](reviews/261012-Reparatur-Abnahme.md).

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

Der vorgesehene Dauerbetrieb liegt auf Railway. Der Scheduler-Sollplan ist Montag bis Freitag
06:00 UTC; der Code-Default lautet `0 6 * * mon-fri`. Das produktive Manifest liegt im Railway-
Volume `/data`, getrennt von der lokalen `state.db`.

Codecommit `ae1b0a0` ist gepusht und deployed; 210 Offline-Tests bestehen. Er prüft Folder-Dateien
auch bei unveränderten Links auf Inhaltsänderungen und wiederholt lesende LearnWeb-GETs begrenzt;
Schreibvorgänge werden nicht wiederholt. Abgenommener Bestand, Schedulerstatus und verbleibende Grenzen
stehen im [Reparaturbericht](reviews/261012-Reparatur-Abnahme.md).

```
[APScheduler intern, konfigurierte Zeit] | [POST /webhook/sync, manuell]
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

### Reparaturabnahme

Der lokale Arbeitsstand verwendet inzwischen eine Gunicorn-App-Factory, validiert Notion-/Webhook-
Konfiguration und Laufzeitpfade, speichert den letzten Lauf neben `state.db` und liefert bekannte
Kurs-, Scan- und Pushfehler als fehlgeschlagenen Gesamtlauf zurück. Endgültige Providerblocker
werden als `incomplete` gemeldet; sie bleiben sichtbar, werden pro Quell-URL gespeichert und
bei Folgeläufen übersprungen. Sie lösen keinen Worker-Neustart aus. Andere technische Fehler
können nach Erreichen der Fehlerschwelle den Gunicorn-Worker beenden; der Railway-Master kann
ihn gemäß Restart-Policy neu starten. Der Code-Default ist `0 6 * * mon-fri`. Große Dateien
werden gestreamt und oberhalb 20 MiB per Multipart hochgeladen; nicht unterstützte Originalformate
bleiben als offene Anlagen.

Der Sync aktualisiert eigene Originaldateien nur mit Hashnachweis. Folder-Dateien werden auch
bei unveränderten Links auf Inhaltsänderungen geprüft. Lesende LearnWeb-GETs werden begrenzt
wiederholt; Schreibvorgänge nicht. Aktueller Byteabgleich und gezielte Folder-Abnahmen stehen im
[Reparaturbericht](reviews/261012-Reparatur-Abnahme.md).

---

## Technische Rahmenbedingungen (historischer Entwicklungsstand)

Diese Angaben stammen aus der bisherigen Implementierung. Sie sind kein vollständiger
Live-Nachweis für das laufende Semester; der datierte Prüfbericht trennt aktuelle Belege
von noch offenen Prüfungen.

| Frage | Ergebnis |
|-------|----------|
| Moodle Web Service API | ❌ Nicht verfügbar (404 auf `/webservice/rest/server.php`) |
| `/my/` Dashboard | ❌ 404, aber `/my/index.php` ✅ |
| HTML-Struktur Kursseiten | ✅ Bekannt, stabil, gut parsebar |
| Datei-Download per Session | ✅ requests.Session mit Cookies |
| Notion File Upload API | ✅ Vorhanden; 20 MiB Single-Part, zusätzlich Multipart im aktuellen lokalen Code. Workspace-Grenze gilt; `.ipynb` und `.pkl` werden abgelehnt. |
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
[APScheduler, konfigurierte Zeit  |  POST /webhook/sync]
         │
         ▼
[server.py: threading.Lock.acquire(blocking=False)]
  └─ bereits aktiv → 409 Conflict / Scheduler überspringt
         │
         ▼
[server.py hält fcntl.flock(STATE_LOCK_PATH)]
  └─ startet Subprozess: sys.executable learnweb_sync.py run
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
         │  POST upload_url       → Single-Part bis 20 MiB; darüber Multipart bis Workspace-Grenze
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

Der Worker beansprucht Aufzeichnungen atomar per SQLite und `flock` und persistiert
Zwischenstände. Der ursprüngliche Code-Stand nahm verwaiste Zustände nicht wieder auf.
Der aktuelle lokale Reparaturcode setzt sie nach exklusivem Lock fort und gleicht den
Remote-Body vor dem Append mit gespeicherten Blöcken ab. Beim Force-Ersatz werden nur
eindeutig eigene, unveränderte Blöcke gelöscht; manuelle Ergänzungen bleiben geschützt.
Commit `5547703` ist bereitgestellt. Eine reale aktive Aufzeichnung lieferte 12 von 12
zurückgelesenen Blöcken. Damit ist der normale Transkriptionspfad dieser Aufzeichnung live
bestätigt; Force und Wiederaufnahme auf bestehenden Nutzerinhalten bleiben durch isolierte
Tests abgedeckt und wurden nicht destruktiv live ausgeführt.
Der stabile Schlüssel lautet `{cmid}-{sha1(discriminator)[:12]}`;
`discriminator` ist die erste vorhandene Angabe aus Episode-ID, Medien-URL und Quell-URL.
CLI-Dry-Runs verwenden eine In-Memory-Datenbank. Im aktuellen lokalen Code bleiben sowohl
`--url` als auch Opencast-`--dry-run` gegenüber Notion lesend; Medienabruf und Whisper können
trotzdem laufen.

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
    status           TEXT DEFAULT 'new'  -- new / synced / error / deferred / removed
);
```

Das Schema oben zeigt die ursprünglichen Kernfelder. `init_db()` ergänzt unter anderem
`failure_reason`, `failure_detail`, `last_attempt_at` und `retryable`;
der Code ist für das vollständige Schema maßgeblich.

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

**Notion File Upload Flow:**

Das folgende Beispiel zeigt den Single-Part-Pfad bis 20 MiB. Commit `5547703` streamt größere
Originale in Multipart-Teilen bis zur abgefragten Workspace-Grenze. PDFs mit 50 und 61 MiB
sowie ein MOV mit 247 MiB wurden auf markierten Testseiten bytegleich zurückgelesen; die
Testseiten sind archiviert. `.ipynb` und `.pkl` werden vom Provider auch bei Multipart abgelehnt
und bleiben gemäß der freigegebenen Reparaturentscheidung offene Anlagen.

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

## Historische Entwicklungsphasen

Die folgenden Abschnitte erhalten die bisher dokumentierten Lieferumfänge und Ergebnisse.
„Abgeschlossen“ bezeichnet hier den historischen Implementierungsstand, keine erneute
Semesterabnahme. Bestätigte Restfehler stehen im [Prüfbericht](PRUEFBERICHT.md), bestehende
offene Vorhaben ausschließlich in der [Roadmap](roadmap/README.md).

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

**Historisch dokumentierter Lieferumfang:** Die folgenden Zusagen wurden im damaligen Plan
als abgeschlossen geführt. Die Prüfung des Code-Baselines `6a25a1d` bestätigte die generelle
Pipeline, aber noch keine vollständige Crash-Recovery und keinen schreibfreien Opencast-Dry-Run
(siehe [Prüfbericht](PRUEFBERICHT.md#3-lokale-transkription)). Der aktuelle Reparaturcode ist
mit Commit `5547703` deployed; eine reale Aufzeichnung wurde mit 12 von 12 Blöcken zurückgelesen.
Das bestätigt den normalen Pfad dieser Aufzeichnung. Recovery und `--force` auf bestehenden
Nutzerinhalten bleiben von dieser Live-Abnahme ausgenommen.
- Opencast-Discovery und lokale Whisper-Transkription mit Crash-/Resume-sicherem Manifest
- YouTube-Discovery über LearnWeb-Seiten, Untertitel-Cascade und cookie-freier Audio-Fallback
- Idempotente Notion-Erstellung für Learnweb-Inhalt und Meeting-Notiz
- `--cmid`, `--course`, `--url`, `--limit`, `--force` und write-freier `--dry-run`
- Launchd-Vorlage für den lokalen Mac; keine Aktivierung durch das Repository

### Phase 4 — Notion-Button Trigger (optional, zurückgestellt)

Bestehendes Vorhaben: [Notion-Button](roadmap/notion-button.md). Der Railway-Webhook übernimmt
bereits den manuellen Start. Direkter Notion-Button inklusive Auth-Header ist nicht verifiziert.

### Phase 5 — Forum/Ankündigungen + Assignments (optional, Zukunft)

Bestehende Zukunftsidee: [Foren und Aufgaben](roadmap/foren-aufgaben.md).
Der Umfang bleibt ungeklärt; es ist keine Umsetzung freigegeben.

---

## Kritische Integrationsregeln

1. **`cmid` ist Wahrheit** — nie Name oder URL als Identität.
2. **Nie lokal löschen** wenn etwas remote verschwindet — `status='removed'` setzen.
3. **Bestehende Notion-Seiten nur gezielt patchen**: Der Sync gleicht Quellen über ihre eindeutige Identität ab und ergänzt fehlende Eigenschaften. Eine geänderte `resource`-Datei wird auf derselben Notion-Seite nur ersetzt, wenn die vorhandene Anlage bytegleich zum letzten Manifest-Hash ist; manuelle Änderungen bleiben erhalten. `folder`-Dateien werden pro Datei auch bei unveränderten Links auf Inhaltsänderungen geprüft. Texte von `page` und Zieladressen von `url` werden nicht allgemein aktualisiert; bestehende Kurs-Select-Werte werden nicht überschrieben. Aktuelle Quellen-, Datei- und Duplikatabnahmen stehen im Reparaturbericht.
4. **Single-Replica** — Railway-Volume erlaubt keinen Multi-Replica-Betrieb.
5. **`state.db` nie in main-Branch** — gitignored; Persistenz über Railway-Volume.
6. **Organizer-Skill bleibt extern** — Sync = remote→Notion. Organize = lokal→kuratiert.
7. **Transkription bleibt lokal** — keine Whisper-Abhängigkeiten und kein Opencast-Download auf Railway.
8. **Keine LearnWeb-Cookies an YouTube** — YouTube-Untertitel und -Audio laufen cookie-frei.

---

## Offene Punkte / Entscheidungen

- **Notion API Version**: Der Code ist auf `2022-06-28` gepinnt; eine Aktualisierung erfolgt separat.
- **Notion-Button für Webhook**: Fähigkeit zu authentifizierten Aufrufen und konkrete
  Nutzbarkeit bleiben im [Vorhaben](roadmap/notion-button.md) offen.

## Dokumentationspflege

Ziel und Grenzen im Abschnitt „Kontext & Problem“ bleiben bei reiner Pflege unverändert.
Die [Roadmap](roadmap/README.md) ist wegen mehrteiliger Vorhaben und eigener Abnahmekriterien
als Verzeichnis geführt. Nach Änderungen Umfang, Zustand und Links zwischen Übersicht und
Vorhaben abgleichen. Beim Formwechsel Inhalte und gültige Verweise erhalten; Löschungen
bleiben freigabepflichtig. Neue Vorhaben benötigen ausdrücklichen Auftrag oder Freigabe.

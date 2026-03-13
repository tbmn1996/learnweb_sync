# learnweb_sync — Projektplan

> Dieses Dokument ersetzt die beiden Explorations-Docs (`learnweb_sync_exploration.md`,
> `learnweb_organizer_integration.md`) und ist die einzige Wahrheitsquelle für Architektur
> und Entwicklungsplan des Projekts. Es wird als `docs/PLAN.md` im Repo geführt.

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
reagieren können (z.B. ein zukünftiger "LearnWeb Processor" Agent).

---

## Technische Rahmenbedingungen (geprüft)

| Frage | Ergebnis |
|-------|----------|
| Moodle Web Service API | ❌ Nicht verfügbar (404 auf `/webservice/rest/server.php`) |
| `/my/` Dashboard | ❌ 404, aber `/my/index.php` ✅ (bestehender Login-Code nutzt das bereits) |
| Letzte-Aktivitäten-Block | ❌ Nicht vorhanden |
| HTML-Struktur Kursseiten | ✅ Bekannt, stabil, gut parsebar |
| Kursseiten-Zugriff per Session | ✅ Bestehender Code beweist es |
| Datei-Download per Session | ✅ Gleicher Mechanismus wie ZIP-Download (requests.Session mit Cookies) |
| Notion File Upload API | ✅ Vorhanden (`POST /v1/file_uploads`, bis 20MB single_part) |
| Notion Webhook Button | ✅ Kann POST an beliebige URL senden |
| Eigener Server | ❌ Nicht vorhanden → GitHub Actions als Ausführungsumgebung |

---

## Was wir bauen

Ein Python-CLI-Tool (`learnweb_sync.py`) mit folgenden Kommandos:

```
python learnweb_sync.py scan         # Kurse scrapen, neue Inhalte im Manifest erfassen
python learnweb_sync.py push         # Neue Manifest-Einträge → Datei laden + Notion-Seite anlegen
python learnweb_sync.py run          # scan + push (Standard-Befehl für automatische Läufe)
python learnweb_sync.py export-zips  # Backup: alle Kurse als ZIP herunterladen (bisheriger Code)
```

---

## Architektur

### Gesamtdatenfluss

```
[GitHub Actions, täglich 06:00 UTC  |  manuell via workflow_dispatch]
         │
         ▼
[0. state.db von letztem Run laden]  ← actions/download-artifact
         │
         ▼
[1. Login]  sso.uni-muenster.de/LearnWeb/learnweb2/login/index.php
         │  (CSRF-Token aus logintoken-Feld, POST mit Credentials)
         │  → requests.Session mit MoodleSession-Cookie
         ▼
[2. Kursliste]  /my/index.php
         │  extrahiert: course_url, course_id, shortname
         │  (bestehender Code aus learnweb_download.py)
         ▼
[3. Kursseiten scrapen]  /course/view.php?id={course_id}
         │  extrahiert pro Aktivität:
         │    cmid      → <li data-id="...">          (stabiler Schlüssel)
         │    modtype   → class="... modtype_resource" (Typ)
         │    name      → data-activityname="..."      (Anzeigename)
         │    section   → <div class="course-section-header"> sectionname
         │    view_url  → href=".../mod/resource/view.php?id={cmid}"
         ▼
[4. Manifest-Vergleich]  state.db (SQLite)
         │  neu     = cmid nicht in Tabelle resources
         │  [Änderungserkennung per Datei-Hash: Phase 2+]
         ▼
[5. Datei herunterladen]  (nur modtype_resource)
         │  GET /mod/resource/view.php?id={cmid}  mit Session-Cookie
         │  → folgt Redirect zu pluginfile.php → echte Datei
         │  Dateiname aus Content-Disposition Header
         │  Schreiben in Temp-Datei, dann umbenennen (atomar)
         ▼
[6. Notion-Seite anlegen]
         │  POST /v1/file_uploads  → file_upload_id
         │  PUT  upload_url        → Datei hochladen (single_part ≤20MB)
         │  POST /v1/pages         → Seite in "Learnweb Inhalte" anlegen
         │                           mit file_upload_id in "LW Download"
         ▼
[7. Manifest aktualisieren]  notion_id + status='synced' in state.db
         │
         ▼
[8. state.db hochladen]  → actions/upload-artifact
         │
         ▼
[Notion: "Page Created" Trigger → Custom Agent kann reagieren]
```

### HTML-Parsing (Kursseiten)

Jede Aktivität ist ein `<li data-for="cmitem" data-id="{cmid}">`:

```html
<li class="activity resource modtype_resource"
    id="module-3857603"
    data-for="cmitem"
    data-id="3857603">
  <div data-activityname="Vorlesung 1" data-region="activity-card">
    <a href=".../mod/resource/view.php?id=3857603">
      <span class="instancename">Vorlesung 1 <span class="accesshide">File</span></span>
    </a>
  </div>
</li>
```

**Bekannte Aktivitätstypen** (aus HTML-Analyse der Inf1-Kursseite):

| modtype | Behandlung Phase 1 | Behandlung Phase 2+ |
|---------|-------------------|---------------------|
| `resource` | Metadaten im Manifest | Datei herunterladen + in Notion hochladen |
| `forum` | Metadaten im Manifest | Forum-Seite scrapen für Ankündigungen (Phase 5) |
| `url` | Metadaten im Manifest | Nur URL in Notion eintragen |
| `opencast` | Metadaten im Manifest | Kein Download (Video zu groß) |
| `assign` | Metadaten im Manifest | Deadline in Notion (Phase 5) |
| `label` | Ignorieren (nur Layout) | – |

Abschnittsstruktur: `<div class="course-section-header">` mit `<span class="sectionname">` und `data-id`.

### SQLite-Manifest (`state.db`)

```sql
CREATE TABLE IF NOT EXISTS resources (
    cmid        TEXT PRIMARY KEY,   -- Moodle course module ID, stabiler Schlüssel
    course_id   TEXT NOT NULL,      -- z.B. "88671"
    course_name TEXT NOT NULL,      -- z.B. "Informatik I -2025_2"
    modtype     TEXT NOT NULL,      -- resource / forum / url / opencast / assign
    name        TEXT NOT NULL,      -- data-activityname
    section     TEXT,               -- Abschnittsname
    view_url    TEXT,               -- /mod/resource/view.php?id={cmid}
    first_seen  TEXT NOT NULL,      -- ISO-8601 UTC
    last_seen   TEXT NOT NULL,      -- ISO-8601 UTC
    file_hash   TEXT,               -- MD5 der heruntergeladenen Datei
    file_name   TEXT,               -- originaler Dateiname
    notion_id   TEXT,               -- Notion Page ID nach Push
    status      TEXT DEFAULT 'new'  -- new / synced / error / removed
);
```

### Notion-Integration

**Entwicklungsphase:** `Learnweb Inhalte (TESTING)`
Notion DB-ID: `322bf244cadc806cbabbf39757c3e27f`
Notion Data Source ID: `322bf244-cadc-81e9-8e37-000b3f6741fe`

**Produktionsdatenbank (nach Abschluss der Tests):** `Learnweb Inhalte`
Notion Collection ID: `321bf244-cadc-8075-a7b4-000b872536b0`

> Während der Entwicklung (Phase 2) wird ausschließlich in die TESTING-DB geschrieben.
> Umstieg auf Produktion erst nach expliziter Freigabe durch Thomas.

**Feldmapping beim Anlegen einer neuen Seite:**

| Notion-Feld | Typ | Quelle / Logik |
|-------------|-----|----------------|
| `Name` | title | `data-activityname` aus HTML |
| `Kurs` | select | course shortname → manuelle Mapping-Tabelle im `.env` oder Config |
| `Kurs-ID` | text | `course_id` (z.B. "88671") |
| `Kategorie` | select | Heuristik: Name enthält "Vorlesung"/"Lecture"→`L Lecture`, "Tutorial/Tutorium"→`T Tutorial`, "Exam/Klausur"→`E Exam`, "Python"→`P Python`, "Aufgabe/Blatt"→`A Aufgabensammlung`, "Script/Skript"→`S Script`, sonst→`R Resource` |
| `Format` | select | Dateiendung aus Content-Disposition (pdf/ipynb/py/pkl/zip) |
| `Quell-Semester` | select | aus `.env`: `CURRENT_SEMESTER=SoSe 26` |
| `LW Download` | file | file_upload_id nach Notion File Upload |
| `Nr` | text | cmid (für spätere Deduplizierung / Referenz) |
| `Variante` | select | Standard: `Original` (bei Push immer gesetzt) |
| `Thema` | text | leer lassen (manuell oder per AI-Agent befüllen) |
| `Course` | relation | leer lassen Phase 2; Phase 3+ via `KurseLearnWeb`-DB verknüpfen |

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

# Schritt 2: Datei hochladen (POST, nicht PUT! + Notion-Version Header erforderlich)
with open(tmp_file, "rb") as f:
    requests.post(upload_url,
                  headers={"Authorization": f"Bearer {NOTION_TOKEN}",
                           "Notion-Version": "2022-06-28"},
                  files={"file": (filename, f, "application/pdf")})

# Schritt 3: Seite anlegen mit Referenz auf file_upload_id
properties["LW Download"] = {
    "files": [{"type": "file_upload", "file_upload": {"id": upload_id}}]
}
```

**Hinweis:** Dateigrößenlimit 20MB für `single_part`. Bei größeren Dateien (unwahrscheinlich
für Kursfolien): Seite ohne Dateianhang anlegen, `status='error'` im Manifest, manuell prüfen.

**Kurs-Verknüpfung:** Relation zu `KurseLearnWeb` wird gesetzt wenn ein Eintrag mit passender
`LW-ID` in der `KurseLearnWeb`-Datenbank existiert. Kein Fehler wenn nicht vorhanden.

---

## Repo-Struktur

```
learnweb_sync/
├── .env.example              # Vorlage für alle benötigten Variablen
├── .gitignore                # .env, state.db, logs/, *.zip, downloads/, __pycache__
├── .github/
│   └── workflows/
│       └── sync.yml          # Täglich 06:00 UTC + workflow_dispatch
├── README.md                 # Setup-Anleitung (Secrets, erster Lauf)
├── requirements.txt          # requests, beautifulsoup4, python-dotenv, notion-client
├── learnweb_sync.py          # Haupt-Script (ersetzt learnweb_download.py)
├── state.db                  # gitignored; in Actions via Artifact persistiert
├── downloads/                # gitignored; temporäre Dateien während eines Runs
├── docs/
│   ├── PLAN.md               # dieses Dokument
│   └── example_course_source_code.html  # HTML-Referenz (gitignored oder .gitkeep)
└── logs/                     # gitignored
```

`.env.example`-Inhalt:
```
LEARNWEB_URL=https://sso.uni-muenster.de/LearnWeb/learnweb2
LEARNWEB_USERNAME=
LEARNWEB_PASSWORD=
NOTION_TOKEN=
NOTION_LW_DB_ID=321bf244cadc804b9d3dd94cb2daaad7
CURRENT_SEMESTER=SoSe 26
# Optionales Kurs-Mapping (shortname → Notion-Select-Wert)
# COURSE_MAP={"Informatik I -2025_2": "Inf1", "AFWFW-2025_2": "Ana"}
```

**Entscheidung:** Kein `src/`-Package, kein `pyproject.toml`. Ein einzelnes flaches Script,
das für einen Nicht-Programmierer lesbar bleibt. Komplexität nur erhöhen wenn nötig.

---

## Entwicklungsphasen

### Phase 1 — Scraper + Manifest ✅ ABGESCHLOSSEN

**Ziel:** Lokaler Lauf erkennt alle Aktivitäten in allen Kursen und zeigt welche neu sind.

Deliverables:
- `learnweb_sync.py` mit `scan`-Kommando ✅
- Login + Kursliste aus bestehendem `learnweb_download.py` übernommen ✅
- BeautifulSoup-Parser für `<li data-for="cmitem">` auf Kursseiten ✅
- SQLite-Manifest (`state.db`) mit Vergleichslogik ✅
- `sync-courses`-Kommando: KurseLearnWeb (TESTING) in Notion befüllen ✅

### Phase 2 — Datei-Download + Notion-Push ✅ ABGESCHLOSSEN

**Ziel:** Neue `modtype_resource`-Einträge automatisch in Notion anlegen.

Deliverables:
- `push`-Kommando: lädt Datei herunter → Notion File Upload API → Seite anlegen ✅
- `run`-Kommando: `scan` + `push` in einem Schritt ✅
- Fehlerbehandlung: >20MB oder Download-Fehler → Seite ohne Datei anlegen, `status='error'` ✅
- Deduplizierung: wenn `cmid` bereits `notion_id` hat → überspringen ✅
- `COURSE_MAP` JSON-Env-Variable: shortname → Kurs-Select-Wert ✅
- `_guess_variante()`: Solution / Partial-Solution / Template / Annotated / Original ✅
- `_guess_kategorie()`: erweiterte Heuristik mit nummerierten Präfixen, mock, mitschrift ✅

**Testergebnis (TESTING DB, WS 25/26):** 91 Ressourcen, 0 Fehler, Kurs 100 %, R Resource 35 %.

**Hinweis Notion File Upload API:** Schritt 2 muss `POST` (nicht `PUT`) auf `upload_url` sein,
mit `Notion-Version: 2022-06-28` Header und `files={"file": (filename, bytes, content_type)}`.

### Phase 3 — GitHub Actions Automation ← **als nächstes**

**Ziel:** Täglicher automatischer Lauf ohne manuelle Intervention.
**Voraussetzung:** Produktions-Switchover (NOTION_LW_DB_ID + NOTION_COURSES_DB_ID auf Produktion setzen).

Deliverables:
- `.github/workflows/sync.yml`
- Secrets im GitHub Repo: `LEARNWEB_USERNAME`, `LEARNWEB_PASSWORD`, `NOTION_TOKEN`,
  `NOTION_LW_DB_ID`, `NOTION_COURSES_DB_ID`, `CURRENT_SEMESTER`, `COURSE_MAP`
- `state.db`-Persistenz via Artifacts:
  ```yaml
  - uses: actions/download-artifact@v4        # Manifest vom letzten Run laden
    with: {name: state-db, path: .}
    continue-on-error: true                   # beim allerersten Run kein Artefakt vorhanden
  - run: python learnweb_sync.py run
  - uses: actions/upload-artifact@v4          # Manifest für nächsten Run speichern
    with: {name: state-db, path: state.db, retention-days: 90}
  ```
- `workflow_dispatch` ohne Parameter → manueller Trigger per GitHub-UI
- Cron: `0 6 * * 1-5` (Mo–Fr 06:00 UTC = 07:00/08:00 MEZ/MESZ)

**Produktions-Switchover (vor Phase 3):**
- `NOTION_LW_DB_ID` → `321bf244cadc804b9d3dd94cb2daaad7` (Learnweb Inhalte Produktion)
- `NOTION_COURSES_DB_ID` → KurseLearnWeb Produktion (Collection `321bf244-cadc-80db-b799-000bec418f90`)
- `COURSE_MAP` um alle aktiven Kurse erweitern

### Phase 4 — Notion-Button Trigger (optional)

**Ziel:** Sync per Button-Klick in Notion auslösen.

**Problem:** Notion Webhook-Buttons senden POST ohne Auth-Header. GitHub API benötigt
`Authorization: Bearer TOKEN`.

**Lösung:** Kleiner Proxy via Cloudflare Worker (kostenloser Free Tier, 100k Requests/Tag):
```
Notion Button → POST notion-webhook-url
    → Cloudflare Worker (hat GITHUB_TOKEN als Secret)
        → POST api.github.com/repos/.../actions/workflows/sync.yml/dispatches
```
Cloudflare Worker: ~15 Zeilen JavaScript, deployment via Wrangler CLI.

Alternative ohne Proxy: n8n Self-Host oder Make Free Tier als Vermittler.

### Phase 5 — Forum/Ankündigungen + Assignments (optional, Zukunft)

**Ziel:** Nicht nur Dateien, sondern auch Ankündigungen und Abgabefristen erfassen.

Technische Herausforderungen:
- Forum-Inhalte stehen nicht auf der Kursseite → separates Scraping von `/mod/forum/view.php`
- Assignments haben Deadlines → scrapen und in `Prüfungsversuche`/`Veranstaltungen` eintragen

Scope erst definieren wenn Phase 2 stabil und im Einsatz.

---

## Umgang mit bestehendem Code

`learnweb_download.py` enthält:
- `login(session)` → **direkt übernehmen**, unverändert
- `get_courses(session)` → **direkt übernehmen**
- `get_course_info(session, course_url)` → nur der `contextid`/`sesskey`-Teil wird für ZIP-Export
  gebraucht; für Phase 1 nicht benötigt
- `download_course(session, info)` → als `export-zips`-Kommando **erhalten**

Die Datei `learnweb_download.py` wird nicht gelöscht sondern in `learnweb_sync.py` aufgegangen.
Nach erfolgreichem Test von Phase 1 kann `learnweb_download.py` aus dem Repo entfernt werden.

---

## Kritische Integrationsregeln

1. **`cmid` ist Wahrheit** — nie Name oder URL als Identität. Dozenten benennen Dateien um.
2. **Nie lokal löschen** wenn etwas remote verschwindet — `status='removed'` setzen, nicht löschen.
3. **Notion-Felder nie überschreiben** wenn `notion_id` bereits gesetzt ist. Nur neu befüllen.
4. **Atomare Downloads** — erst in Temp-Datei (`downloads/tmp_{cmid}`), dann umbenennen.
5. **`state.db` nie ins Git** — wird via GitHub Actions Artifact persistiert.
6. **Organizer-Skill bleibt extern** — der bestehende Claude-Prompt für ZIP-Sortierung ist
   ein externer Post-Processor, kein Teil des Sync-Kerns. Die Grenze ist unveränderlich:
   Sync = remote→lokal/Notion. Organize = lokal→kuratiert. Diese Schichten nie vermischen.

---

## Offene Punkte (zu klären bei Implementierung)

- **Kurs-Mapping Config**: Wie wird `course shortname → Notion-Kurs-Select-Wert` konfiguriert?
  Vorschlag: JSON in `.env`-Datei (manuell gepflegt, einmalig beim Setup).
- **`state.db` bei GitHub Actions erster Run**: Kein Artefakt vorhanden →
  `continue-on-error: true` in der download-Step, leere DB wird neu erstellt.
- **Notion API Version**: Aktuell `2022-06-28` (letzte stabile); bei Bedarf auf `2026-03-11`
  aktualisieren wenn File Upload API neuere Version erfordert.
- **Benachrichtigung bei Fehlern**: GitHub Actions sendet bei Workflow-Failure automatisch
  Email an Repository-Owner → kein zusätzlicher Notifier nötig.

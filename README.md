# learnweb_sync

Synchronisiert LearnWeb (Moodle, Uni Münster) automatisch mit Notion.
Neue Dateien, Folien und Ressourcen werden erkannt und als Notion-Seiten angelegt — inklusive PDF-Anhang, den Notion AI lesen kann.

## Was das Skript macht

1. Loggt sich in LearnWeb ein
2. Scannt alle Kurse auf neue Aktivitäten (Dateien, Ordner, Foren, etc.)
3. Vergleicht mit dem lokalen Manifest (`state.db`) — nur echte Neuigkeiten werden gemeldet
4. (Phase 2) Lädt neue Dateien herunter und legt Notion-Seiten an

## Setup

### 1. Python-Umgebung einrichten

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. `.env` anlegen

```bash
cp .env.example .env
```

Dann `.env` mit den eigenen Zugangsdaten befüllen (LearnWeb-Login + Notion-Token).

### 3. Ersten Scan starten

```bash
python learnweb_sync.py scan
```

## Befehle

| Befehl | Was er tut |
|--------|-----------|
| `python learnweb_sync.py scan` | Alle Kurse scannen, neue Aktivitäten ausgeben |
| `python learnweb_sync.py push` | Neue Ressourcen herunterladen + Notion-Seiten anlegen *(Phase 2)* |
| `python learnweb_sync.py run` | `scan` + `push` in einem Schritt *(Phase 2)* |
| `python learnweb_sync.py export-zips` | Alle Kurse als ZIP-Backup herunterladen |

## Projektstruktur

```
learnweb_sync/
├── .env.example          # Vorlage für Zugangsdaten
├── .github/workflows/    # GitHub Actions (Phase 3)
├── docs/PLAN.md          # Architektur- und Entwicklungsplan
├── learnweb_sync.py      # Haupt-Skript
├── requirements.txt
├── state.db              # Manifest (gitignored, lokal / via Actions Artifact)
└── logs/                 # Logdateien (gitignored)
```

## Entwicklungsphasen

- **Phase 1** ✅ Scraper + Manifest (lokal, kein Notion)
- **Phase 2** — Datei-Download + Notion-Push
- **Phase 3** — GitHub Actions (täglicher automatischer Lauf)
- **Phase 4** — Notion-Button Trigger
- **Phase 5** — Ankündigungen + Abgabefristen

Details: [`docs/PLAN.md`](docs/PLAN.md)

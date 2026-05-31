# learnweb_sync — Agent-Orientierung

## Was das Projekt tut
Synchronisiert Vorlesungen, Inhalte und Aktivitäten aus LearnWeb (Uni-Münster Moodle) in zwei Notion-Datenbanken: eine für Kursverwaltung, eine für strukturierte Lehr-Inhalte.

## Starten
```bash
cd /Users/thomasniermann/Scripts/learnweb_sync
source .venv/bin/activate
python learnweb_sync.py <command>
```

**Verfügbare Befehle:**
- `sync-courses` — Kurse in KurseLearnWeb (Notion) prüfen/anlegen
- `scan` — LearnWeb scrapen, neue Aktivitäten im Manifest erfassen
- `push` — Pushbare Inhalte nach Notion schreiben/aktualisieren
- `run` — scan + push (Phase 2, experimentell)
- `diagnose-resource-errors` — Offene Resource-Fehler klassifizieren
- `export-zips` — Alle Kurse als ZIP-Backup herunterladen

**Launchd-Service:** `de.thomasn.learnweb-sync` (angenommen aus Memory; im Code nicht explizit referenziert)

## Architektur
- **LearnWeb-Login:** SSO-Authentifizierung gegen `https://sso.uni-muenster.de/LearnWeb/learnweb2`
- **Manifest-DB:** `manifest.db` — SQLite, speichert Aktivitäts-Struktur und Sync-Status pro Kurs
- **State-DB:** `state.db` — SQLite, Zwischenspeicher für Kurs-/Inhalts-Metadaten
- **Notion Push:** Batches gegen zwei Notion-DBs via Notion API
- **Semester-Logik:** Automatisch abgeleitet aus NRW-Semesterkalender (SoSe 01.04.–30.09., WS 01.10.–31.03., Tz: Europe/Berlin)
- **Course-Mapping:** JSON in `COURSE_MAP` — Moodle-Shortname → Notion Select-Wert

## Notion-DBs
- `a44bf244-cadc-8266-9c11-816719a8ec06` — **KurseLearnWeb** (Produktiv, sync-courses)
- `322bf244-cadc-806c-babb-f39757c3e27f` — **Learnweb Inhalte** (Testing, push-Phase)
  - Produktion nach Freigabe: `321bf244-cadc-804b-9d3d-d94cb2daaad7`
- `320bf244-cadc-80e3-b714-ece5096f83d7` — **MODULHANDBUCH** (Referenzbasis)

## .env-Variablen
- `LEARNWEB_URL`, `LEARNWEB_USERNAME`, `LEARNWEB_PASSWORD` — SSO-Creds
- `NOTION_TOKEN` — Notion API-Key
- `NOTION_COURSES_DB_ID` — KurseLearnWeb (oben)
- `NOTION_LW_DB_ID` — Learnweb Inhalte (oben; produktiv vs. testing wechselbar)
- `COURSE_MAP` — JSON-String für Shortname-Mapping
- `CURRENT_SEMESTER_OVERRIDE` — Nur für Backfills (z. B. `WS 25/26`)
- `SEMESTER_TIMEZONE` — Tz-Override (Default: `Europe/Berlin`)
- `DOWNLOAD_DIR` — Für `export-zips` (optional)
- `LOG_DIR` — Pfad zu Logs (optional)
- `STATE_DB_PATH` — Pfad zur `state.db` (optional)

## Fallstricke
- **Semester-DB-Wechsel:** Nach `CURRENT_SEMESTER_OVERRIDE` oder `NOTION_LW_DB_ID` wird Manifest **nicht** automatisch gelöscht — alte Daten können durchsickern. Im Zweifelsfall `manifest.db` + `state.db` löschen und neu scannen.
- **Manifest-Struktur:** Ist abhängig von LearnWeb-HTML-Struktur; Änderungen bei Moodle-Updates führen zu Parse-Fehlern. **Logs prüfen** vor `push`.
- **Notion Push-Batches:** Intern begrenzt auf ~100 Pages/Update pro Aufruf; bei großen Kursen kann `push` mehrere Minuten dauern.
- **SSO-Timeouts:** LearnWeb-Session läuft nach ~2h ab; bei langen `scan`-Läufen ggf. neu-Login nötig (Code prüft nicht automatisch).

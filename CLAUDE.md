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
- `transcribe` — **Lokaler Mac-Worker:** Opencast-Vorlesungsaufzeichnungen finden, on-device transkribieren, Transkript als Notion-Seite ablegen (siehe Abschnitt „Transkriptions-Worker"). Argumente: `--cmid N`, `--course X`, `--url <LearnWeb-URL>`, `--limit N`, `--force` (nur mit `--cmid`/`--course`/`--url`), `--dry-run`.

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
- `322bf244-cadc-806c-babb-f39757c3e27f` — **Learnweb Inhalte** (Produktiv)
- `321bf244-cadc-804b-9d3d-d94cb2daaad7` — **Learnweb Inhalte (OLD)** (archiviert; historische Backlinks)
- `320bf244-cadc-80e3-b714-ece5096f83d7` — **MODULHANDBUCH** (Referenzbasis)

## .env-Variablen
- `LEARNWEB_URL`, `LEARNWEB_USERNAME`, `LEARNWEB_PASSWORD` — SSO-Creds
- `NOTION_TOKEN` — Notion API-Key
- `NOTION_COURSES_DB_ID` — KurseLearnWeb (oben)
- `NOTION_LW_DB_ID` — Learnweb Inhalte (oben; produktive Learnweb-Inhalte-DB)
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

## Transkriptions-Worker (`transcribe`, nur lokal auf dem Mac)

Findet YouTube-Videos und Opencast-Vorlesungsaufzeichnungen, transkribiert sie **on-device** (kostenlos) mit Whisper (oder Untertitel wenn vorhanden) und legt das Transkript als eigene **Meeting-Notiz-Seite** in Notion ab.

**Zwei Transkriptions-Pfade:**

1. **`--url <LearnWeb-Abschnitts-URL>`** — Lädt die Seite, findet verlinkte YouTube-Videos (direkte Links oder `mod/url`-Aktivitäten mit YouTube-Ziel), transkribiert sie. Transkriptquelle: **Untertitel zuerst** (YouTube, bevorzugte Sprachen aus `YT_SUBTITLE_LANGS`), falls nicht vorhanden → on-device Whisper (Audio via yt-dlp, cookie-frei). `--url` ist gegenseitig exklusiv mit `--cmid`/`--course`.

2. **`--cmid N` / `--course X`** — Findet Opencast-Aufzeichnungen in den Kursen, transkribiert sie mit Whisper.

**Sicherheit:** YouTube-Aufrufe laufen ohne LearnWeb-Cookies (Trennung LearnWeb/Google). Der `--url`-Pfad nutzt nur read-only Notion-Lookups → `--dry-run` schreibt garantiert nichts.

**Warum lokal-only:** On-Device-Whisper (MLX/Apple Silicon) läuft nicht auf Railway (CPU-Linux). Der Railway-Dienst macht weiterhin nur `run`/`scan`/`push` (Dateien). `opencast` ist bewusst **nicht** in `PUSHABLE_MODTYPES`, sondern in der separaten Konstante `TRANSCRIBABLE_MODTYPES` — der Railway-Pfad bleibt unverändert.

**Ablauf (Zustandsautomat, je Aufzeichnung sofort persistiert):**
`login` → sync-markierte Kurse → opencast-Aktivitäten → Episoden-Discovery → atomares Claiming (SQLite + `flock`) → yt-dlp-Download → ffmpeg (16 kHz mono) → Whisper → create-or-find Inhalts-Eintrag + Meeting-Seite → Body in Batches (≤100 Blöcke, ≤1900 Zeichen) anhängen (resume-fähig) → `done`. Dedupe-Key: `{cmid}-{sha1(episode_id|media_url)[:12]}`, Tabelle `transcripts` in `state.db`.

**Paket `transcription/`:** `recordings.py` (Opencast-Discovery, neues `window.episode`-JSON + altes Listenformat), `downloader.py` (yt-dlp/ffmpeg/ffprobe), `transcriber.py` (mlx-whisper primär, faster-whisper Fallback, Capability-Detection), `notion_blocks.py` (Segment→Absatz, Timestamps, Chunking), `manifest.py` (Key + Zustandsautomat), `types.py` (`Recording`, `Segment`).

**Installation (lokal, freigabepflichtig):**
```bash
source .venv/bin/activate
pip install -r requirements-transcription.txt   # mlx-whisper, faster-whisper, yt-dlp
brew install ffmpeg                              # ffmpeg + ffprobe (System-Binaries)
```

**Zusätzliche .env-Variablen:** `NOTION_MEETING_DB_ID` (Meeting-Notizen-DB `30bbf244…`, **Pflicht** außer bei `--dry-run`), optional `WHISPER_MODEL` (Default `large-v3-turbo`), `WHISPER_LANGUAGE` (`de`), `YT_SUBTITLE_LANGS` (Default: `de,en` abgeleitet aus `WHISPER_LANGUAGE` + en), `TRANSCRIBE_MAX_ATTEMPTS`, `TRANSCRIBE_LOCK_PATH`, `TRANSCRIBE_WORK_DIR`, `YT_DLP_BIN`/`FFMPEG_BIN`/`FFPROBE_BIN`.

**Performance (gemessen 06/2026, Apple Silicon, `large-v3-turbo`):** ~Faktor 8 schneller als Echtzeit (120 s Audio → 15 s Transkription) → eine 90-Min-Vorlesung ≈ 11 min Transkription + Video-Download. Modell-Erstdownload einmalig ~1,5 GB (danach gecacht). Kosten = nur Strom/Zeit.

**Automatisierung:** Vorlage `launchd/de.thomasn.learnweb-transcribe.plist` (RunAtLoad + täglich 13:00/20:00). Aktivierung (`launchctl load`) **erst nach expliziter Freigabe** (CLAUDE.md/Workspace §5).

**Bekannte Einschränkungen (Stand 06/2026):**
- **Aufzeichnungsdatum:** LearnWeb-Opencast liefert pro Episode kein verlässliches Datum (kein `created`/`start` im `window.episode`-JSON; Titel enthalten nur eine laufende Nummer). `recorded_at` bleibt daher bewusst leer, statt zu raten → in Notion bleibt `Datum` leer.
- **Single-Episode-Format:** Manche Aktivitäten (z. B. einzelne BWL-VLs) liefern nur die Medien-URL ohne Metadaten → generischer Titel „Aufzeichnung &lt;cmid&gt;", keine `episode_id`. Transkription funktioniert trotzdem.
- **Alte/abgemeldete Kurse:** Bei nicht mehr zugänglichen Kursen findet die Discovery 0 Aufzeichnungen.

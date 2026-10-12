# learnweb_sync — Agent-Orientierung

## Prüfstand und Arbeitsgrenze

Die [Semesterprüfung vom 12.10.2026](docs/PRUEFBERICHT.md) dokumentiert den damaligen
Auditstand; ihr ursprünglicher Auftrag war auf Prüfung und Dokumentation begrenzt. Ein
separater Reparaturplan wurde umgesetzt und live geprüft. Den abgenommenen Stand und die
verbleibenden Providergrenzen führt die [Reparatur-Nachprüfung](docs/reviews/261012-Reparatur-Abnahme.md).
Für Betrieb und Wiederherstellung zuerst [BETRIEB.md](docs/BETRIEB.md) und diese aktuellen Befunde lesen.

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
- `scan` — zuerst Kursabgleich (kann Notion ändern), danach neue Aktivitäten im Manifest erfassen
- `push` — Pushbare Inhalte nach Notion schreiben/aktualisieren
- `run` — sync-courses + scan + push (produktiver Railway-Ablauf)
- `diagnose-resource-errors` — Offene Resource-Fehler klassifizieren
- `export-zips` — Alle Kurse als ZIP-Backup herunterladen
- `transcribe` — **Lokaler Mac-Worker:** Opencast- und YouTube-Aufzeichnungen nach Notion transkribieren (siehe Abschnitt „Transkriptions-Worker"). Argumente: `--cmid N`, `--course X`, `--url <LearnWeb-URL>`, `--limit N`, `--force` (nur mit `--cmid`/`--course`/`--url`), `--dry-run`.

## Architektur
- **LearnWeb-Login:** SSO-Authentifizierung gegen `https://sso.uni-muenster.de/LearnWeb/learnweb2`
- **State-DB:** `state.db` — SQLite-Manifest für Aktivitäten, Sync-Status und lokale Transkriptionszustände
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
- **Semester-/DB-Wechsel:** Nach `CURRENT_SEMESTER_OVERRIDE` oder `NOTION_LW_DB_ID` wird `state.db` nicht automatisch zurückgesetzt. Vor einem Backfill Pfad und Bestand prüfen; die Produktions-DB nicht ungeprüft löschen.
- **Manifest-Struktur:** Ist abhängig von LearnWeb-HTML-Struktur; Änderungen bei Moodle-Updates führen zu Parse-Fehlern. **Logs prüfen** vor `push`.
- **Notion-Batches:** Seiten werden einzeln angelegt; angehängte Body-Blöcke in Batches bis 100.
- **SSO-Timeouts:** Die Session kann ablaufen; der Code meldet sich nicht automatisch erneut an. Eine feste Laufzeit ist nicht aktuell verifiziert.
- **Lokales Manifest:** Die lokale `state.db` ist vom Railway-Volume getrennt und veraltet. Kein lokales `run` als unverbindlicher Test gegen die produktiven Notion-DBs.
- **Erfolgsmeldungen:** `ae1b0a0` prüft Folder-Dateien auch bei unveränderten Links auf Inhaltsänderungen und begrenzt Wiederholungen auf lesende LearnWeb-GETs; Schreibvorgänge werden nicht wiederholt. Der Commit ist gepusht und deployed; 210 Offline-Tests bestehen. Aktueller Bestand: 42 reguläre Semesterinhalte, ein Transkript, 106 bytegleich geprüfte Originaldateien; 18 Provider-Blocker bleiben offen. Details und abgeschlossene Byte-/Folder-Abnahmen stehen im [Reparaturbericht](docs/reviews/261012-Reparatur-Abnahme.md).

## Transkriptions-Worker (`transcribe`, nur lokal auf dem Mac)

Findet YouTube-Videos und Opencast-Vorlesungsaufzeichnungen, transkribiert sie **on-device** (kostenlos) mit Whisper (oder Untertitel wenn vorhanden) und legt das Transkript als eigene **Meeting-Notiz-Seite** in Notion ab.

**Zwei Transkriptions-Pfade:**

1. **`--url <LearnWeb-Abschnitts-URL>`** — Lädt die Seite, findet verlinkte YouTube-Videos (direkte Links oder `mod/url`-Aktivitäten mit YouTube-Ziel), transkribiert sie. Transkriptquelle: **Untertitel zuerst** (YouTube, bevorzugte Sprachen aus `YT_SUBTITLE_LANGS`), falls nicht vorhanden → on-device Whisper (Audio via yt-dlp, cookie-frei). `--url` ist gegenseitig exklusiv mit `--cmid`/`--course`.

2. **`--cmid N` / `--course X`** — Findet Opencast-Aufzeichnungen in den Kursen, transkribiert sie mit Whisper.

**Dry-Run:** YouTube-Aufrufe laufen ohne LearnWeb-Cookies. Im aktuellen lokalen Reparaturstand
nutzen `--url` und Opencast-`--dry-run` lesende Notion-Kursabfragen und eine In-Memory-DB; beide
schreiben nicht nach Notion oder dauerhaft in `state.db`. Dry-Runs können Medien laden und
Whisper ausführen. Diese Änderung ist in Commit `5547703` enthalten. Railway führt den
Transkriptions-Worker grundsätzlich nicht aus.

**Warum lokal-only:** On-Device-Whisper (MLX/Apple Silicon) läuft nicht auf Railway (CPU-Linux). Der Railway-Dienst macht weiterhin nur `run`/`scan`/`push` (Dateien). `opencast` ist bewusst **nicht** in `PUSHABLE_MODTYPES`, sondern in der separaten Konstante `TRANSCRIBABLE_MODTYPES` — der Railway-Pfad bleibt unverändert.

**Ablauf (Zustandsautomat, je Aufzeichnung sofort persistiert):**
`login` → sync-markierte Kurse → opencast-Aktivitäten → Episoden-Discovery → atomares Claiming (SQLite + `flock`) → yt-dlp-Download → ffmpeg (16 kHz mono) → Whisper → create-or-find Inhalts-Eintrag + Meeting-Seite → Body in Batches (≤100 Blöcke, ≤1900 Zeichen) anhängen → `done`. Dedupe-Key: `{cmid}-{sha1(discriminator)[:12]}`, erste vorhandene Angabe aus Episode-ID, Medien-URL und Quell-URL; Tabelle `transcripts` in `state.db`.

**Recovery:** Die fehlende Wiederaufnahme nach hartem Abbruch und das unsichere `--force` waren
Befunde des ursprünglichen Audit-Stands. Der aktuelle lokale Reparaturcode setzt Zwischenstände
unter exklusiver Sperre fort und gleicht Remote-Blöcke vor Append oder Ersatz ab. Nur eindeutig
eigene, unveränderte Blöcke werden gelöscht; manuelle oder nicht zuordenbare Inhalte bleiben
geschützt. Eine reale Aufzeichnung wurde mit 12 von 12 zurückgelesenen Blöcken geprüft; Force-
und Recovery-Verhalten auf Bestandsseiten bleiben durch isolierte Tests geschützt, nicht durch
destruktive Live-Force-Läufe. Details und verbleibende Grenzen stehen im Reparaturbericht.

**Paket `transcription/`:** `recordings.py` (Opencast-Discovery), `youtube.py` (YouTube-ID/Link-/Untertitel-Parsing), `downloader.py` (yt-dlp/ffmpeg/ffprobe und Untertitel-Cascade), `transcriber.py` (mlx-whisper primär, faster-whisper Fallback), `notion_blocks.py` (Segment→Absatz, Timestamps, Chunking), `manifest.py` (Key + Zustandsautomat), `types.py` (`Recording`, `Segment`, Quelltyp über `source_kind`).

**Installation (lokal, freigabepflichtig):**
```bash
source .venv/bin/activate
pip install -r requirements-transcription.txt   # mlx-whisper, faster-whisper, yt-dlp
brew install ffmpeg                              # ffmpeg + ffprobe (System-Binaries)
```

**Zusätzliche .env-Variablen:** `NOTION_MEETING_DB_ID` (Meeting-Notizen-DB `30bbf244…`, **Pflicht** außer bei `--dry-run`), optional `WHISPER_MODEL` (Default `large-v3-turbo`), `WHISPER_LANGUAGE` (`de`), `YT_SUBTITLE_LANGS` (Default: `de,en` abgeleitet aus `WHISPER_LANGUAGE` + en), `TRANSCRIBE_MAX_ATTEMPTS`, `TRANSCRIBE_LOCK_PATH`, `TRANSCRIBE_WORK_DIR`, `YT_DLP_BIN`/`FFMPEG_BIN`/`FFPROBE_BIN`.

**Performance (gemessen 06/2026, Apple Silicon, `large-v3-turbo`):** ~Faktor 8 schneller als Echtzeit (120 s Audio → 15 s Transkription) → eine 90-Min-Vorlesung ≈ 11 min Transkription + Video-Download. Modell-Erstdownload einmalig ~1,5 GB (danach gecacht). Kosten = nur Strom/Zeit.

**Automatisierung:** Vorlage `launchd/de.thomasn.learnweb-transcribe.plist` (RunAtLoad + täglich 13:00/20:00). Aktivierung (`launchctl load`) **erst nach expliziter Freigabe** (CLAUDE.md/Workspace §5).

Am 12.10.2026 ist die Vorlage lokal weder installiert noch geladen.

**Bekannte Einschränkungen (Stand 06/2026):**
- **Aufzeichnungsdatum:** LearnWeb-Opencast liefert pro Episode kein verlässliches Datum (kein `created`/`start` im `window.episode`-JSON; Titel enthalten nur eine laufende Nummer). `recorded_at` bleibt daher bewusst leer, statt zu raten → in Notion bleibt `Datum` leer.
- **Single-Episode-Format:** Manche Aktivitäten (z. B. einzelne BWL-VLs) liefern nur die Medien-URL ohne Metadaten → generischer Titel „Aufzeichnung &lt;cmid&gt;", keine `episode_id`. Transkription funktioniert trotzdem.
- **Alte/abgemeldete Kurse:** Bei nicht mehr zugänglichen Kursen findet die Discovery 0 Aufzeichnungen.

## Dokumentation und Roadmap

- [README.md](README.md): Einstieg, CLI und Testbefehl.
- [docs/PLAN.md](docs/PLAN.md): bestehendes Projektziel, Architektur und historische Lieferumfänge.
- [docs/BETRIEB.md](docs/BETRIEB.md): Laufwege, Datenhaltung, Semesterwechsel und Wiederherstellung.
- [docs/PRUEFBERICHT.md](docs/PRUEFBERICHT.md): historischer Auditstand vor der Reparatur;
  [DSH-REVIEW.md](docs/DSH-REVIEW.md) hält die unabhängigen Reviews und verworfenen Gegenbehauptungen fest.
- [docs/roadmap/README.md](docs/roadmap/README.md): bestehende Vorhaben und Zustände.

Nach Änderungen die betroffenen Dokumente und Zustände nachziehen; neue oder verschobene
Dokumente hier mit tatsächlichem Pfad verlinken. Vor jeder Roadmap-Änderung deren Pflegeregeln
lesen. Sobald ein Vorhaben eigene Abnahmekriterien, mehrere Teile oder gegenseitige Verweise
hat, gilt Übersicht plus eine Datei je Vorhaben; nur ohne diese Merkmale genügt eine kurze
Roadmap-Datei. Die derzeitige Verzeichnisform ist wegen des vorhandenen Backfills erforderlich.
Beim Formwechsel alle Inhalte, Zustände und gültigen Links erhalten, Projektverweise nachziehen;
Löschungen bleiben freigabepflichtig. Neue Vorhaben und Änderungen des bestehenden Projektziels
brauchen ausdrücklichen Auftrag oder Freigabe. Reine Pflege startet keine Zielplanung.

Falls `project-doc-watcher` eingerichtet ist, vor Doku-Arbeit
`$(git rev-parse --git-common-dir)/claudex-doc-watch/pruefprotokoll.md` auf offene Befunde lesen.
Beim Erstellen der Semesterprüfung war noch kein solches Protokoll eingerichtet.
Der relative Symlink `AGENTS.md -> CLAUDE.md` lässt Codex und Claude Code dieselbe
Projektanleitung lesen. Nach Änderungen an einer der beiden Dateien den gemeinsamen
Integritätscheck ausführen. Tests ausschließlich über `testlauf`.
